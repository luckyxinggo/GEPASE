"""Recompute R4 package-evolution gates from durable run artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from gepase.evals.engine import MultiFidelityEvalEngine
from gepase.evals.schema import EvidenceTier
from gepase.optimizer.evolution_controller import TrainAdmission
from gepase.optimizer.runtime import EvolutionRunState
from gepase.store.artifacts import atomic_write, canonical_json_bytes


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _gate(gate_id: str, passed: bool, detail: str, evidence: list[str]) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "status": "passed" if passed else "failed",
        "detail": detail,
        "evidence": evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--run-dir", type=Path, default=Path("artifacts/runs/r4-slack-gif-creator-evolution")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/stages/R4/evidence/r4-gates.json")
    )
    args = parser.parse_args()
    repo = args.repo.resolve()
    run = (repo / args.run_dir).resolve()
    output = (repo / args.output).resolve()
    state = EvolutionRunState.model_validate_json(
        (run / "evolution-state.json").read_text(encoding="utf-8")
    )
    candidate_ids = (*state.branch_candidate_ids, *state.merge_candidate_ids)
    admissions = {
        path.stem: TrainAdmission.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted((run / "train-admission").glob("candidate-*.json"))
    }
    admitted = {candidate_id for candidate_id, item in admissions.items() if item.passed}

    reference_audits: list[dict[str, Any]] = [_load(run / "reference-cache-audit.json")]
    train_coverage: dict[str, int] = {}
    validation_coverage: dict[str, int] = {}
    score_verification = True
    isolation_valid = True
    all_contexts: list[str] = []
    e1_records = 0
    e2_success = 0
    typed_failures = 0
    e3_records = 0
    result_jsons: list[str] = []
    comparison_reconciliations = 0
    for candidate_id in candidate_ids:
        for split, coverage in (("train", train_coverage), ("validation", validation_coverage)):
            eval_run = run / "evals" / candidate_id / split
            if not eval_run.is_dir():
                coverage[candidate_id] = 0
                continue
            reference_audits.append(_load(eval_run / "reference-cache-audit.json"))
            with MultiFidelityEvalEngine(repo, eval_run) as engine:
                items = engine.ledger.work_items()
                records = engine.ledger.records()
                coverage[candidate_id] = len(engine.ledger.submissions())
                e1_records += sum(
                    record.evidence_tier is EvidenceTier.E1_SIMULATED for record in records
                )
                for item in items:
                    source = engine.ledger.record_for_work(item.work_id)
                    if source is None:
                        continue
                    if source.failure_kind is None:
                        e2_success += 1
                        if engine.ledger.record_for_work(f"{item.work_id}-assertions") is not None:
                            e3_records += 1
                    else:
                        typed_failures += 1
            verification = _load(eval_run / "score-independent-verification.json")
            score_verification &= bool(verification.get("valid"))
            isolation = _load(eval_run / "isolation-audit.json")
            isolation_valid &= bool(isolation.get("valid"))
            for key in (
                "executor_context_ids",
                "grader_context_ids",
                "comparator_context_ids",
                "analyzer_context_ids",
            ):
                all_contexts.extend(str(item) for item in isolation.get(key, []))
            comparison_reconciliations += len(
                list((eval_run / "comparator-reconciliation").glob("*.json"))
            )
            result_jsons.extend(
                path.relative_to(run).as_posix()
                for path in (eval_run / "workspaces").rglob("result.json")
            )

    gates: list[dict[str, Any]] = []
    root_cache = reference_audits[0]
    cache_ok = bool(
        len(reference_audits) == 8
        and all(item.get("hit") is True for item in reference_audits)
        and all(item.get("partial_match_used") is False for item in reference_audits)
        and all(item.get("stale_evidence_used") is False for item in reference_audits)
        and len(root_cache.get("verified_artifacts", [])) == 429
    )
    gates.append(
        _gate(
            "R4-G01-reference-cache",
            cache_ok,
            f"Verified immutable R3 anchor plus 7 candidate split hits; root artifacts="
            f"{len(root_cache.get('verified_artifacts', []))}.",
            ["reference-evidence-key.json", "reference-cache-audit.json", "evals/"],
        )
    )

    expected_train = set(candidate_ids)
    execution_ok = bool(
        set(train_coverage) == expected_train
        and all(train_coverage[item] == 5 for item in expected_train)
        and all(validation_coverage.get(item) == 3 for item in admitted)
        and all(validation_coverage.get(item, 0) == 0 for item in expected_train - admitted)
        and e2_success + typed_failures == 29
        and e3_records == e2_success
        and typed_failures == 1
        and not result_jsons
        and e1_records == 0
    )
    gates.append(
        _gate(
            "R4-G02-real-candidate-execution",
            execution_ok,
            f"Train coverage={train_coverage}; validation coverage={validation_coverage}; "
            f"successful E2/E3={e2_success}, typed failures={typed_failures}, E1={e1_records}.",
            ["evals/", "scheduler/", "train-admission/"],
        )
    )

    isolation_ok = bool(
        isolation_valid
        and all_contexts
        and len(all_contexts) == len(set(all_contexts))
        and comparison_reconciliations == 9
    )
    gates.append(
        _gate(
            "R4-G03-role-isolation-and-comparison",
            isolation_ok,
            f"Unique role contexts={len(all_contexts)}; duplicate contexts="
            f"{len(all_contexts) - len(set(all_contexts))}; reconciled validation cases="
            f"{comparison_reconciliations}.",
            ["evals/*/*/isolation-audit.json", "evals/*/validation/comparator-reconciliation/"],
        )
    )

    vector_count = sum(
        len(list(path.glob("*.json")))
        for path in (run / "evals").glob("candidate-*/*/task-score-vectors")
    )
    scoring_ok = score_verification and vector_count == 29
    gates.append(
        _gate(
            "R4-G04-score-recomputation",
            scoring_ok,
            f"Independently verified TaskScoreVectors={vector_count}; all split verifiers="
            f"{score_verification}.",
            ["evals/*/*/task-score-vectors/", "evals/*/*/score-independent-verification.json"],
        )
    )

    decision_rows = [_load(path) for path in sorted((run / "gate-decisions").glob("*.json"))]
    final_by_candidate = {
        str(item["candidate_id"]): item
        for item in decision_rows
        if len(item.get("validation_pairs", [])) == 3
    }
    accepted = {
        candidate_id
        for candidate_id, item in final_by_candidate.items()
        if item.get("verdict") == "accepted"
    }
    strict_gate_ok = bool(
        len(admissions) == 4
        and admitted == set(state.evaluated_candidate_ids)
        and set(final_by_candidate) == admitted
        and accepted == set(state.deployable_candidate_ids)
        and accepted
        and all(
            any(
                gate.get("level") == "gate_3_validation"
                and gate.get("outcome") in {"passed", "failed", "inconclusive"}
                for gate in item.get("gates", [])
            )
            for item in final_by_candidate.values()
        )
    )
    gates.append(
        _gate(
            "R4-G05-strict-admission",
            strict_gate_ok,
            f"Train admissions={len(admissions)}, held-out candidates={len(final_by_candidate)}, "
            f"deployable={sorted(accepted)}.",
            ["train-admission/", "gate-decisions/", "deployable-frontier.json"],
        )
    )

    causality = _load(run / "proposal-causality-audit.json")
    pre_eval = [_load(path) for path in sorted((run / "pre-eval-gates").glob("*.json"))]
    graph_patch_ok = bool(
        len(state.branch_candidate_ids) >= 2
        and len(list((run / "patches").glob("*.json"))) == len(candidate_ids)
        and causality.get("valid") is True
        and len(pre_eval) == len(candidate_ids)
        and all(
            item.get("passed") is True
            or (
                item.get("gate_0", {}).get("outcome") == "passed"
                and item.get("gate_1", {}).get("outcome") == "passed"
            )
            for item in pre_eval
        )
    )
    gates.append(
        _gate(
            "R4-G06-graph-patch-causality",
            graph_patch_ok,
            f"Mutation branches={len(state.branch_candidate_ids)}, patches={len(pre_eval)}, "
            f"causality valid={causality.get('valid')}.",
            ["branch-plan.json", "proposal-causality-audit.json", "patches/", "pre-eval-gates/"],
        )
    )

    merge = _load(run / "merge/build-record.json")
    conflicts = _load(run / "merge/conflict-report.json")
    merge_ids = set(state.merge_candidate_ids)
    merge_ok = bool(
        len(merge_ids) == 1
        and merge.get("same_package") is True
        and merge.get("cross_package_parent_count") == 0
        and len(merge.get("parent_candidate_ids", [])) >= 2
        and conflicts.get("unresolved") == 0
        and merge_ids <= set(final_by_candidate)
        and merge.get("gate_0_1_passed") is True
    )
    gates.append(
        _gate(
            "R4-G07-same-package-merge",
            merge_ok,
            f"Merge children={sorted(merge_ids)}, parents={merge.get('parent_candidate_ids')}, "
            f"unresolved conflicts={conflicts.get('unresolved')}.",
            [
                "merge/build-record.json",
                "merge/contribution-map.json",
                "merge/conflict-report.json",
            ],
        )
    )

    runtime = _load(run / "scheduler/runtime-report.json")
    audit = _load(run / "r4-audit.json")
    reflection_counts = audit.get("reflection_count_by_candidate", {})
    runtime_ok = bool(
        state.phase.value == "complete"
        and audit.get("valid") is True
        and runtime.get("reference_cache", {}).get("hits") == 8
        and runtime.get("reference_cache", {}).get("misses") == 0
        and runtime.get("evaluation_calls") == 73
        and runtime.get("proposal_and_reflection_calls") == 4
        and runtime.get("queue_wait_observed") is False
        and all(int(value) <= 1 for value in reflection_counts.values())
    )
    gates.append(
        _gate(
            "R4-G08-runtime-and-resume-audit",
            runtime_ok,
            f"Agent calls={runtime.get('usage', {}).get('agent_calls')}; cache hits="
            f"{runtime.get('reference_cache', {}).get('hits')}; exhausted axes="
            f"{runtime.get('exhausted_axes')}; reflections={reflection_counts}.",
            ["scheduler/runtime-report.json", "r4-audit.json", "evolution-state.json"],
        )
    )

    result = {
        "schema_version": "1.0.0",
        "stage_id": "R4",
        "valid": all(item["status"] == "passed" for item in gates),
        "passed": sum(item["status"] == "passed" for item in gates),
        "failed": sum(item["status"] == "failed" for item in gates),
        "gates": gates,
        "metrics": {
            "candidate_count": len(candidate_ids),
            "mutation_branches": len(state.branch_candidate_ids),
            "merge_children": len(state.merge_candidate_ids),
            "task_score_vectors": vector_count,
            "fresh_evaluation_calls": runtime.get("evaluation_calls"),
            "deployable_candidates": sorted(accepted),
            "typed_failures": typed_failures,
        },
    }
    atomic_write(output, canonical_json_bytes(result))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
