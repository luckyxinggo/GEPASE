from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MethodType

import pytest
from typer.testing import CliRunner

from gepase.cli.app import app
from gepase.evals.eval_plan import (
    FixtureBinding,
    FrozenEvalPlan,
    FunctionalEvalCase,
    FunctionalExpectation,
    RequestedOutput,
    RubricCriterion,
)
from gepase.evals.evidence import UsageRecord
from gepase.evals.functional import (
    FunctionalRole,
    RoleAttemptFailure,
    RoleAttemptKind,
    RoleFailureKind,
    build_role_attempt_terminalization,
)
from gepase.evals.statistics import PairedScore
from gepase.evals.work_items import (
    ExecutionBundle,
    PackageAccessEvent,
    PackageAccessKind,
    canonical_hash,
)
from gepase.mutation.proposer import (
    PatchProposalStore,
    PatchProposalWorkItem,
    build_patch_submission,
)
from gepase.mutation.schema import (
    PackagePatch,
    PatchApplication,
    PatchApplicationStatus,
    application_id_for,
    package_patch_from_proposal,
)
from gepase.optimizer.candidate import (
    PackageCandidate,
    build_seed_candidate,
    derive_candidate,
)
from gepase.optimizer.evolution.branching import MutationBranchState
from gepase.optimizer.evolution.models import MergeEligibility
from gepase.optimizer.evolution_controller import (
    CandidateReflectionSubmission,
    CandidateReflectionWorkItem,
    CandidateTaskReflection,
    R4EvolutionController,
    ReflectionDiagnosis,
    TrainAdmission,
    _SelectorGraphView,
)
from gepase.optimizer.materialize import materialize_candidate
from gepase.optimizer.merge.models import MergeOutcome, MergeOutcomeStatus
from gepase.optimizer.runtime import BudgetUsage, EvolutionPhase, EvolutionRunState
from gepase.optimizer.status import CandidateStatus
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.ir import NodeKind, PackageGraph
from gepase.store.artifacts import ArtifactStore, atomic_write, canonical_json_bytes, sha256_bytes
from gepase.store.candidates import CandidateStore
from gepase.store.evolution_pool import EvolutionPoolEntry, EvolutionPoolStore

ROOT = Path(__file__).resolve().parents[2]
BASE_CONFIG = ROOT / "configs/graph-hardening/slack-gif-creator-gh-e1-evolution.json"
FROZEN_AT = datetime(2026, 7, 30, tzinfo=UTC)


@dataclass(frozen=True)
class _Generation2Fixture:
    controller: R4EvolutionController
    config_path: Path
    seed: PackageCandidate
    parent: PackageCandidate
    plan: FrozenEvalPlan
    plan_path: Path
    package_ref: str


def _functional_case(
    case_id: str,
    split: str,
    *,
    fixture_ref: str,
    fixture_hash: str,
) -> FunctionalEvalCase:
    return FunctionalEvalCase(
        case_id=case_id,
        case_family="generic-text-output",
        prompt=f"Create the deterministic fixture output for {case_id}.",
        fixtures=(
            FixtureBinding(
                ref=fixture_ref,
                sha256=fixture_hash,
                media_type="text/markdown",
                license="CC0-1.0",
                purpose_zh="generation-2 自包含测试输入",
            ),
        ),
        requested_output=RequestedOutput(
            filename=f"{case_id}.txt",
            media_type="text/plain",
            description_zh="确定性文本结果",
        ),
        expected_output_zh="输出一个非空文本文件。",
        expectations=(
            FunctionalExpectation(
                expectation_id=f"{case_id}-present",
                category="technical",
                statement_zh="结果文件存在且非空。",
                evidence_kind="file_presence",
                deterministic=True,
                weight=1.0,
            ),
        ),
        rubric=(
            RubricCriterion(
                criterion_id="quality",
                label_zh="质量",
                description_zh="结果清晰且满足任务。",
                weight=1.0,
            ),
        ),
        required_capabilities=("text_generation",),
        difficulty="easy",
        risk="low",
        leakage_group=case_id,
        split=split,  # type: ignore[arg-type]
    )


def _write_frozen_plan_fixture(
    base: Path,
    *,
    name: str,
    package_id: str,
    package_snapshot_hash: str,
    fixture_ref: str,
    fixture_hash: str,
    train_case_count: int,
    validation_case_count: int,
) -> tuple[Path, FrozenEvalPlan]:
    cases = tuple(
        _functional_case(
            f"train-case-{index + 1}",
            "train",
            fixture_ref=fixture_ref,
            fixture_hash=fixture_hash,
        )
        for index in range(train_case_count)
    ) + tuple(
        _functional_case(
            f"validation-case-{index + 1}",
            "validation",
            fixture_ref=fixture_ref,
            fixture_hash=fixture_hash,
        )
        for index in range(validation_case_count)
    )
    payload = {
        "plan_id": f"fixture-{name}",
        "revision": 1,
        "package_id": package_id,
        "package_snapshot_hash": package_snapshot_hash,
        "source_commit": "a" * 40,
        "draft_hash": "b" * 64,
        "review_id": f"review-{name}",
        "review_hash": "c" * 64,
        "trigger_cases": [],
        "functional_cases": [case.model_dump(mode="json") for case in cases],
        "frozen_at": FROZEN_AT.isoformat(),
    }
    plan = FrozenEvalPlan.model_validate({**payload, "plan_hash": canonical_hash(payload)})
    path = base / f"{name}-frozen-eval-plan.json"
    atomic_write(path, canonical_json_bytes(plan.model_dump(mode="json")))
    return path, plan


def _fixture_selector_graph_view(
    controller: R4EvolutionController,
    *_args: object,
    **_kwargs: object,
) -> _SelectorGraphView:
    graph = PackageGraph.model_validate_json(
        (controller.run_dir / "fixture-selector-graph.json").read_text(encoding="utf-8")
    )
    return _SelectorGraphView(graph=graph, binding=None)


def build_generation2_fixture_controller(
    base: Path,
) -> _Generation2Fixture:
    run_dir = base / "generation2-controller-fixture"
    package_dir = base / "fixture-package"
    skill_path = package_dir / "SKILL.md"
    atomic_write(
        skill_path,
        (
            b"---\nname: fixture-package\ndescription: deterministic fixture\n---\n\n"
            b"# Fixture Package\n\n## Workflow\n\nFollow the requested output contract.\n\n"
            b"## Safety\n\nKeep all output inside the task workspace.\n"
        ),
    )
    package_ref = package_dir.relative_to(ROOT).as_posix()
    seed = build_seed_candidate(ROOT, package_ref, run_id=run_dir.name)
    skill_component = next(item for item in seed.components if item.path == "SKILL.md")
    parent = derive_candidate(
        seed,
        {
            skill_component.component_id: (
                skill_component.content
                + "\n## Refinement\n\nUse explicit, deterministic steps for every task.\n"
            )
        },
        operator="fixture_patch",
        run_id=run_dir.name,
    )
    plan_path, plan = _write_frozen_plan_fixture(
        base,
        name="two-train-one-validation",
        package_id=seed.package_id,
        package_snapshot_hash=seed.snapshot_hash,
        fixture_ref=skill_path.relative_to(ROOT).as_posix(),
        fixture_hash=sha256_bytes(skill_path.read_bytes()),
        train_case_count=2,
        validation_case_count=1,
    )
    config = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    config["run_id"] = run_dir.name
    config["package_ref"] = package_ref
    config["frozen_plan_ref"] = plan_path.relative_to(ROOT).as_posix()
    config["reference_run_ref"] = base.relative_to(ROOT).as_posix()
    config["package_graph_ref"] = (
        (run_dir / "fixture-selector-graph.json").relative_to(ROOT).as_posix()
    )
    config.pop("lifecycle_policy", None)
    config.pop("active_session_budget_policy", None)
    config.pop("conditional_merge_policy", None)
    config_path = base / "generation2-config.json"
    atomic_write(config_path, canonical_json_bytes(config))
    controller = R4EvolutionController(ROOT, run_dir, config_path)

    workspace = run_dir / f"candidate-workspaces/{parent.candidate_id}/workspace"
    materialize_candidate(ROOT, parent, workspace)
    graph = PackageAnalyzer().analyze(workspace).graph
    controller._write("fixture-selector-graph.json", graph)
    controller.build_selector_graph_view = MethodType(  # type: ignore[method-assign]
        _fixture_selector_graph_view,
        controller,
    )
    with CandidateStore(run_dir / "candidates.sqlite3") as store:
        store.add_candidate(seed, CandidateStatus.SEED)
        store.add_candidate(parent, CandidateStatus.ACCEPTED)
    state = EvolutionRunState(
        run_id=run_dir.name,
        config_hash=controller.config_hash,
        phase=EvolutionPhase.REFLECTION,
        seed_candidate_id=seed.candidate_id,
        branch_candidate_ids=(parent.candidate_id,),
        evaluated_candidate_ids=(parent.candidate_id,),
        reflected_candidate_ids=(parent.candidate_id,),
        budget_usage=BudgetUsage(proposals=2, candidates=2),
        updated_at=FROZEN_AT,
    )
    controller._write("evolution-state.json", state)

    graph_ref = (run_dir / "fixture-selector-graph.json").relative_to(ROOT).as_posix()
    workspace_ref = workspace.relative_to(ROOT).as_posix()
    application = PatchApplication(
        application_id="application-generation2-fixture",
        patch_id="patch-generation2-fixture",
        parent_candidate_id=seed.candidate_id,
        parent_content_hash=seed.content_hash,
        status=PatchApplicationStatus.APPLIED,
        candidate_id=parent.candidate_id,
        candidate_content_hash=parent.content_hash,
        workspace_ref=workspace_ref,
        original_workspace_hash_unchanged=True,
    )
    controller._write(f"candidates/{parent.candidate_id}/candidate.json", parent)
    controller._write(f"candidates/{parent.candidate_id}/graph.json", graph)

    seed_graph = PackageAnalyzer().analyze(package_dir).graph
    seed_target = next(
        node
        for node in seed_graph.nodes
        if node.mutable and node.kind in {NodeKind.INSTRUCTION, NodeKind.SECTION}
    )
    fixture_patch = package_patch_from_proposal(
        {
            "proposal_work_id": "proposal-work-generation2-parent-fixture",
            "base_candidate_id": seed.candidate_id,
            "base_snapshot_hash": seed.snapshot_hash,
            "base_content_hash": seed.content_hash,
            "selector": "fixture",
            "selected_node_ids": [seed_target.node_id],
            "operations": [
                {
                    "operation_id": "op-generation2-parent-fixture",
                    "op": "replace_markdown_block",
                    "target_node_id": seed_target.node_id,
                    "path": seed_target.path,
                    "precondition_hash": seed_target.content_hash,
                    "replacement": (
                        "## Workflow\n\nUse explicit, deterministic steps for every task."
                    ),
                    "evidence_refs": ["artifacts/local/fixture/train-evidence.json"],
                    "expected_benefit": "Create the deterministic generation-1 parent.",
                    "regression_risk": "low",
                    "rationale": "Exercise Candidate bundle sealing.",
                }
            ],
            "edit_budget": controller.config.patch_budget.model_dump(mode="json"),
            "evidence_refs": ["artifacts/local/fixture/train-evidence.json"],
            "summary": "Create the deterministic generation-1 parent fixture.",
        }
    )
    application = application.model_copy(
        update={
            "application_id": application_id_for(
                fixture_patch.patch_id,
                seed.candidate_id,
            ),
            "patch_id": fixture_patch.patch_id,
        }
    )
    controller._write(f"candidates/{parent.candidate_id}/application.json", application)
    controller._write(f"candidates/{parent.candidate_id}/patch.json", fixture_patch)

    branch = MutationBranchState(
        branch_id="branch-generation2-fixture",
        package_id=parent.package_id,
        source_snapshot_hash=parent.snapshot_hash,
        lineage_root_candidate_id=seed.candidate_id,
        branch_root_candidate_id=parent.candidate_id,
        failure_cluster_id="failure-cluster-fixture",
        variant_index=0,
        head_candidate_id=parent.candidate_id,
        generation=1,
        operator_history=("fixture_patch",),
        candidate_chain=(seed.candidate_id, parent.candidate_id),
    )
    controller._write(f"branches/{branch.branch_id}.json", branch)

    train_ids = tuple(
        sorted(case.case_id for case in plan.functional_cases if case.split == "train")
    )
    eval_dir = run_dir / f"evals/{parent.candidate_id}/train"
    ArtifactStore(eval_dir).write_json(
        "run-metadata.json",
        {
            "schema_version": "1.0.0",
            "mode": "frozen-candidate",
            "candidate_id": parent.candidate_id,
            "candidate_content_hash": parent.content_hash,
            "split": "train",
            "selected_case_ids": list(train_ids),
            "frozen_plan_ref": plan_path.relative_to(ROOT).as_posix(),
            "frozen_plan_hash": plan.plan_hash,
            "package_graph_ref": graph_ref,
            "reference_key_hash": "d" * 64,
        },
    )
    pairs = tuple(
        PairedScore(
            task_id=task_id,
            category="generic",
            risk_level="low",
            parent_score=0.5,
            candidate_score=0.6,
            evidence_tier="E3",
            minimum_acceptance_tier="E2",
            parent_record_id=f"artifacts/local/reference/{task_id}.json",
            candidate_record_id=f"artifacts/local/candidate/{task_id}.json",
        )
        for task_id in train_ids
    )
    admission = TrainAdmission(
        candidate_id=parent.candidate_id,
        passed=True,
        gate={"level": 2, "outcome": "passed"},
        paired_scores=pairs,
        strict_task_wins=train_ids,
        protected_floor_satisfied=True,
        validation_required=True,
    )
    controller._write(f"train-admission/{parent.candidate_id}.json", admission)

    target = next(
        node
        for node in graph.nodes
        if node.mutable and node.kind in {NodeKind.INSTRUCTION, NodeKind.SECTION, NodeKind.FILE}
    )
    reflection_work = CandidateReflectionWorkItem(
        work_id="reflection-work-generation2-fixture",
        candidate_id=parent.candidate_id,
        parent_candidate_ids=parent.parent_ids,
        patch_ref=(run_dir / f"candidates/{parent.candidate_id}/patch.json")
        .relative_to(ROOT)
        .as_posix(),
        graph_ref=graph_ref,
        graph_diff={"changed": True},
        task_feedback=tuple(
            CandidateTaskReflection(
                task_id=task_id,
                paired_delta=0.1,
                evidence_refs=(f"artifacts/local/train/{task_id}.json",),
                failed_expectation_ids=(f"{task_id}-quality",),
                grader_feedback_zh="可以进一步收紧指令。",
            )
            for task_id in train_ids
        ),
        node_hints=(
            {
                "node_id": target.node_id,
                "path": target.path,
                "kind": target.kind.value,
            },
        ),
    )
    reflection = CandidateReflectionSubmission(
        submission_id="reflection-submission-generation2-fixture",
        work_id=reflection_work.work_id,
        host="fixture-host",
        model="fixture-model",
        host_task_id="fixture-host-task",
        context_id="fixture-context",
        duration_ms=1,
        token_estimate=1,
        diagnoses=tuple(
            ReflectionDiagnosis(
                task_id=task_id,
                diagnosis_zh="当前指令仍可更明确。",
                evidence_refs=(f"artifacts/local/train/{task_id}.json",),
                target_node_ids=(target.node_id,),
                recommendation_zh="增加确定性执行约束。",
            )
            for task_id in train_ids
        ),
        summary_zh="仅使用 train 反馈形成 refinement。",
    )
    controller._write(f"reflection-work-items/{reflection_work.work_id}.json", reflection_work)
    controller._write(f"reflection-submissions/{reflection_work.work_id}.json", reflection)

    entry = EvolutionPoolEntry(
        candidate_id=parent.candidate_id,
        parent_candidate_id=seed.candidate_id,
        patch_id=application.patch_id,
        package_id=parent.package_id,
        source_package_ref=parent.source_package_ref,
        source_snapshot_hash=parent.snapshot_hash,
        lineage_root_candidate_id=seed.candidate_id,
        branch_id=branch.branch_id,
        branch_root_candidate_id=parent.candidate_id,
        failure_cluster_ids=(branch.failure_cluster_id,),
        ancestor_candidate_ids=(seed.candidate_id,),
        candidate_content_hash=parent.content_hash,
        train_evidence_refs=tuple(f"artifacts/local/train/{task_id}.json" for task_id in train_ids),
        exclusive_task_keys=(train_ids[0],),
        exclusive_component_ids=(target.node_id,),
        train_mean_delta=0.1,
        train_floor_satisfied=True,
        gate_0_1_passed=True,
        merge_eligibility=MergeEligibility.ELIGIBLE,
    )
    with EvolutionPoolStore(run_dir / "evolution-pool.sqlite3") as pool:
        pool.add(entry)
        pool.snapshot(run_dir / "evolution-pool.json")
    controller._write(
        "gepa-state-snapshot.json",
        {
            "schema_version": "1.0.0",
            "pareto_selected_candidate_id": parent.candidate_id,
            "current_best_candidate_id": parent.candidate_id,
        },
    )
    return _Generation2Fixture(
        controller=controller,
        config_path=config_path,
        seed=seed,
        parent=parent,
        plan=plan,
        plan_path=plan_path,
        package_ref=package_ref,
    )


def build_generation2_active_selector_fixture(
    base: Path,
    *,
    seal_candidate: bool = True,
) -> _Generation2Fixture:
    """Build an active-run fixture that exercises the real graph overlay path."""

    fixture = build_generation2_fixture_controller(base)
    controller = fixture.controller
    del controller.__dict__["build_selector_graph_view"]
    graph_ref = (
        controller.run_dir / f"candidates/{fixture.parent.candidate_id}/graph.json"
    ).relative_to(ROOT).as_posix()
    eval_dir = controller.run_dir / f"evals/{fixture.parent.candidate_id}/train"
    metadata = json.loads((eval_dir / "run-metadata.json").read_text(encoding="utf-8"))
    metadata.update(
        {
            "package_graph_ref": graph_ref,
            "host": controller.config.host,
            "model": controller.config.model,
            "seed": controller.config.seed,
            "timeout_seconds": controller.config.timeout_seconds,
        }
    )
    store = ArtifactStore(eval_dir)
    store.write_json("run-metadata.json", metadata)
    graph = PackageGraph.model_validate_json(
        (
            controller.run_dir / f"candidates/{fixture.parent.candidate_id}/graph.json"
        ).read_text(encoding="utf-8")
    )
    file_node = next(node for node in graph.nodes if node.kind is NodeKind.FILE)
    workspace_ref = fixture.controller._candidate_application(
        fixture.parent.candidate_id
    ).workspace_ref
    assert workspace_ref is not None
    store.write_json(
        "executor-work-items/work-active-selector.json",
        {
            "work_id": "work-active-selector",
            "task_id": metadata["selected_case_ids"][0],
            "skill_ref": workspace_ref,
            "package_graph_ref": graph_ref,
            "package_node_map": {file_node.path: file_node.node_id},
        },
    )
    store.write_json(
        "execution-submissions/work-active-selector.json",
        ExecutionBundle(
            submission_id="submission-active-selector",
            work_id="work-active-selector",
            provider_id=controller.config.provider_snapshot,
            host=controller.config.host,
            model=controller.config.model,
            host_task_id="host-task-active-selector",
            context_id="context-active-selector",
            package_access=(
                PackageAccessEvent(
                    sequence=0,
                    kind=PackageAccessKind.READ,
                    path=file_node.path,
                    node_id=file_node.node_id,
                    bytes_loaded=1,
                    tokens_loaded=1,
                ),
            ),
            usage=UsageRecord(duration_ms=1),
            started_at=FROZEN_AT,
            finished_at=FROZEN_AT,
        ).model_dump(mode="json"),
    )
    store.write_json(
        "package-access/work-active-selector.json",
        {
            "schema_version": "1.0.0",
            "work_id": "work-active-selector",
            "variant": "candidate",
            "valid": True,
        },
    )
    store.verify_complete()
    if seal_candidate:
        controller.seal_candidate_bundle(fixture.parent.candidate_id)
    return fixture


def _generation2_raw_proposal(work: PatchProposalWorkItem) -> dict[str, object]:
    operations = []
    for index, target in enumerate(work.targets, 1):
        operation = (
            work.allowed_operations[0]
            if len(work.allowed_operations) == 1
            else work.allowed_operations[index - 1]
        )
        operations.append(
            {
                "operation_id": f"op-generation2-{index}",
                "op": operation.value,
                "target_node_id": target.node_id,
                "path": target.path,
                "precondition_hash": target.content_hash,
                "replacement": (
                    target.content.rstrip()
                    + "\n\nGeneration-2 keeps this parent-bound refinement deterministic.\n"
                ),
                "evidence_refs": list(work.evidence_refs),
                "expected_benefit": "Exercise the parent-bound deterministic refinement path.",
                "regression_risk": "low",
                "rationale": "The replacement is limited to the exported causal target.",
            }
        )
    return {
        "operations": operations,
        "summary": "Apply the deterministic generation-2 test refinement.",
    }


def _plan_and_ingest_generation2(
    fixture: _Generation2Fixture,
    *,
    patch_override: PackagePatch | None = None,
) -> tuple[PatchProposalWorkItem, str]:
    controller = fixture.controller
    plan = controller.plan_generation2_refinement(fixture.parent.candidate_id)
    assert plan.proposal_work_id is not None
    with PatchProposalStore(controller.run_dir / "proposal-work.sqlite3") as proposals:
        work = proposals.get_work(plan.proposal_work_id)
    submission = build_patch_submission(
        work,
        _generation2_raw_proposal(work),
        host="fixture-host",
        model="fixture-model",
        host_task_id="fixture-generation2-proposer",
        duration_ms=1,
        token_estimate=1,
    )
    if patch_override is not None:
        submission = submission.model_copy(update={"patch": patch_override})
    controller.ingest_proposal(submission)
    return work, submission.work_id


def test_generation2_plan_is_parent_bound_train_only_bounded_and_idempotent() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="generation2-plan-", dir=local) as temporary:
        fixture = build_generation2_fixture_controller(Path(temporary))
        controller = fixture.controller
        before_state = controller.state()
        with CandidateStore(controller.run_dir / "candidates.sqlite3") as store:
            before_candidates = store.candidates()
        first = controller.plan_generation2_refinement(fixture.parent.candidate_id)
        second = controller.plan_generation2_refinement(fixture.parent.candidate_id)
        after_state = controller.state()
        with CandidateStore(controller.run_dir / "candidates.sqlite3") as store:
            after_candidates = store.candidates()

        assert first == second
        assert first.status == "planned"
        assert first.parent_generation == 1
        assert first.planned_generation == 2
        assert first.branch_id
        assert first.proposal_work_id
        assert not first.proposal_intent_charged
        assert not first.candidate_materialized
        assert not first.held_out_evidence_read
        assert before_state.budget_usage == after_state.budget_usage
        assert before_candidates == after_candidates
        work = json.loads(
            (controller.run_dir / f"proposal-work-items/{first.proposal_work_id}.json").read_text(
                encoding="utf-8"
            )
        )
        generation = work["actionable_side_information"]["generation_contract"]
        assert generation["parent_candidate_id"] == fixture.parent.candidate_id
        assert generation["parent_generation"] == 1
        assert generation["planned_generation"] == 2
        assert not generation["held_out_evidence_read"]
        assert not generation["sibling_evidence_read"]
        assert not generation["merge_path_used"]
        causal_targets = work["actionable_side_information"]["causal_targets"]
        assert {item["node_id"] for item in causal_targets} == {
            item["node_id"] for item in work["targets"]
        }
        assert all(item["failure_evidence_ids"] for item in causal_targets)
        assert all(item["causal_path_node_ids"] for item in causal_targets)
        assert all(item["expected_affected_metrics"] for item in causal_targets)
        assert not any("/validation/" in ref for ref in work["evidence_refs"])
        assert first.train_feedback_ref is not None
        projection = json.loads(
            (controller.project_root / first.train_feedback_ref).read_text(encoding="utf-8")
        )
        expected_train = {
            case.case_id for case in fixture.plan.functional_cases if case.split == "train"
        }
        assert {item["task_id"] for item in projection["task_feedback"]} == expected_train
        assert {item["task_id"] for item in projection["diagnoses"]} == expected_train
        assert not any(
            "/validation/" in ref
            for item in projection["diagnoses"]
            for ref in item["evidence_refs"]
        )


def test_active_run_candidate_seal_enables_real_generation2_graph_overlay() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="generation2-active-seal-", dir=local) as temporary:
        fixture = build_generation2_active_selector_fixture(Path(temporary))
        controller = fixture.controller
        assert "build_selector_graph_view" not in controller.__dict__
        before = controller.state().budget_usage

        outcome = controller.plan_generation2_refinement(fixture.parent.candidate_id)

        assert outcome.status == "planned"
        assert outcome.selector_graph_ref is not None
        assert controller.state().budget_usage == before
        candidate_dir = controller.run_dir / f"candidates/{fixture.parent.candidate_id}"
        verification = ArtifactStore(candidate_dir).verify_complete()
        assert verification.checked == 5
        assert controller.state().phase is not EvolutionPhase.COMPLETE


def test_active_run_generation2_rejects_missing_or_tampered_candidate_seal() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    for failure in ("missing", "graph_tamper", "unindexed"):
        with tempfile.TemporaryDirectory(
            prefix=f"generation2-active-{failure}-",
            dir=local,
        ) as temporary:
            fixture = build_generation2_active_selector_fixture(
                Path(temporary),
                seal_candidate=failure != "missing",
            )
            candidate_dir = (
                fixture.controller.run_dir / f"candidates/{fixture.parent.candidate_id}"
            )
            if failure == "graph_tamper":
                atomic_write(candidate_dir / "graph.json", b"{}\n")
            elif failure == "unindexed":
                atomic_write(candidate_dir / "unexpected.json", b"{}\n")
            with pytest.raises(ValueError, match="sealed or hash-matched"):
                fixture.controller.plan_generation2_refinement(fixture.parent.candidate_id)


@pytest.mark.parametrize("failure", ("wrong_graph_snapshot", "wrong_application_content"))
def test_candidate_seal_rejects_wrong_snapshot_or_content(failure: str) -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"candidate-seal-{failure}-", dir=local) as temporary:
        fixture = build_generation2_fixture_controller(Path(temporary))
        candidate_dir = (
            fixture.controller.run_dir / f"candidates/{fixture.parent.candidate_id}"
        )
        if failure == "wrong_graph_snapshot":
            graph = PackageGraph.model_validate_json(
                (candidate_dir / "graph.json").read_text(encoding="utf-8")
            )
            atomic_write(
                candidate_dir / "graph.json",
                canonical_json_bytes(
                    graph.model_copy(update={"snapshot_hash": "0" * 64}).model_dump(
                        mode="json"
                    )
                ),
            )
        else:
            application = PatchApplication.model_validate_json(
                (candidate_dir / "application.json").read_text(encoding="utf-8")
            )
            atomic_write(
                candidate_dir / "application.json",
                canonical_json_bytes(
                    application.model_copy(
                        update={"candidate_content_hash": "0" * 64}
                    ).model_dump(mode="json")
                ),
            )
        with pytest.raises(ValueError, match="identity mismatch"):
            fixture.controller.seal_candidate_bundle(fixture.parent.candidate_id)


def test_terminal_r4_seal_remains_compatible_with_candidate_substores() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="generation2-terminal-seal-", dir=local) as temporary:
        fixture = build_generation2_active_selector_fixture(Path(temporary))
        state = fixture.controller.state().model_copy(update={"phase": EvolutionPhase.COMPLETE})
        fixture.controller._write("evolution-state.json", state)

        result = fixture.controller.seal()

        assert result["valid"]
        assert result["unindexed_files"] == 0
        assert ArtifactStore(
            fixture.controller.run_dir / f"candidates/{fixture.parent.candidate_id}"
        ).verify_complete().valid


def test_generation2_no_parent_and_budget_caps_are_typed() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="generation2-terminal-", dir=local) as temporary:
        fixture = build_generation2_fixture_controller(Path(temporary))
        controller = fixture.controller
        missing = controller.plan_generation2_refinement("candidate-missing")
        assert missing.status == "no_eligible_parent"

        state = controller.state().model_copy(
            update={
                "budget_usage": controller.state().budget_usage.model_copy(
                    update={"proposals": controller.config.runtime_budget.max_proposals}
                )
            }
        )
        controller._write("evolution-state.json", state)
        exhausted = controller.plan_generation2_refinement()
        assert exhausted.status == "proposal_budget_exhausted"


def test_generation2_rejects_cross_package_or_snapshot_pool_parent() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    invalid_bindings = (
        {"package_id": "pkg-cross-package"},
        {"source_snapshot_hash": "0" * 64},
    )
    for invalid_binding in invalid_bindings:
        with tempfile.TemporaryDirectory(
            prefix="generation2-cross-binding-", dir=local
        ) as temporary:
            fixture = build_generation2_fixture_controller(Path(temporary))
            controller = fixture.controller
            with EvolutionPoolStore(controller.run_dir / "evolution-pool.sqlite3") as pool:
                entry = pool.all()[0]
            database = controller.run_dir / "evolution-pool.sqlite3"
            database.unlink()
            with EvolutionPoolStore(database) as pool:
                pool.add(entry.model_copy(update=invalid_binding))
            outcome = controller.plan_generation2_refinement(fixture.parent.candidate_id)
            assert outcome.status == "no_eligible_parent"


def test_generation2_fixture_uses_complete_train_admission() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="generation2-admission-", dir=local) as temporary:
        fixture = build_generation2_fixture_controller(Path(temporary))
        admission = TrainAdmission.model_validate_json(
            (
                fixture.controller.run_dir / f"train-admission/{fixture.parent.candidate_id}.json"
            ).read_text(encoding="utf-8")
        )
        expected = {case.case_id for case in fixture.plan.functional_cases if case.split == "train"}
        assert admission.passed
        assert {item.task_id for item in admission.paired_scores} == expected


def test_validation_incomplete_terminalization_is_durable_and_complete_is_idempotent() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="validation-incomplete-complete-", dir=local) as tmp:
        fixture = build_generation2_fixture_controller(Path(tmp))
        controller = fixture.controller
        controller._write("resolved-config.json", controller.config)
        candidate_id = fixture.parent.candidate_id
        validation_ids = tuple(
            sorted(
                case.case_id
                for case in fixture.plan.functional_cases
                if case.split == "validation"
            )
        )
        assert len(validation_ids) == 1
        work_id = "grader-work-validation-incomplete"
        terminalization = build_role_attempt_terminalization(
            run_id=controller.config.run_id,
            task_id=validation_ids[0],
            work_id=work_id,
            role=FunctionalRole.INDEPENDENT_GRADER,
            attempts=(
                RoleAttemptFailure(
                    attempt_kind=RoleAttemptKind.INITIAL,
                    host_attempt_accounting_id="host-attempt-validation-incomplete",
                    host_task_id="host-task-validation-incomplete",
                    context_id="context-validation-incomplete",
                    evidence_sha256="e" * 64,
                    failure_kind=RoleFailureKind.TIMEOUT,
                    source_refs=("artifacts/local/validation-incomplete.json",),
                ),
            ),
            allowed_repair_attempts=0,
            terminalized_at=FROZEN_AT,
        )
        validation_run = controller.run_dir / f"evals/{candidate_id}/validation"
        store = ArtifactStore(validation_run)
        store.write_json(
            "run-metadata.json",
            {
                "mode": "frozen-candidate",
                "split": "validation",
                "candidate_id": candidate_id,
                "selected_case_ids": list(validation_ids),
                "frozen_plan_ref": fixture.plan_path.relative_to(ROOT).as_posix(),
                "frozen_plan_hash": fixture.plan.plan_hash,
            },
        )
        store.write_json(
            "candidate-run-summary.json",
            {
                "candidate_id": candidate_id,
                "split": "validation",
                "status": "evidence_incomplete",
                "evidence_complete": False,
                "gate_eligible": False,
                "pair_summaries": [],
                "incomplete_cases": [
                    {
                        "task_id": validation_ids[0],
                        "role": "independent_grader",
                        "work_id": work_id,
                        "terminalization_id": terminalization.terminalization_id,
                        "disposition": "evidence_incomplete",
                    }
                ],
            },
        )
        store.write_json(
            f"role-terminalizations/independent_grader/{work_id}.json",
            terminalization.model_dump(mode="json"),
        )
        controller._write(
            "merge/outcome.json",
            MergeOutcome(
                status=MergeOutcomeStatus.NO_ELIGIBLE_PARENT_SET,
                considered_parent_candidate_ids=(candidate_id,),
                considered_parent_set_count=0,
                eligible_parent_set_count=0,
                rejected_parent_set_count=0,
                rejection_reason_counts={"insufficient_parents": 1},
                cross_package_pair_count=0,
                enumeration_ref="artifacts/local/fixture/merge-enumeration.json",
            ),
        )
        controller._write(
            "evolution-state.json",
            controller.state().model_copy(update={"evaluated_candidate_ids": ()}),
        )

        first_resolution = controller.finalize_validation(candidate_id)
        second_resolution = controller.finalize_validation(candidate_id)
        assert first_resolution == second_resolution
        assert controller.state().evaluated_candidate_ids == ()
        assert controller.state().validation_incomplete_candidate_ids == (candidate_id,)

        original_audit = controller.audit

        def fixture_audit(self: R4EvolutionController) -> dict[str, object]:
            result = {"schema_version": "1.0.0", "valid": True}
            self._write("r4-audit.json", result)
            return result

        controller.audit = MethodType(fixture_audit, controller)  # type: ignore[method-assign]
        first_complete = controller.complete()
        controller.audit = original_audit  # type: ignore[method-assign]
        second_complete = controller.complete()

        assert first_complete["phase"] == "complete"
        assert second_complete["idempotent"]
        assert second_complete["validation_incomplete_candidate_ids"] == [candidate_id]
        assert controller.state().deployable_candidate_ids == ()


def test_generation2_accepts_non_five_case_frozen_train_split() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="generation2-two-case-", dir=local) as temporary:
        fixture = build_generation2_fixture_controller(Path(temporary))
        controller = fixture.controller
        outcome = controller.plan_generation2_refinement(fixture.parent.candidate_id)
        assert outcome.train_feedback_ref is not None
        projection = json.loads(
            (controller.project_root / outcome.train_feedback_ref).read_text(encoding="utf-8")
        )
        metadata = json.loads(
            (
                controller.run_dir / f"evals/{fixture.parent.candidate_id}/train/run-metadata.json"
            ).read_text(encoding="utf-8")
        )

        assert outcome.status == "planned"
        assert sum(case.split == "train" for case in fixture.plan.functional_cases) == 2
        assert sum(case.split == "validation" for case in fixture.plan.functional_cases) == 1
        assert len(metadata["selected_case_ids"]) == 2
        assert {item["task_id"] for item in projection["task_feedback"]} == set(
            metadata["selected_case_ids"]
        )


def test_generation2_complete_parent_bound_mainline_is_idempotent() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="generation2-mainline-", dir=local) as temporary:
        fixture = build_generation2_fixture_controller(Path(temporary))
        controller = fixture.controller
        before_state = controller.state()
        work, work_id = _plan_and_ingest_generation2(fixture)

        first = controller.apply_generation2_refinement(work_id)
        second = controller.apply_generation2_refinement(work_id)

        assert first == second
        assert first["status"] == "materialized"
        assert first["parent_candidate_id"] == fixture.parent.candidate_id
        assert first["generation"] == 2
        assert first["gate_0"] == "passed"
        assert first["gate_1"] == "passed"
        assert first["parent_bound"]
        assert not first["held_out_evidence_read"]
        child = controller._candidate(first["candidate_id"])
        assert child.parent_ids == (fixture.parent.candidate_id,)
        assert child.generation == 2
        branch = MutationBranchState.model_validate_json(
            (controller.run_dir / f"branches/{first['branch_id']}.json").read_text(
                encoding="utf-8"
            )
        )
        assert branch.candidate_chain == (
            fixture.seed.candidate_id,
            fixture.parent.candidate_id,
            child.candidate_id,
        )
        assert branch.head_candidate_id == child.candidate_id
        assert branch.generation == 2
        assert len(branch.operator_history) == 2
        assert work.parent_content_hash == fixture.parent.content_hash
        assert "Generation-2 keeps" in next(
            component.content
            for component in child.components
            if component.path == work.targets[0].path
        )
        state = controller.state()
        assert state.branch_candidate_ids[-1] == child.candidate_id
        assert state.budget_usage.candidates == before_state.budget_usage.candidates + 1
        with CandidateStore(controller.run_dir / "candidates.sqlite3") as candidates:
            assert candidates.candidate(child.candidate_id) == child
            assert len(candidates.candidates()) == 3


def test_generation2_apply_cli_uses_the_same_parent_bound_controller_path() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="generation2-apply-cli-", dir=local) as temporary:
        fixture = build_generation2_fixture_controller(Path(temporary))
        _work, work_id = _plan_and_ingest_generation2(fixture)
        args = [
            "optimizer",
            "r4-apply-generation2",
            "--run-dir",
            fixture.controller.run_dir.as_posix(),
            "--config",
            fixture.config_path.as_posix(),
            "--work-id",
            work_id,
        ]

        first = CliRunner().invoke(app, args)
        second = CliRunner().invoke(app, args)

        assert first.exit_code == 0, first.output
        assert second.exit_code == 0, second.output
        assert json.loads(first.output) == json.loads(second.output)
        assert json.loads(first.output)["status"] == "materialized"


@pytest.mark.parametrize(
    ("binding", "match"),
    (
        ("wrong_parent", "another parent"),
        ("stale_hash", "stale"),
    ),
)
def test_generation2_apply_rejects_wrong_parent_and_stale_hash(
    binding: str,
    match: str,
) -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"generation2-{binding}-", dir=local) as temporary:
        fixture = build_generation2_fixture_controller(Path(temporary))
        plan = fixture.controller.plan_generation2_refinement(fixture.parent.candidate_id)
        assert plan.proposal_work_id is not None
        with PatchProposalStore(fixture.controller.run_dir / "proposal-work.sqlite3") as store:
            work = store.get_work(plan.proposal_work_id)
        original = build_patch_submission(
            work,
            _generation2_raw_proposal(work),
            host="fixture-host",
            model="fixture-model",
            host_task_id=f"fixture-{binding}",
            duration_ms=1,
            token_estimate=1,
        )
        assert original.patch is not None
        payload = original.patch.identity_payload()
        if binding == "wrong_parent":
            payload.update(
                {
                    "base_candidate_id": fixture.seed.candidate_id,
                    "base_content_hash": fixture.seed.content_hash,
                }
            )
        else:
            payload["base_content_hash"] = "0" * 64
        invalid_patch = package_patch_from_proposal(payload)
        fixture.controller.ingest_proposal(original.model_copy(update={"patch": invalid_patch}))

        with pytest.raises(ValueError, match=match):
            fixture.controller.apply_generation2_refinement(work.work_id)
        assert not (fixture.controller.run_dir / "generation2-applications").exists()


def test_generation2_apply_rejects_cross_package_or_snapshot_branch() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    invalid_bindings = (
        {"package_id": "pkg-cross-package"},
        {"source_snapshot_hash": "0" * 64},
    )
    for invalid_binding in invalid_bindings:
        with tempfile.TemporaryDirectory(
            prefix="generation2-apply-cross-binding-",
            dir=local,
        ) as temporary:
            fixture = build_generation2_fixture_controller(Path(temporary))
            _work, work_id = _plan_and_ingest_generation2(fixture)
            branch_path = fixture.controller.run_dir / "branches/branch-generation2-fixture.json"
            branch = MutationBranchState.model_validate_json(
                branch_path.read_text(encoding="utf-8")
            )
            fixture.controller._write(
                "branches/branch-generation2-fixture.json",
                branch.model_copy(update=invalid_binding),
            )

            with pytest.raises(ValueError, match="cross-package, or cross-snapshot"):
                fixture.controller.apply_generation2_refinement(work_id)
            assert not (fixture.controller.run_dir / "generation2-applications").exists()


def test_generation2_apply_rejects_validation_leakage() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="generation2-validation-leak-", dir=local) as temporary:
        fixture = build_generation2_fixture_controller(Path(temporary))
        plan = fixture.controller.plan_generation2_refinement(fixture.parent.candidate_id)
        assert plan.proposal_work_id is not None
        with PatchProposalStore(fixture.controller.run_dir / "proposal-work.sqlite3") as store:
            work = store.get_work(plan.proposal_work_id)
        submission = build_patch_submission(
            work,
            _generation2_raw_proposal(work),
            host="fixture-host",
            model="fixture-model",
            host_task_id="fixture-validation-leak",
            duration_ms=1,
            token_estimate=1,
        )
        assert submission.patch is not None
        payload = submission.patch.identity_payload()
        validation_ref = "artifacts/local/evals/candidate/validation/task.json"
        payload["evidence_refs"] = [validation_ref]
        for operation in payload["operations"]:  # type: ignore[union-attr]
            operation["evidence_refs"] = [validation_ref]
        leaked_patch = package_patch_from_proposal(payload)
        fixture.controller.ingest_proposal(submission.model_copy(update={"patch": leaked_patch}))

        with pytest.raises(ValueError, match="held-out evidence"):
            fixture.controller.apply_generation2_refinement(work.work_id)
        assert not (fixture.controller.run_dir / "generation2-applications").exists()


def test_generation2_apply_rechecks_candidate_cap() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="generation2-candidate-cap-", dir=local) as temporary:
        fixture = build_generation2_fixture_controller(Path(temporary))
        _work, work_id = _plan_and_ingest_generation2(fixture)
        state = fixture.controller.state()
        fixture.controller._write(
            "evolution-state.json",
            state.model_copy(
                update={
                    "budget_usage": state.budget_usage.model_copy(
                        update={
                            "candidates": fixture.controller.config.runtime_budget.max_candidates
                            - 1
                        }
                    )
                }
            ),
        )

        with pytest.raises(ValueError, match="reserved merge slot"):
            fixture.controller.apply_generation2_refinement(work_id)
        assert not (fixture.controller.run_dir / "generation2-applications").exists()


def test_initial_apply_fails_closed_before_rewriting_non_proposal_phase() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="initial-apply-phase-guard-", dir=local) as temporary:
        fixture = build_generation2_fixture_controller(Path(temporary))
        branch_before = (
            fixture.controller.run_dir / "branches/branch-generation2-fixture.json"
        ).read_bytes()

        with pytest.raises(ValueError, match="requires the proposal phase"):
            fixture.controller.apply_proposals()

        assert (
            fixture.controller.run_dir / "branches/branch-generation2-fixture.json"
        ).read_bytes() == branch_before
        assert not (fixture.controller.run_dir / "proposal-causality-audit.json").exists()


def test_generation2_cli_uses_existing_store_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="generation2-cli-", dir=local) as temporary:
        monkeypatch.setattr(
            R4EvolutionController,
            "build_selector_graph_view",
            _fixture_selector_graph_view,
        )
        fixture = build_generation2_fixture_controller(Path(temporary))
        controller = fixture.controller
        before_state = controller.state()
        with CandidateStore(controller.run_dir / "candidates.sqlite3") as candidates:
            before_candidates = candidates.candidates()

        args = [
            "optimizer",
            "r4-plan-generation2",
            "--run-dir",
            controller.run_dir.as_posix(),
            "--config",
            fixture.config_path.as_posix(),
            "--parent-candidate-id",
            fixture.parent.candidate_id,
        ]
        first = CliRunner().invoke(app, args)
        second = CliRunner().invoke(app, args)

        assert first.exit_code == 0, first.output
        assert second.exit_code == 0, second.output
        first_payload = json.loads(first.output)
        second_payload = json.loads(second.output)
        assert first_payload == second_payload
        assert first_payload["status"] == "planned"
        assert not first_payload["proposal_intent_charged"]
        assert not first_payload["candidate_materialized"]
        assert not first_payload["held_out_evidence_read"]
        with PatchProposalStore(controller.run_dir / "proposal-work.sqlite3") as proposals:
            assert proposals.get_work(first_payload["proposal_work_id"])
        with CandidateStore(controller.run_dir / "candidates.sqlite3") as candidates:
            assert candidates.candidates() == before_candidates
        assert controller.state().budget_usage == before_state.budget_usage


def test_candidate_split_rejects_missing_frozen_case() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="candidate-split-missing-", dir=local) as temporary:
        fixture = build_generation2_fixture_controller(Path(temporary))
        controller = fixture.controller
        metadata_path = (
            controller.run_dir / f"evals/{fixture.parent.candidate_id}/train/run-metadata.json"
        )
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["selected_case_ids"] = metadata["selected_case_ids"][:-1]
        ArtifactStore(metadata_path.parent).write_json("run-metadata.json", metadata)

        with pytest.raises(ValueError, match="missing"):
            controller._candidate_split_task_ids(fixture.parent.candidate_id, "train")


def test_candidate_split_rejects_another_valid_frozen_plan() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="candidate-split-other-plan-", dir=local) as temporary:
        base = Path(temporary)
        fixture = build_generation2_fixture_controller(base)
        skill_path = ROOT / fixture.package_ref / "SKILL.md"
        alternate_path, alternate = _write_frozen_plan_fixture(
            base,
            name="alternate-three-train-one-validation",
            package_id=fixture.seed.package_id,
            package_snapshot_hash=fixture.seed.snapshot_hash,
            fixture_ref=skill_path.relative_to(ROOT).as_posix(),
            fixture_hash=sha256_bytes(skill_path.read_bytes()),
            train_case_count=3,
            validation_case_count=1,
        )
        metadata_path = (
            fixture.controller.run_dir
            / f"evals/{fixture.parent.candidate_id}/train/run-metadata.json"
        )
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["frozen_plan_ref"] = alternate_path.relative_to(ROOT).as_posix()
        metadata["frozen_plan_hash"] = alternate.plan_hash
        metadata["selected_case_ids"] = sorted(
            case.case_id for case in alternate.functional_cases if case.split == "train"
        )
        ArtifactStore(metadata_path.parent).write_json("run-metadata.json", metadata)

        with pytest.raises(ValueError, match="another frozen EvalPlan"):
            fixture.controller._candidate_split_task_ids(
                fixture.parent.candidate_id,
                "train",
            )
