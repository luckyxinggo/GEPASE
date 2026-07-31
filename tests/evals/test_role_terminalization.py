from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import gepase.cli.eval as eval_cli
from gepase.cli.app import app
from gepase.evals.candidate_pipeline import (
    CandidateFunctionalCoordinator,
    CandidatePairSummary,
    build_validation_incomplete_resolution,
)
from gepase.evals.eval_plan import RubricCriterion
from gepase.evals.functional import (
    AnalysisNodeHint,
    AnalyzerEvidenceSummary,
    AnalyzerWorkItem,
    BlindArtifact,
    ComparatorSide,
    ComparatorWorkItem,
    FunctionalRole,
    IndependentGraderWorkItem,
    IsolationAudit,
    RoleAttemptFailure,
    RoleAttemptKind,
    RoleAttemptTerminalization,
    RoleFailureKind,
    build_role_attempt_terminalization,
)
from gepase.evals.functional_pipeline import (
    FunctionalEvalCoordinator,
    RoleEvidenceIncompleteError,
)
from gepase.evals.scores import TaskScoreVector
from gepase.evals.statistics import PairedScore
from gepase.optimizer.evolution_controller import R4EvolutionController
from gepase.optimizer.session_runtime import (
    ActiveSessionBudgetPolicy,
    ActiveSessionRuntime,
    HostAttemptReason,
    MeasurementKind,
    RoleBatchEstimate,
    RuntimeBudgetBinding,
    UsageAllowance,
    build_host_attempt_accounting,
)
from gepase.schemas.common import ArtifactRef
from gepase.store.artifacts import ArtifactStore, sha256_bytes

ROOT = Path(__file__).resolve().parents[2]


class _EmptyLedger:
    def submissions(self) -> list[object]:
        return []


def _policy() -> ActiveSessionBudgetPolicy:
    allowance = UsageAllowance(
        agent_calls=20,
        estimated_tokens=200_000,
        active_wall_clock_ms=2_000_000,
        repairs=10,
    )
    return ActiveSessionBudgetPolicy(
        initial_tranche=allowance,
        maximum_continuation_increment=allowance,
        role_estimates={
            role: RoleBatchEstimate(
                max_estimated_tokens_per_work=20_000,
                timeout_ms_per_work=600_000,
                max_repair_attempts_per_work=1,
            )
            for role in ("independent_grader", "comparator", "analyzer")
        },
        required_barriers=(),
    )


def _work(
    role: FunctionalRole,
    work_id: str,
) -> IndependentGraderWorkItem | ComparatorWorkItem | AnalyzerWorkItem:
    rubric = (
        RubricCriterion(
            criterion_id="quality",
            label_zh="质量",
            description_zh="离线终态夹具",
            weight=1.0,
        ),
    )
    blind = BlindArtifact(
        blind_id="blind-fixture",
        artifact_root="artifacts/local/blind",
        artifact=ArtifactRef(
            path="result.gif",
            sha256="a" * 64,
            media_type="image/gif",
            size_bytes=1,
        ),
        contact_sheet_ref="artifacts/local/contact.png",
        inspection_ref="artifacts/local/inspection.json",
    )
    if role is FunctionalRole.INDEPENDENT_GRADER:
        return IndependentGraderWorkItem(
            grader_work_id=work_id,
            task_id="task-fixture",
            task_prompt="inspect",
            expected_output_zh="GIF",
            rubric=rubric,
            blind_artifact=blind,
            submission_schema_ref="schemas/independent_grader_submission.schema.json",
        )
    if role is FunctionalRole.COMPARATOR:
        return ComparatorWorkItem(
            comparator_work_id=work_id,
            task_id="task-fixture",
            task_prompt="compare",
            expected_output_zh="GIF",
            rubric=rubric,
            left=ComparatorSide(side_id="left", blind_artifact=blind),
            right=ComparatorSide(side_id="right", blind_artifact=blind),
            order_label="AB",
            submission_schema_ref="schemas/comparator_submission.schema.json",
        )
    summary = AnalyzerEvidenceSummary(
        variant="original",
        execution_record_ref="artifacts/local/record.json",
        deterministic_bundle_ref="artifacts/local/deterministic.json",
        independent_grade_ref="artifacts/local/grade.json",
        task_correctness=0.5,
        output_quality=0.5,
        failed_expectation_ids=("expectation",),
        grader_feedback_zh="fixture",
    )
    return AnalyzerWorkItem(
        analyzer_work_id=work_id,
        task_id="task-fixture",
        pair_id="pair-fixture",
        task_prompt="analyze",
        baseline=summary.model_copy(update={"variant": "no-skill"}),
        original=summary,
        package_graph_ref="artifacts/local/graph.json",
        node_hints=(
            AnalysisNodeHint(
                node_id="node-fixture",
                path="SKILL.md",
                kind="instruction",
                label="fixture",
            ),
        ),
        submission_schema_ref="schemas/analyzer_submission.schema.json",
    )


def _coordinator(
    eval_dir: Path,
    owner_dir: Path,
    policy: ActiveSessionBudgetPolicy,
) -> FunctionalEvalCoordinator:
    binding = RuntimeBudgetBinding(
        owner_run_id="fixture-owner",
        owner_run_ref=owner_dir.relative_to(ROOT).as_posix(),
        config_hash="b" * 64,
        policy=policy,
    )
    ArtifactStore(eval_dir).write_json(
        "runtime-budget-binding.json",
        binding.model_dump(mode="json"),
    )
    value = object.__new__(FunctionalEvalCoordinator)
    value.project_root = ROOT
    value.run_dir = eval_dir
    value.run_ref = eval_dir.relative_to(ROOT).as_posix()
    value.store = ArtifactStore(eval_dir)
    value.ledger = _EmptyLedger()  # type: ignore[assignment]
    return value


def _single_attempt_terminalization(
    *,
    run_id: str,
    task_id: str,
    work_id: str,
) -> RoleAttemptTerminalization:
    return build_role_attempt_terminalization(
        run_id=run_id,
        task_id=task_id,
        work_id=work_id,
        role=FunctionalRole.INDEPENDENT_GRADER,
        attempts=(
            RoleAttemptFailure(
                attempt_kind=RoleAttemptKind.INITIAL,
                host_attempt_accounting_id=f"host-{work_id}",
                host_task_id=f"host-task-{work_id}",
                context_id=f"context-{work_id}",
                evidence_sha256="c" * 64,
                failure_kind=RoleFailureKind.TIMEOUT,
                source_refs=("artifacts/local/role-timeout.json",),
            ),
        ),
        allowed_repair_attempts=0,
        terminalized_at=datetime(2026, 7, 30, tzinfo=UTC),
    )


def test_validation_incomplete_resolution_partitions_frozen_tasks_without_fake_score() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="validation-resolution-", dir=local) as temporary:
        run = Path(temporary) / "validation"
        store = ArtifactStore(run)
        terminalization = _single_attempt_terminalization(
            run_id="fixture-owner",
            task_id="task-incomplete",
            work_id="grader-work-incomplete",
        )
        store.write_json(
            "run-metadata.json",
            {
                "mode": "frozen-candidate",
                "split": "validation",
                "candidate_id": "candidate-fixture",
                "selected_case_ids": ["task-scored", "task-incomplete"],
            },
        )
        store.write_json(
            "candidate-run-summary.json",
            {
                "candidate_id": "candidate-fixture",
                "split": "validation",
                "status": "evidence_incomplete",
                "evidence_complete": False,
                "gate_eligible": False,
                "pair_summaries": [{"task_id": "task-scored"}],
                "incomplete_cases": [
                    {
                        "task_id": "task-incomplete",
                        "role": "independent_grader",
                        "work_id": terminalization.work_id,
                        "terminalization_id": terminalization.terminalization_id,
                        "disposition": "evidence_incomplete",
                    }
                ],
            },
        )
        store.write_json(
            f"role-terminalizations/independent_grader/{terminalization.work_id}.json",
            terminalization.model_dump(mode="json"),
        )

        first = build_validation_incomplete_resolution(
            ROOT,
            run,
            owner_run_id="fixture-owner",
            candidate_id="candidate-fixture",
            required_task_ids=("task-scored", "task-incomplete"),
        )
        second = build_validation_incomplete_resolution(
            ROOT,
            run,
            owner_run_id="fixture-owner",
            candidate_id="candidate-fixture",
            required_task_ids=("task-incomplete", "task-scored"),
        )

        assert first == second
        assert not first.gate_eligible
        assert not first.deployable
        assert first.scored_task_ids == ("task-scored",)
        assert first.incomplete_cases[0].terminalization_id == terminalization.terminalization_id
        payload = first.model_dump(mode="json")
        assert "score" not in payload
        assert "winner" not in payload


@pytest.mark.parametrize("role", tuple(FunctionalRole))
def test_exhausted_role_attempts_terminalize_once_without_fake_output(role: FunctionalRole) -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="role-terminal-", dir=local) as temporary:
        base = Path(temporary)
        owner = base / "owner"
        eval_dir = base / "eval"
        policy = _policy()
        runtime = ActiveSessionRuntime(
            owner,
            run_id="fixture-owner",
            config_hash="b" * 64,
            policy=policy,
        )
        started = datetime(2026, 7, 30, tzinfo=UTC)
        runtime.create(now=started)
        work_id = f"{role.value}-work-fixture"
        runtime.reserve(
            batch_id=f"batch-{role.value}",
            role=role.value,
            work_ids=(work_id,),
            now=started,
        )
        work_dir, submission_dir, _model = FunctionalEvalCoordinator._role_paths(role)
        ArtifactStore(eval_dir).write_json(
            f"{work_dir}/{work_id}.json",
            _work(role, work_id).model_dump(mode="json"),
        )
        attempts: list[RoleAttemptFailure] = []
        for index, attempt_kind in enumerate((RoleAttemptKind.INITIAL, RoleAttemptKind.REPAIR)):
            source_ref = f"artifacts/local/failure-{role.value}-{index}.json"
            accounting = build_host_attempt_accounting(
                run_id="fixture-owner",
                config_hash="b" * 64,
                role=role.value,
                host_task_id=f"host-{role.value}-{index}",
                context_id=f"context-{role.value}-{index}",
                work_id=work_id,
                reason=(
                    HostAttemptReason.SUBMISSION_VALIDATION_FAILURE
                    if index == 0
                    else HostAttemptReason.EXECUTION_REPAIR
                ),
                usage=UsageAllowance(
                    agent_calls=1,
                    estimated_tokens=100 + index,
                    active_wall_clock_ms=0,
                    repairs=index,
                ),
                token_count_kind=MeasurementKind.ESTIMATED,
                agent_duration_ms=1_000 + index,
                duration_kind=MeasurementKind.REPORTED,
                reason_zh="初次提交无效" if index == 0 else "有界 repair 仍超时",
                evidence_refs=(source_ref,),
                recorded_at=started,
            )
            runtime.record_host_attempt(accounting, now=started)
            host_path = owner / "host-attempt-accounting" / f"{accounting.accounting_id}.json"
            attempts.append(
                RoleAttemptFailure(
                    attempt_kind=attempt_kind,
                    host_attempt_accounting_id=accounting.accounting_id,
                    host_task_id=accounting.host_task_id,
                    context_id=accounting.context_id,
                    evidence_sha256=sha256_bytes(host_path.read_bytes()),
                    failure_kind=(
                        RoleFailureKind.PARTIAL_ARTIFACT if index == 0 else RoleFailureKind.TIMEOUT
                    ),
                    source_refs=(source_ref,),
                )
            )
        terminal = build_role_attempt_terminalization(
            run_id="fixture-owner",
            task_id="task-fixture",
            work_id=work_id,
            role=role,
            attempts=tuple(attempts),
            allowed_repair_attempts=1,
            terminalized_at=started,
        )
        coordinator = _coordinator(eval_dir, owner, policy)
        before = runtime.state().used
        first = coordinator.terminalize_role_attempts(terminal)
        after = runtime.state()
        resumed = _coordinator(eval_dir, owner, policy)
        second = resumed.terminalize_role_attempts(terminal)
        final = runtime.state()

        assert not first["duplicate"]
        assert second["duplicate"]
        assert after.used == before == final.used
        assert work_id in final.completed_work_ids
        assert not final.open_reservations
        assert not (eval_dir / submission_dir / f"{work_id}.json").exists()
        assert not list((eval_dir / "task-score-vectors").glob("*.json"))
        payload = json.loads(
            (eval_dir / f"role-terminalizations/{role.value}/{work_id}.json").read_text()
        )
        assert not payload["scoring_penalty_applied"]
        assert not payload["synthetic_submission_created"]


def test_role_terminalization_rejects_cross_context() -> None:
    with pytest.raises(ValueError, match="contexts must be isolated"):
        build_role_attempt_terminalization(
            run_id="run",
            task_id="task",
            work_id="work",
            role=FunctionalRole.INDEPENDENT_GRADER,
            attempts=(
                RoleAttemptFailure(
                    attempt_kind=RoleAttemptKind.INITIAL,
                    host_attempt_accounting_id="host-a",
                    host_task_id="task-a",
                    context_id="same",
                    evidence_sha256="a" * 64,
                    failure_kind=RoleFailureKind.INVALID_SUBMISSION,
                    source_refs=("artifacts/local/a.json",),
                ),
                RoleAttemptFailure(
                    attempt_kind=RoleAttemptKind.REPAIR,
                    host_attempt_accounting_id="host-b",
                    host_task_id="task-b",
                    context_id="same",
                    evidence_sha256="b" * 64,
                    failure_kind=RoleFailureKind.TIMEOUT,
                    source_refs=("artifacts/local/b.json",),
                ),
            ),
            allowed_repair_attempts=1,
        )


def test_role_terminalization_cli_delegates_to_existing_coordinator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminalization = _single_attempt_terminalization(
        run_id="fixture-run",
        task_id="task-a",
        work_id="grader-work-a",
    )
    terminal_path = tmp_path / "terminalization.json"
    terminal_path.write_text(terminalization.model_dump_json(), encoding="utf-8")
    calls: list[object] = []
    snapshots: list[bool] = []

    class FakeCoordinator:
        def terminalize_role_attempts(self, value: object) -> dict[str, object]:
            calls.append(value)
            return {"duplicate": False, "status": "evidence_incomplete"}

    class FakeEngine:
        def __enter__(self) -> FakeEngine:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def functional_coordinator(self) -> FakeCoordinator:
            return FakeCoordinator()

        def snapshot_ledger(self) -> None:
            snapshots.append(True)

    monkeypatch.setattr(eval_cli, "_eval_engine", lambda _run_dir: FakeEngine())
    result = CliRunner().invoke(
        app,
        [
            "eval",
            "terminalize-role-attempts",
            "--run-dir",
            tmp_path.as_posix(),
            "--terminalization",
            terminal_path.as_posix(),
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0] == terminalization
    assert snapshots == [True]


class _FixtureRecord:
    def __init__(self, record_id: str) -> None:
        self.record_id = record_id
        self.failure_kind = None

    def model_copy(self, *, update: dict[str, object]) -> _FixtureRecord:
        _ = update
        return _FixtureRecord(self.record_id)

    def model_dump(self, *, mode: str = "python") -> dict[str, object]:
        _ = mode
        return {"record_id": self.record_id, "failure_kind": None}


class _FixtureLedger:
    def __init__(self, records: dict[str, _FixtureRecord]) -> None:
        self.records = records
        self.derived: list[_FixtureRecord] = []

    def record_for_work(self, work_id: str) -> _FixtureRecord | None:
        return self.records.get(work_id)

    def store_derived_record(self, record: _FixtureRecord) -> None:
        self.derived.append(record)


def _blind(task_id: str, variant: str) -> BlindArtifact:
    return BlindArtifact(
        blind_id=f"blind-{task_id}-{variant}",
        artifact_root=f"artifacts/local/{task_id}/{variant}",
        artifact=ArtifactRef(
            path="result.gif",
            sha256="e" * 64,
            media_type="image/gif",
            size_bytes=1,
        ),
        contact_sheet_ref=f"artifacts/local/{task_id}/{variant}/contact.png",
        inspection_ref=f"artifacts/local/{task_id}/{variant}/inspection.json",
    )


def _case(task_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        case_id=task_id,
        prompt=f"fixture prompt {task_id}",
        expected_output_zh="GIF",
        rubric=(
            RubricCriterion(
                criterion_id="quality",
                label_zh="质量",
                description_zh="自包含 role fixture",
                weight=1.0,
            ),
        ),
    )


def test_two_cases_one_grader_terminalized_other_continues_and_gate_is_closed() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="role-two-case-", dir=local) as temporary:
        run_dir = Path(temporary) / "candidate-train"
        selected = ("task-a", "task-b")
        items = tuple(
            SimpleNamespace(
                task_id=task_id,
                work_id=f"candidate-work-{task_id}",
                variant="candidate",
                pair_id=f"pair-{task_id}",
            )
            for task_id in selected
        )
        terminalization = _single_attempt_terminalization(
            run_id="fixture-run",
            task_id=selected[0],
            work_id="grader-work-task-a",
        )
        records = {item.work_id: _FixtureRecord(f"record-{item.work_id}") for item in items}
        records.update(
            {
                f"{item.work_id}-assertions": _FixtureRecord(f"record-{item.work_id}-assertions")
                for item in items
            }
        )
        ledger = _FixtureLedger(records)
        coordinator = object.__new__(CandidateFunctionalCoordinator)
        coordinator.project_root = ROOT
        coordinator.run_dir = run_dir
        coordinator.run_ref = run_dir.relative_to(ROOT).as_posix()
        coordinator.store = ArtifactStore(run_dir)
        coordinator.ledger = ledger  # type: ignore[assignment]
        coordinator.metadata = {
            "candidate_id": "candidate-fixture",
            "candidate_content_hash": "f" * 64,
            "split": "train",
        }
        coordinator.reference_key = SimpleNamespace(key_hash="a" * 64)  # type: ignore[assignment]
        coordinator.cases = {task_id: _case(task_id) for task_id in selected}  # type: ignore[assignment]
        coordinator.policy = SimpleNamespace(comparator_case_ids=selected)  # type: ignore[assignment]
        coordinator._items = lambda: items  # type: ignore[method-assign]
        coordinator._reserve_role_batch = lambda _role, _ids: None  # type: ignore[method-assign]

        def grader_for_source(source_work_id: str) -> tuple[object, str, BlindArtifact]:
            if source_work_id == items[0].work_id:
                raise RoleEvidenceIncompleteError(terminalization)
            return object(), "grader-work-task-b", _blind("task-b", "candidate")

        coordinator._grader_for_source = grader_for_source  # type: ignore[method-assign]
        coordinator._reference_models = (  # type: ignore[method-assign]
            lambda task_id: (
                object(),
                object(),
                _blind(task_id, "reference"),
                f"reference-work-{task_id}",
            )
        )

        comparison = coordinator.prepare_comparators()
        assert comparison["prepared"] == 2
        assert comparison["evidence_incomplete_task_ids"] == [selected[0]]
        comparator_tasks = {
            json.loads(path.read_text(encoding="utf-8"))["task_id"]
            for path in (run_dir / "comparator-work-items").glob("*.json")
        }
        assert comparator_tasks == {selected[1]}

        coordinator.policy = SimpleNamespace(comparator_case_ids=())  # type: ignore[assignment]
        coordinator.audit_package_access = lambda: {"valid": True}  # type: ignore[method-assign]
        coordinator.audit_isolation = lambda: IsolationAudit(  # type: ignore[method-assign]
            valid=True,
            executor_context_ids=(),
            grader_context_ids=(),
            comparator_context_ids=(),
            analyzer_context_ids=(),
            duplicate_context_ids=(),
            oracle_leakage_findings=(),
            sibling_leakage_findings=(),
            candidate_identity_findings=(),
        )
        coordinator._usage_report = lambda: {}  # type: ignore[method-assign]

        def score_task(
            task_id: str,
        ) -> tuple[TaskScoreVector, CandidatePairSummary, PairedScore, dict[str, object]]:
            if task_id == selected[0]:
                raise RoleEvidenceIncompleteError(terminalization)
            vector = TaskScoreVector(
                task_id=task_id,
                pair_id=f"pair-{task_id}",
                variant="candidate",
                candidate_snapshot_hash="b" * 64,
                task_correctness=0.8,
                output_quality=0.8,
                skill_gain=0.1,
                reliability=1.0,
                efficiency=1.0,
                package_quality=1.0,
                evidence_refs=(f"artifacts/local/{task_id}/vector.json",),
                scoring_policy_ref="configs/fixture-scoring.json",
            )
            summary = CandidatePairSummary(
                task_id=task_id,
                pair_id=f"pair-{task_id}",
                split="train",
                reference_vector_ref=f"artifacts/local/{task_id}/reference.json",
                candidate_vector_ref=f"artifacts/local/{task_id}/candidate.json",
                reference_score=0.7,
                candidate_score=0.8,
                paired_delta=0.1,
                correctness_delta=0.1,
                quality_delta=0.1,
            )
            paired = PairedScore(
                task_id=task_id,
                category="generic",
                risk_level="low",
                parent_score=0.7,
                candidate_score=0.8,
                evidence_tier="E3",
                minimum_acceptance_tier="E2",
                parent_record_id=summary.reference_vector_ref,
                candidate_record_id=summary.candidate_vector_ref,
            )
            return vector, summary, paired, {"task_id": task_id}

        coordinator._score_task = score_task  # type: ignore[method-assign]
        result = coordinator.finalize()
        assert result["status"] == "evidence_incomplete"
        assert not result["gate_eligible"]
        assert result["vectors"] == 1
        assert result["incomplete_cases"][0]["task_id"] == selected[0]
        paired_payload = json.loads((run_dir / "paired-scores.json").read_text())
        observed = tuple(row["task_id"] for row in paired_payload["rows"])
        with pytest.raises(ValueError, match="missing"):
            R4EvolutionController._require_exact_task_ids(
                observed,
                selected,
                source="two-case role failure Gate fixture",
            )


def test_analyzer_skips_terminalized_task_and_prepares_other_case() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="role-analyzer-", dir=local) as temporary:
        run_dir = Path(temporary) / "reference"
        selected = ("task-a", "task-b")
        items = tuple(
            SimpleNamespace(
                task_id=task_id,
                work_id=f"{task_id}-{variant}",
                variant=variant,
                pair_id=f"pair-{task_id}",
            )
            for task_id in selected
            for variant in ("no-skill", "original")
        )
        terminalization = _single_attempt_terminalization(
            run_id="fixture-run",
            task_id=selected[0],
            work_id="grader-work-task-a",
        )
        coordinator = object.__new__(FunctionalEvalCoordinator)
        coordinator.project_root = ROOT
        coordinator.run_dir = run_dir
        coordinator.run_ref = run_dir.relative_to(ROOT).as_posix()
        coordinator.store = ArtifactStore(run_dir)
        coordinator.ledger = _EmptyLedger()  # type: ignore[assignment]
        coordinator.policy = SimpleNamespace(comparator_case_ids=())  # type: ignore[assignment]
        coordinator.frozen = SimpleNamespace(plan_hash="c" * 64)  # type: ignore[assignment]
        coordinator.cases = {task_id: _case(task_id) for task_id in selected}  # type: ignore[assignment]
        coordinator.metadata = {"package_graph_ref": "artifacts/local/fixture-graph.json"}
        coordinator.graph = SimpleNamespace(nodes=())  # type: ignore[assignment]
        coordinator._items = lambda: items  # type: ignore[method-assign]
        coordinator._reserve_role_batch = lambda _role, _ids: None  # type: ignore[method-assign]
        coordinator.audit_package_access = lambda: {"valid": True}  # type: ignore[method-assign]
        coordinator._source_record = (  # type: ignore[method-assign]
            lambda item: SimpleNamespace(
                record_id=f"record-{item.work_id}",
                failure_kind=None,
            )
        )
        coordinator._deterministic = (  # type: ignore[method-assign]
            lambda _item: SimpleNamespace(weighted_score=1.0, assertion_results=())
        )
        coordinator._node_hints = lambda _item: ()  # type: ignore[method-assign]

        def grader_for_source(source_work_id: str) -> tuple[object, str, BlindArtifact]:
            if source_work_id.startswith("task-a-"):
                raise RoleEvidenceIncompleteError(terminalization)
            return (
                SimpleNamespace(overall_score=0.9, feedback_zh="fixture feedback"),
                f"grader-{source_work_id}",
                _blind("task-b", source_work_id.rsplit("-", 1)[-1]),
            )

        coordinator._grader_for_source = grader_for_source  # type: ignore[method-assign]
        result = coordinator.prepare_analyzers()

        assert result["prepared"] == 1
        assert result["evidence_incomplete_task_ids"] == [selected[0]]
        analyzer = json.loads(
            next((run_dir / "analyzer-work-items").glob("*.json")).read_text(encoding="utf-8")
        )
        assert analyzer["task_id"] == selected[1]
