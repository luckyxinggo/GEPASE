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
from gepase.evals.statistics import PairedScore
from gepase.evals.work_items import canonical_hash
from gepase.mutation.proposer import PatchProposalStore
from gepase.mutation.schema import PatchApplication, PatchApplicationStatus
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

    workspace = run_dir / f"candidates/{parent.candidate_id}/workspace"
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
    controller._write(f"candidates/{parent.candidate_id}/application.json", application)
    controller._write(f"candidates/{parent.candidate_id}/graph.json", graph)

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
