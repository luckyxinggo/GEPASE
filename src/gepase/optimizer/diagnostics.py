"""Deterministic S5 contract, ASI, and interruption diagnostics."""

from __future__ import annotations

import tempfile
from pathlib import Path
from statistics import mean
from typing import Any

from gepase.benchmarks.loader import load_cases, load_manifest
from gepase.evals.engine import MultiFidelityEvalEngine, build_submission
from gepase.evals.evidence import (
    ProviderFailureKind,
    TraceStep,
)
from gepase.evals.schema import EvidenceTier
from gepase.optimizer.asi import ASIBuilder
from gepase.optimizer.candidate import (
    PackageCandidate,
    build_seed_candidate,
    derive_candidate,
)
from gepase.optimizer.frontier import frontier_snapshot
from gepase.optimizer.gepa_adapter import (
    CandidateEvaluation,
    CandidateEvaluationRow,
    GEPASEAdapter,
)
from gepase.optimizer.materialize import materialize_candidate
from gepase.schemas.common import ArtifactRef
from gepase.store.artifacts import sha256_bytes
from gepase.store.candidates import CandidateStatus, CandidateStore


def _train_cases(root: Path, count: int = 3):
    cases = load_cases(root, load_manifest(root / "benchmarks/manifest-v1.json"))
    return [
        case
        for case in cases
        if case.skill_id == "structured-report-builder" and case.split == "train"
    ][:count]


def _synthetic_evaluation(
    candidate: PackageCandidate,
    scores: tuple[float, ...],
    *,
    suffix: str,
) -> CandidateEvaluation:
    rows = tuple(
        CandidateEvaluationRow(
            task_id=f"mock-task-{index}",
            record_id=f"mock-record-{suffix}-{index}",
            record_ref=f"records/mock-record-{suffix}-{index}.json",
            evidence_tier=EvidenceTier.E1_SIMULATED,
            score=score,
            objective_scores={
                "quality": score,
                "reliability": 0.9,
                "evidence_strength": 1 / 3,
                "cost_efficiency": 0.8,
            },
            output={"status": "mock"},
            planned_trace=(TraceStep(sequence=0, action="validate", target="fixture"),),
            uncertainty=0.1,
            provenance={
                "origin": "mock",
                "provider_id": "mock",
                "generated_by": "s5-contract-diagnostic",
            },
        )
        for index, score in enumerate(scores)
    )
    return CandidateEvaluation(
        evaluation_id=f"evaluation-{suffix}",
        candidate_id=candidate.candidate_id,
        candidate_content_hash=candidate.content_hash,
        split="train",
        requested_tier=EvidenceTier.E1_SIMULATED,
        rows=rows,
        mean_score=mean(scores),
        objective_means={
            key: mean(row.objective_scores[key] for row in rows)
            for key in rows[0].objective_scores
        },
    )


def adapter_contract_diagnostic(project_root: Path) -> dict[str, Any]:
    root = project_root.resolve()
    local = root / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="s5-contract-", dir=local) as temporary:
        run = Path(temporary)
        candidate = build_seed_candidate(
            root,
            "benchmarks/skills/structured-report-builder",
            run_id="s5-contract",
        )
        materialized = materialize_candidate(root, candidate, run / "materialized/package")
        cases = _train_cases(root)
        adapter = GEPASEAdapter(root, root / "benchmarks/manifest-v1.json", run / "evals")
        items = adapter.plan_evaluations(
            cases,
            candidate,
            split="train",
            tier=EvidenceTier.E1_SIMULATED,
            candidate_ref=(run / "materialized/package").relative_to(root).as_posix(),
            host="mock-agent-host",
            model="mock-agent-model",
            seed=42,
        )
        for index, item in enumerate(items):
            planned = (
                TraceStep(sequence=0, action="select_node", target="SKILL.md"),
                TraceStep(
                    sequence=1,
                    action="inspect_fixture",
                    target=item.fixture_ref,
                    tool="read",
                ),
                TraceStep(sequence=2, action="render", target="report.html", tool="python"),
                TraceStep(sequence=3, action="write", target="report.html", tool="write"),
                TraceStep(sequence=4, action="validate", target="report.html", tool="check"),
                TraceStep(sequence=5, action="risk", target="schema mismatch"),
            )
            failure = (
                ProviderFailureKind.ENVIRONMENT_UNAVAILABLE
                if index == len(items) - 1
                else None
            )
            submission = build_submission(
                root,
                item,
                host="mock-agent-host",
                model="mock-agent-model",
                host_task_id=f"mock-task-{index}",
                duration_ms=100,
                artifact_root=None,
                planned_trace=planned,
                observed_trace=(),
                failure_kind=failure,
                failure_detail="injected provider failure" if failure else None,
            )
            with MultiFidelityEvalEngine(root, run / "evals") as engine:
                engine.ingest(submission)
        evaluation = adapter.ingest_evidence(
            candidate,
            cases,
            split="train",
            requested_tier=EvidenceTier.E1_SIMULATED,
        )
        child_one = derive_candidate(
            candidate,
            {
                candidate.components[0].component_id:
                candidate.components[0].content + "\nContract mutation one.\n"
            },
            operator="mock_reflection",
            run_id="s5-contract",
        )
        child_two = derive_candidate(
            child_one,
            {
                child_one.components[1].component_id:
                child_one.components[1].content + "\nContract mutation two.\n"
            },
            operator="mock_reflection",
            run_id="s5-contract",
        )
        candidates = [candidate, child_one, child_two]
        evaluations = [
            _synthetic_evaluation(candidate, (0.4, 0.8, 0.2), suffix="seed"),
            _synthetic_evaluation(child_one, (0.6, 0.7, 0.3), suffix="one"),
            _synthetic_evaluation(child_two, (0.5, 0.9, 0.4), suffix="two"),
        ]
        synchronous = frontier_snapshot(
            candidates,
            evaluations,
            frontier_type="hybrid",
            iteration=2,
            seed=42,
        )
        store_dir = run / "step"
        store_dir.mkdir()
        with CandidateStore(store_dir / "candidates.sqlite3") as store:
            store.add_candidate(candidate, CandidateStatus.SEED)
            store.add_evaluation(evaluations[0])
            store.save_state({"run_id": "contract", "phase": "interrupted"})
            store.write_checkpoint(store_dir)
        with CandidateStore(store_dir / "candidates.sqlite3") as store:
            for child, child_eval in zip(candidates[1:], evaluations[1:], strict=True):
                store.add_candidate(child, CandidateStatus.ACCEPTED)
                store.add_evaluation(child_eval)
            stepwise = frontier_snapshot(
                store.candidates(),
                store.evaluations(),
                frontier_type="hybrid",
                iteration=2,
                seed=42,
            )
        criteria = {
            "work_item_count_correct": len(items) == len(cases) == 3,
            "score_tier_trace_complete": all(
                row.evidence_tier is EvidenceTier.E1_SIMULATED
                and bool(row.planned_trace)
                and not row.observed_trace
                and bool(row.record_ref)
                for row in evaluation.rows
            ),
            "failed_task_preserved": len(evaluation.rows) == 3
            and len(evaluation.failed_tasks) == 1,
            "materialization_valid": all(
                (
                    materialized.file_set_equal,
                    materialized.content_hash_equal,
                    materialized.permission_policy_equal,
                )
            ),
            "sync_step_frontier_equal": synchronous == stepwise,
        }
        return {
            "schema_version": "1.0.0",
            "valid": all(criteria.values()),
            "criteria": criteria,
            "work_items": len(items),
            "evaluation_rows": len(evaluation.rows),
            "failed_rows": len(evaluation.failed_tasks),
            "frontier": synchronous.model_dump(mode="json"),
        }


def asi_audit_diagnostic(project_root: Path) -> dict[str, Any]:
    root = project_root.resolve()
    candidate = build_seed_candidate(
        root,
        "benchmarks/skills/structured-report-builder",
        run_id="s5-asi-audit",
    )
    digest = sha256_bytes(b"audit artifact")
    artifact = ArtifactRef(path="report.html", sha256=digest, size_bytes=14, media_type="text/html")
    planned = (
        TraceStep(sequence=0, action="inspect_fixture", target="fixture", tool="read"),
        TraceStep(sequence=1, action="validate", target="report.html", tool="check"),
    )
    observed = (
        TraceStep(
            sequence=0,
            action="render_report",
            target="report.html",
            tool="python",
            outcome="completed",
        ),
    )
    rows = (
        CandidateEvaluationRow(
            task_id="asi-e1",
            record_id="record-asi-e1",
            record_ref="records/record-asi-e1.json",
            evidence_tier=EvidenceTier.E1_SIMULATED,
            score=0.4,
            objective_scores={"quality": 0.4, "reliability": 0.7},
            output={"failure_detail": "weak validation plan"},
            planned_trace=planned,
            uncertainty=0.3,
            provenance={"origin": "simulation", "provider_id": "agent", "generated_by": "audit"},
            failure_kind="provider_error",
        ),
        CandidateEvaluationRow(
            task_id="asi-e2",
            record_id="record-asi-e2",
            record_ref="records/record-asi-e2.json",
            evidence_tier=EvidenceTier.E2_DELEGATED,
            score=0.0,
            objective_scores={"quality": 0.0, "reliability": 0.8},
            output={"artifact_root": "workspaces/asi-e2"},
            planned_trace=planned,
            observed_trace=observed,
            uncertainty=0.2,
            artifacts=(artifact,),
            provenance={
                "origin": "agent-native",
                "provider_id": "agent",
                "host": "codex",
                "model": "agent-model",
                "host_task_id": "asi-e2-worker",
                "submission_id": "asi-e2-submission",
                "generated_by": "audit",
            },
        ),
        CandidateEvaluationRow(
            task_id="asi-e3",
            record_id="record-asi-e3",
            record_ref="records/record-asi-e3.json",
            evidence_tier=EvidenceTier.E3_EXECUTABLE,
            score=1.0,
            objective_scores={"quality": 1.0, "reliability": 1.0},
            output={"artifact_root": "workspaces/asi-e3"},
            planned_trace=planned,
            observed_trace=observed,
            uncertainty=0.0,
            artifacts=(artifact,),
            assertion_feedback=(
                {"assertion_id": "report-exists", "family": "file_exists", "passed": True},
            ),
            provenance={
                "origin": "assertion",
                "provider_id": "assertion",
                "host": "codex",
                "model": "agent-model",
                "host_task_id": "asi-e3-worker",
                "submission_id": "asi-e3-submission",
                "generated_by": "audit",
            },
        ),
    )
    evaluation = CandidateEvaluation(
        evaluation_id="evaluation-asi-failure-corpus",
        candidate_id=candidate.candidate_id,
        candidate_content_hash=candidate.content_hash,
        split="train",
        requested_tier=EvidenceTier.E2_DELEGATED,
        rows=rows,
        mean_score=mean(row.score for row in rows),
        objective_means={"quality": mean(row.score for row in rows), "reliability": 0.833333},
        failed_tasks=("asi-e1",),
    )
    result = ASIBuilder().build(
        candidate,
        evaluation,
        tuple(item.component_id for item in candidate.components[:2]),
        token_budget=32_000,
        max_examples=3,
    )
    omissions_valid = all(item.section_id and item.reason for item in result.omitted_sections)
    def has_record_ref(row: dict[str, object]) -> bool:
        evidence = row.get("Evidence")
        return isinstance(evidence, dict) and bool(evidence.get("record_ref"))

    criteria = {
        "required_evidence_coverage": result.required_evidence_coverage == 1.0,
        "planned_observed_confusion_zero": result.planned_observed_confusion == 0,
        "token_budget_respected": result.token_estimate <= result.token_budget,
        "omissions_recorded": omissions_valid,
        "record_refs_complete": all(
            has_record_ref(row)
            for rows_value in result.reflective_dataset.values()
            for row in rows_value
        ),
    }
    return {
        "schema_version": "1.0.0",
        "valid": all(criteria.values()),
        "criteria": criteria,
        **result.model_dump(mode="json"),
    }


def checkpoint_resume_diagnostic(project_root: Path) -> dict[str, Any]:
    root = project_root.resolve()
    local = root / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    seed = build_seed_candidate(
        root,
        "benchmarks/skills/structured-report-builder",
        run_id="s5-resume",
    )
    candidates = [seed]
    for index in range(4):
        parent = candidates[-1]
        component = parent.components[index % len(parent.components)]
        candidates.append(
            derive_candidate(
                parent,
                {component.component_id: component.content + f"\nResume mutation {index}.\n"},
                operator="mock_reflection",
                run_id="s5-resume",
            )
        )
    evaluations = [
        _synthetic_evaluation(candidate, (0.4 + 0.05 * index, 0.5, 0.6), suffix=str(index))
        for index, candidate in enumerate(candidates)
    ]

    def populate(path: Path, interrupt_at: int | None) -> dict[str, Any]:
        path.mkdir()
        database = path / "candidates.sqlite3"
        store = CandidateStore(database)
        try:
            for index, (candidate, evaluation) in enumerate(
                zip(candidates, evaluations, strict=True)
            ):
                status = CandidateStatus.SEED if index == 0 else CandidateStatus.ACCEPTED
                store.add_candidate(candidate, status)
                store.add_evaluation(evaluation)
                if index > 0:
                    usage = {
                        "proposals": index,
                        "metric_calls": index * 3,
                        "e1_calls": index * 3,
                        "e2_e3_calls": 0,
                        "reflection_calls": index,
                        "tokens": index * 100,
                        "elapsed_seconds": float(index),
                    }
                    store.add_budget_event(
                        f"proposal-{index}", "proposals", 1.0, usage
                    )
                    store.add_proposal(
                        f"proposal-{index}",
                        candidate.candidate_id,
                        {
                            "proposal_id": f"proposal-{index}",
                            "candidate_id": candidate.candidate_id,
                            "parent_candidate_id": candidate.parent_ids[0],
                        },
                    )
                store.save_state({"run_id": "resume", "last_index": index})
                store.write_checkpoint(path)
                if interrupt_at is not None and index == interrupt_at:
                    store.close()
                    store = CandidateStore(database)
                    store.write_checkpoint(path)
            frontier = frontier_snapshot(
                store.candidates(),
                store.evaluations(),
                frontier_type="hybrid",
                iteration=4,
                seed=42,
            )
            store.save_frontier(frontier)
            store.save_state({"run_id": "resume", "last_index": 4})
            store.write_checkpoint(path)
            return {
                "candidate_ids": [item.candidate_id for item in store.candidates()],
                "parents": [list(item.parent_ids) for item in store.candidates()],
                "evaluations": len(store.evaluations()),
                "proposals": len(store.proposals()),
                "budget": store.latest_budget_usage(),
                "frontier": frontier.model_dump(mode="json"),
                "counts": store.counts(),
            }
        finally:
            try:
                store.close()
            except sqlite3.ProgrammingError:  # type: ignore[name-defined]
                pass

    import sqlite3

    with tempfile.TemporaryDirectory(prefix="s5-resume-", dir=local) as temporary:
        base = Path(temporary)
        uninterrupted = populate(base / "uninterrupted", None)
        resumed = populate(base / "resumed", 2)
    criteria = {
        "candidate_no_duplicates": len(resumed["candidate_ids"])
        == len(set(resumed["candidate_ids"]))
        == 5,
        "parent_lineage_equal": resumed["parents"] == uninterrupted["parents"],
        "score_rows_equal": resumed["evaluations"] == uninterrupted["evaluations"] == 5,
        "proposal_no_duplicates": resumed["proposals"] == uninterrupted["proposals"] == 4,
        "budget_not_reset": resumed["budget"] == uninterrupted["budget"]
        and resumed["budget"]["proposals"] == 4,
        "frontier_equal": resumed["frontier"] == uninterrupted["frontier"],
    }
    return {
        "schema_version": "1.0.0",
        "valid": all(criteria.values()),
        "criteria": criteria,
        "uninterrupted": uninterrupted,
        "resumed": resumed,
    }
