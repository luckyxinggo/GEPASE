from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gepase.evals.candidate_pipeline import CandidateFunctionalCoordinator
from gepase.evals.engine import MultiFidelityEvalEngine, build_submission
from gepase.evals.evidence import ProviderFailureKind, TraceStep
from gepase.evals.providers.artifact import ArtifactProvider
from gepase.evals.providers.delegated import DelegatedProvider
from gepase.evals.recovery import (
    AgentAttemptKind,
    EvidenceDisposition,
    RecoveryActionKind,
    RecoveryDisposition,
    ReexecutionAuthorization,
    audit_recovery_attempt,
    build_recovered_submission,
    build_repair_exhaustion_terminalization,
    stage_recovery_evidence,
    validate_agent_reexecution,
)
from gepase.evals.schema import EvidenceTier
from gepase.evals.work_items import (
    EvalWorkItem,
    PackageAccessEvent,
    PackageAccessKind,
    PairingSnapshot,
    WorkStatus,
)
from gepase.optimizer.session_runtime import (
    ActiveSessionBudgetPolicy,
    ActiveSessionRuntime,
    HostAttemptReason,
    MeasurementKind,
    RoleBatchEstimate,
    RuntimeBarrier,
    RuntimeBudgetBinding,
    UsageAllowance,
    build_host_attempt_accounting,
)
from gepase.store.artifacts import ArtifactStore, canonical_json_bytes, sha256_bytes

ROOT = Path(__file__).resolve().parents[2]


def _item(work_id: str) -> EvalWorkItem:
    return EvalWorkItem(
        work_id=work_id,
        pair_id=f"pair-{work_id}",
        task_id=f"task-{work_id}",
        skill_id="fixture-skill",
        variant="candidate",
        evidence_tier=EvidenceTier.E2_DELEGATED,
        provider_id="agent-delegated-v1",
        prompt="Create a native artifact.",
        fixture_ref="benchmarks/fixtures/policy-evidence-06.json",
        skill_ref="benchmarks/skills/policy-evidence-evaluator",
        package_node_map={
            "SKILL.md": "node-skill",
            "scripts/run.py": "node-script",
        },
        requested_output={
            "filename": "result.bin",
            "media_type": "application/octet-stream",
        },
        candidate_snapshot_hash="a" * 64,
        frozen_plan_hash="b" * 64,
        split="train",
        pairing=PairingSnapshot(
            prompt_hash="c" * 64,
            fixture_hash="d" * 64,
            policy_hash="e" * 64,
            provider_snapshot="provider",
            host_model_snapshot="host:model",
            seed=42,
        ),
        required_capabilities=("filesystem",),
    )


def _write_workspace(path: Path, *, sensitive_required: bool = False) -> None:
    path.mkdir(parents=True)
    (path / "result.bin").write_bytes(b"native-output\x00\x01")
    private_path = "/" + "/".join(("Users", "private", "source"))
    transcript = private_path if sensitive_required else "isolated transcript"
    (path / "transcript.md").write_text(transcript, encoding="utf-8")
    (path / "package-access.json").write_bytes(
        canonical_json_bytes(
            {
                "package_access": [
                    {
                        "sequence": 0,
                        "kind": "read",
                        "path": "SKILL.md",
                        "node_id": "node-skill",
                        "bytes_loaded": 10,
                        "tokens_loaded": 3,
                    },
                    {
                        "sequence": 1,
                        "kind": "executed",
                        "path": "scripts/run.py",
                        "node_id": "node-script-typo",
                        "bytes_loaded": 0,
                        "tokens_loaded": 0,
                    },
                ]
            }
        )
    )
    (path / "observed-trace.json").write_bytes(
        canonical_json_bytes(
            {
                "observed_trace": [
                    {
                        "sequence": 0,
                        "action": "create",
                        "target": "result.bin",
                        "tool": "python",
                        "outcome": "completed",
                    }
                ]
            }
        )
    )
    diagnostic_path = "/" + "/".join(("Users", "private", "diagnostic"))
    (path / "generation-report.json").write_text(
        json.dumps({"source": diagnostic_path}) + "\n", encoding="utf-8"
    )


def _source_submission(
    item: EvalWorkItem,
    path: Path,
    workspace: Path,
    *,
    repair_attempt: bool,
) -> None:
    submission = build_submission(
        ROOT,
        item,
        host="test-host",
        model="test-model",
        host_task_id=f"host-{item.work_id}",
        context_id=f"context-{item.work_id}",
        duration_ms=100,
        artifact_root=workspace,
        transcript_path=workspace / "transcript.md",
        package_access=(
            PackageAccessEvent(
                sequence=0,
                kind=PackageAccessKind.READ,
                path="SKILL.md",
                node_id="node-skill",
                bytes_loaded=10,
                tokens_loaded=3,
            ),
            PackageAccessEvent(
                sequence=1,
                kind=PackageAccessKind.EXECUTED,
                path="scripts/run.py",
                node_id="node-script-typo",
            ),
        ),
        planned_trace=(TraceStep(sequence=0, action="plan"),),
        observed_trace=(TraceStep(sequence=0, action="create", outcome="completed"),),
        input_tokens=10,
        output_tokens=20,
        tool_calls=1,
        repair_attempt=repair_attempt,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(submission.model_dump_json(indent=2), encoding="utf-8")


def _host_attempt(
    item: EvalWorkItem,
    *,
    run_id: str,
    repair_attempt: bool,
) -> Any:
    return build_host_attempt_accounting(
        run_id=run_id,
        config_hash="f" * 64,
        role="executor",
        host_task_id=f"host-{item.work_id}",
        context_id=f"context-{item.work_id}",
        work_id=item.work_id,
        reason=(
            HostAttemptReason.EXECUTION_REPAIR
            if repair_attempt
            else HostAttemptReason.SUBMISSION_VALIDATION_FAILURE
        ),
        usage=UsageAllowance(
            agent_calls=1,
            estimated_tokens=1234,
            active_wall_clock_ms=0,
            repairs=1 if repair_attempt else 0,
        ),
        token_count_kind=MeasurementKind.ESTIMATED,
        agent_duration_ms=500,
        duration_kind=MeasurementKind.REPORTED,
        reason_zh="测试夹具中的既有 Host attempt。",
        evidence_refs=("state.md",),
        recorded_at=datetime(2026, 7, 29, tzinfo=UTC),
    )


def test_manifest_stages_required_evidence_and_preserves_raw_bytes() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="recovery-stage-", dir=local) as temporary:
        base = Path(temporary)
        workspace = base / "raw-workspace"
        item = _item("work-stage")
        _write_workspace(workspace)
        source = base / "source-submission.json"
        _source_submission(item, source, workspace, repair_attempt=False)
        before = {path.name: sha256_bytes(path.read_bytes()) for path in workspace.iterdir()}

        audit = audit_recovery_attempt(
            ROOT,
            item,
            run_id="fixture-run",
            run_root=base,
            raw_workspace=workspace,
            source_submission_path=source,
            attempt_kind=AgentAttemptKind.INITIAL_EXECUTION,
            host_attempt_accountings=(
                _host_attempt(item, run_id="fixture-run", repair_attempt=False),
            ),
            audited_at=datetime(2026, 7, 29, tzinfo=UTC),
        )
        assert audit.disposition is RecoveryDisposition.RECOVERABLE_WITHOUT_AGENT
        assert audit.manifest.action_kinds == (
            RecoveryActionKind.DETERMINISTIC_SUBMISSION_PACKAGING,
            RecoveryActionKind.DETERMINISTIC_METADATA,
        )
        assert len(audit.manifest.metadata_corrections) == 1
        correction = audit.manifest.metadata_corrections[0]
        assert correction.path == "scripts/run.py"
        assert correction.original_node_id == "node-script-typo"
        optional = next(
            entry
            for entry in audit.manifest.entries
            if entry.staged_path == "generation-report.json"
        )
        assert optional.disposition is EvidenceDisposition.EXCLUDED_OPTIONAL
        assert optional.sensitive_findings == ("private_home_path",)

        staged = stage_recovery_evidence(ROOT, audit, base / "staged")
        assert set(staged.artifact_relative_paths) == {
            "result.bin",
            "transcript.md",
            "package-access.json",
            "observed-trace.json",
        }
        assert not (base / "staged/generation-report.json").exists()
        assert (base / "staged/result.bin").read_bytes() == (workspace / "result.bin").read_bytes()
        corrected = json.loads((base / "staged/package-access.json").read_text())
        assert corrected["package_access"][1]["path"] == "scripts/run.py"
        assert corrected["package_access"][1]["node_id"] == "node-script"
        submission = build_submission(
            ROOT,
            item,
            host="test-host",
            model="test-model",
            host_task_id="recovery-packaging",
            context_id="recovery-packaging",
            duration_ms=100,
            artifact_root=base / "staged",
            artifact_relative_paths=staged.artifact_relative_paths,
            transcript_path=base / "staged/transcript.md",
            package_access=staged.package_access,
            planned_trace=(TraceStep(sequence=0, action="plan"),),
            observed_trace=staged.observed_trace,
            input_tokens=10,
            output_tokens=20,
            tool_calls=1,
        )
        ArtifactProvider().verify(ROOT, submission)
        DelegatedProvider().validate_submission(item, submission)
        assert {artifact.path for artifact in submission.artifacts} == set(
            staged.artifact_relative_paths
        )
        assert before == {
            path.name: sha256_bytes(path.read_bytes()) for path in workspace.iterdir()
        }


def test_sensitive_required_evidence_and_interrupted_workspace_fail_closed() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="recovery-fail-", dir=local) as temporary:
        base = Path(temporary)
        item = _item("work-sensitive")
        workspace = base / "sensitive"
        _write_workspace(workspace, sensitive_required=True)
        source = base / "source.json"
        _source_submission(item, source, workspace, repair_attempt=True)
        audit = audit_recovery_attempt(
            ROOT,
            item,
            run_id="fixture-run",
            run_root=base,
            raw_workspace=workspace,
            source_submission_path=source,
            attempt_kind=AgentAttemptKind.REEXECUTION,
            host_attempt_accountings=(
                _host_attempt(item, run_id="fixture-run", repair_attempt=True),
            ),
        )
        assert audit.disposition is RecoveryDisposition.TERMINAL_FAILURE_REQUIRED
        assert audit.failure_kind_if_terminal is ProviderFailureKind.INVALID_SUBMISSION
        with pytest.raises(ValueError, match="unrecoverable"):
            stage_recovery_evidence(ROOT, audit, base / "forbidden-stage")

        interrupted = base / "interrupted"
        interrupted.mkdir()
        (interrupted / "result.bin").write_bytes(b"partial")
        partial = audit_recovery_attempt(
            ROOT,
            item,
            run_id="fixture-run",
            run_root=base,
            raw_workspace=interrupted,
            source_submission_path=None,
            attempt_kind=AgentAttemptKind.REEXECUTION,
            host_attempt_accountings=(
                _host_attempt(item, run_id="fixture-run", repair_attempt=True),
            ),
        )
        assert partial.disposition is RecoveryDisposition.TERMINAL_FAILURE_REQUIRED
        assert partial.failure_kind_if_terminal is ProviderFailureKind.PARTIAL_ARTIFACT


def test_attempt_workspace_host_binding_rejects_mixed_evidence() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="recovery-binding-", dir=local) as temporary:
        base = Path(temporary)
        item = _item("work-binding")
        workspace = base / "raw-workspace"
        _write_workspace(workspace)
        source = base / "source.json"
        _source_submission(item, source, workspace, repair_attempt=False)
        attempt = _host_attempt(item, run_id="fixture-run", repair_attempt=False)

        wrong_workspace = base / "other-workspace"
        _write_workspace(wrong_workspace)
        with pytest.raises(ValueError, match="artifact_root"):
            audit_recovery_attempt(
                ROOT,
                item,
                run_id="fixture-run",
                run_root=base,
                raw_workspace=wrong_workspace,
                source_submission_path=source,
                attempt_kind=AgentAttemptKind.INITIAL_EXECUTION,
                host_attempt_accountings=(attempt,),
            )
        with pytest.raises(ValueError, match="repair_attempt"):
            audit_recovery_attempt(
                ROOT,
                item,
                run_id="fixture-run",
                run_root=base,
                raw_workspace=workspace,
                source_submission_path=source,
                attempt_kind=AgentAttemptKind.REEXECUTION,
                host_attempt_accountings=(attempt,),
            )
        wrong_host = attempt.model_copy(update={"context_id": "another-context"})
        with pytest.raises(ValueError, match="exactly one"):
            audit_recovery_attempt(
                ROOT,
                item,
                run_id="fixture-run",
                run_root=base,
                raw_workspace=workspace,
                source_submission_path=source,
                attempt_kind=AgentAttemptKind.INITIAL_EXECUTION,
                host_attempt_accountings=(wrong_host,),
            )

        audit = audit_recovery_attempt(
            ROOT,
            item,
            run_id="fixture-run",
            run_root=base,
            raw_workspace=workspace,
            source_submission_path=source,
            attempt_kind=AgentAttemptKind.INITIAL_EXECUTION,
            host_attempt_accountings=(attempt,),
        )
        (workspace / "transcript.md").write_text("changed after audit", encoding="utf-8")
        with pytest.raises(ValueError, match="workspace changed"):
            stage_recovery_evidence(ROOT, audit, base / "forbidden-stage")


def test_additional_agent_reexecution_requires_new_user_checkpoint() -> None:
    with pytest.raises(ValueError, match="new user checkpoint"):
        validate_agent_reexecution(
            ROOT,
            run_id="run",
            work_id="work",
            prior_reexecution_count=1,
            frozen_max_reexecutions=1,
        )
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reexecution-auth-", dir=local) as temporary:
        checkpoint = Path(temporary) / "checkpoint.json"
        checkpoint.write_text('{"approved":true}\n', encoding="utf-8")
        authorization = ReexecutionAuthorization(
            authorization_id="authorization-fixture",
            run_id="run",
            work_id="work",
            checkpoint_ref=checkpoint.relative_to(ROOT).as_posix(),
            checkpoint_sha256=sha256_bytes(checkpoint.read_bytes()),
            prior_reexecution_count=1,
            authorized_additional_reexecutions=1,
            reason_zh="用户明确批准一次额外重执行。",
            authorized_at=datetime(2026, 7, 29, tzinfo=UTC),
        )
        assert (
            validate_agent_reexecution(
                ROOT,
                run_id="run",
                work_id="work",
                prior_reexecution_count=1,
                frozen_max_reexecutions=1,
                authorization=authorization,
            )
            is RecoveryActionKind.AGENT_REEXECUTION
        )


def _runtime_policy() -> ActiveSessionBudgetPolicy:
    allowance = UsageAllowance(
        agent_calls=20,
        estimated_tokens=200_000,
        active_wall_clock_ms=1_000_000_000,
        repairs=10,
    )
    return ActiveSessionBudgetPolicy(
        initial_tranche=allowance,
        maximum_continuation_increment=allowance,
        max_concurrency=1,
        role_estimates={
            "executor": RoleBatchEstimate(
                max_estimated_tokens_per_work=10_000,
                timeout_ms_per_work=600_000,
                max_repair_attempts_per_work=1,
            ),
            "independent_grader": RoleBatchEstimate(
                max_estimated_tokens_per_work=10_000,
                timeout_ms_per_work=600_000,
                max_repair_attempts_per_work=1,
            ),
        },
        required_barriers=(
            RuntimeBarrier.PACKAGE_COMPILED,
            RuntimeBarrier.REFERENCE_EXECUTION_COMPLETE,
        ),
    )


class _FailureLedger:
    def __init__(self, record: Any) -> None:
        self.record = record

    def record_for_work(self, _work_id: str) -> Any:
        return self.record


class _FailureOnlyCandidateCoordinator(CandidateFunctionalCoordinator):
    def __init__(self, run_dir: Path, item: EvalWorkItem) -> None:
        self.project_root = ROOT
        self.run_dir = run_dir
        self.run_ref = run_dir.relative_to(ROOT).as_posix()
        self._failure_item = item
        self.ledger = _FailureLedger(
            SimpleNamespace(
                failure_kind=ProviderFailureKind.INVALID_SUBMISSION,
                record_id="record-failure",
            )
        )
        self.store = ArtifactStore(run_dir)
        self.policy = SimpleNamespace(comparator_case_ids=(item.task_id,))
        self.cases = {item.task_id: SimpleNamespace()}

    def _items(self) -> list[EvalWorkItem]:
        return [self._failure_item]


def test_candidate_typed_failure_skips_agent_grader_and_comparator() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="recovery-candidate-", dir=local) as temporary:
        run_dir = Path(temporary)
        item = _item("work-eval-failure")
        coordinator = _FailureOnlyCandidateCoordinator(run_dir, item)
        graders = coordinator.prepare_graders()
        comparators = coordinator.prepare_comparators()
        assert graders == {
            "prepared": 0,
            "grader_work_ids": [],
            "typed_failures_without_grader": [item.work_id],
        }
        assert comparators == {
            "prepared": 0,
            "comparator_work_ids": [],
            "evidence_incomplete_task_ids": [],
        }
        decision = json.loads(
            (run_dir / f"failure-comparator-decisions/{item.task_id}.json").read_text()
        )
        assert decision["candidate_margin"] == -1.0
        assert decision["agent_comparator_calls"] == 0


def test_recovered_success_ingests_without_double_accounting() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="recovery-success-", dir=local) as temporary:
        run_dir = Path(temporary) / "run"
        with MultiFidelityEvalEngine(ROOT, run_dir) as engine:
            runtime = ActiveSessionRuntime(
                run_dir,
                run_id="recovery-run",
                config_hash="f" * 64,
                policy=_runtime_policy(),
            )
            runtime.create(now=datetime(2026, 7, 29, tzinfo=UTC))
            binding = RuntimeBudgetBinding(
                owner_run_id="recovery-run",
                owner_run_ref=run_dir.relative_to(ROOT).as_posix(),
                config_hash="f" * 64,
                policy=_runtime_policy(),
            )
            engine.store.write_json("runtime-budget-binding.json", binding.model_dump(mode="json"))
            engine.store.write_json("run-metadata.json", {"mode": "recovery-fixture"})
            item = _item("work-recovered")
            engine.ledger.plan(item, "cache-recovered")
            engine.export_work(run_dir / "exports/work.json")
            workspace = run_dir / "raw-workspace"
            _write_workspace(workspace)
            source_path = run_dir / "execution-submissions/source.json"
            _source_submission(item, source_path, workspace, repair_attempt=True)
            host_attempt = build_host_attempt_accounting(
                run_id="recovery-run",
                config_hash="f" * 64,
                role="executor",
                host_task_id=f"host-{item.work_id}",
                context_id=f"context-{item.work_id}",
                work_id=item.work_id,
                reason=HostAttemptReason.EXECUTION_REPAIR,
                usage=UsageAllowance(
                    agent_calls=1,
                    estimated_tokens=1234,
                    active_wall_clock_ms=0,
                    repairs=1,
                ),
                token_count_kind=MeasurementKind.ESTIMATED,
                agent_duration_ms=500,
                duration_kind=MeasurementKind.REPORTED,
                reason_zh="既有 repair context 的 submission 需要确定性证据重打包。",
                evidence_refs=(source_path.relative_to(ROOT).as_posix(),),
                recorded_at=datetime(2026, 7, 29, tzinfo=UTC),
            )
            runtime.record_host_attempt(host_attempt, now=datetime(2026, 7, 29, tzinfo=UTC))
            audit = audit_recovery_attempt(
                ROOT,
                item,
                run_id="recovery-run",
                run_root=run_dir,
                raw_workspace=workspace,
                source_submission_path=source_path,
                attempt_kind=AgentAttemptKind.REEXECUTION,
                host_attempt_accountings=(host_attempt,),
                audited_at=datetime(2026, 7, 29, tzinfo=UTC),
            )
            staged = stage_recovery_evidence(ROOT, audit, run_dir / "staged")
            submission = build_recovered_submission(ROOT, audit, staged)
            before = runtime.state()
            first = engine.ingest_recovered_submission(
                submission,
                audit,
                auto_assert=False,
            )
            repeated = engine.ingest_recovered_submission(
                submission,
                audit,
                auto_assert=False,
            )
            after = runtime.state()
            assert first["duplicate"] is False and repeated["duplicate"] is True
            assert engine.ledger.work_statuses()[item.work_id] is WorkStatus.COMPLETED
            assert after.used.agent_calls == before.used.agent_calls
            assert after.used.estimated_tokens == before.used.estimated_tokens
            assert after.used.repairs == before.used.repairs
            assert after.cumulative_agent_duration_ms == before.cumulative_agent_duration_ms


@pytest.mark.parametrize(
    ("attempt_reason", "repairs", "failure_kind"),
    [
        (
            HostAttemptReason.SUBMISSION_VALIDATION_FAILURE,
            0,
            ProviderFailureKind.INVALID_SUBMISSION,
        ),
        (HostAttemptReason.EXECUTION_REPAIR, 1, ProviderFailureKind.INVALID_SUBMISSION),
        (HostAttemptReason.EXECUTION_INTERRUPTED, 0, ProviderFailureKind.PARTIAL_ARTIFACT),
    ],
)
def test_repair_exhaustion_terminalizes_once_without_double_accounting_and_continues(
    attempt_reason: HostAttemptReason,
    repairs: int,
    failure_kind: ProviderFailureKind,
) -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="recovery-terminal-", dir=local) as temporary:
        run_dir = Path(temporary) / "run"
        with MultiFidelityEvalEngine(ROOT, run_dir) as engine:
            runtime = ActiveSessionRuntime(
                run_dir,
                run_id="recovery-run",
                config_hash="f" * 64,
                policy=_runtime_policy(),
            )
            runtime.create(now=datetime(2026, 7, 29, tzinfo=UTC))
            binding = RuntimeBudgetBinding(
                owner_run_id="recovery-run",
                owner_run_ref=run_dir.relative_to(ROOT).as_posix(),
                config_hash="f" * 64,
                policy=_runtime_policy(),
            )
            engine.store.write_json("runtime-budget-binding.json", binding.model_dump(mode="json"))
            failed_item = _item("work-failed")
            remaining_item = _item("work-remaining")
            engine.ledger.plan(failed_item, "cache-failed")
            engine.ledger.plan(remaining_item, "cache-remaining")
            engine.export_work(run_dir / "exports/first.json", limit=1)
            workspace = run_dir / "raw-workspace"
            _write_workspace(workspace)
            source_path = run_dir / "execution-submissions/source.json"
            _source_submission(
                failed_item,
                source_path,
                workspace,
                repair_attempt=(
                    attempt_reason is not HostAttemptReason.SUBMISSION_VALIDATION_FAILURE
                ),
            )
            host_attempt = build_host_attempt_accounting(
                run_id="recovery-run",
                config_hash="f" * 64,
                role="executor",
                host_task_id="host-failed",
                context_id="context-failed",
                work_id=failed_item.work_id,
                reason=attempt_reason,
                usage=UsageAllowance(
                    agent_calls=1,
                    estimated_tokens=1234,
                    active_wall_clock_ms=0,
                    repairs=repairs,
                ),
                token_count_kind=MeasurementKind.ESTIMATED,
                agent_duration_ms=500,
                duration_kind=MeasurementKind.REPORTED,
                reason_zh="已有 Host context 未形成可接受 submission。",
                evidence_refs=(source_path.relative_to(ROOT).as_posix(),),
                recorded_at=datetime(2026, 7, 29, tzinfo=UTC),
            )
            runtime.record_host_attempt(host_attempt, now=datetime(2026, 7, 29, tzinfo=UTC))
            before_state = runtime.state()
            terminalization = build_repair_exhaustion_terminalization(
                ROOT,
                run_id="recovery-run",
                config_hash="f" * 64,
                work_id=failed_item.work_id,
                failure_kind=failure_kind,
                failure_detail="frozen repair allowance exhausted",
                source_submission_path=source_path,
                host_attempt_accounting_ids=(host_attempt.accounting_id,),
                requested_at=datetime(2026, 7, 29, tzinfo=UTC),
            )
            first = engine.terminalize_repair_exhaustion(terminalization)
            repeated = engine.terminalize_repair_exhaustion(terminalization)
            assert first["duplicate"] is False
            assert repeated["duplicate"] is True
            assert first["agent_usage_added"] is False
            state = runtime.state()
            assert state.used.agent_calls == before_state.used.agent_calls
            assert state.used.estimated_tokens == before_state.used.estimated_tokens
            assert state.used.repairs == before_state.used.repairs
            assert state.cumulative_agent_duration_ms == before_state.cumulative_agent_duration_ms
            assert state.completed_work_ids.count(failed_item.work_id) == 1
            assert engine.ledger.work_statuses()[failed_item.work_id] is WorkStatus.FAILED
            record = engine.ledger.record_for_work(failed_item.work_id)
            assert record is not None and record.failure_kind is failure_kind
            settlement = json.loads(
                (run_dir / f"reservation-settlements/{failed_item.work_id}.json").read_text()
            )
            assert settlement["accounting_mode"] == "preaccounted_host_attempts"
            assert settlement["actual"]["agent_calls"] == 0
            assert settlement["host_attempt_accounting_ids"] == [host_attempt.accounting_id]

            next_batch = engine.export_work(run_dir / "exports/remaining.json")
            assert next_batch["exported"] == 1
            assert engine.ledger.work_statuses()[remaining_item.work_id] is WorkStatus.EXPORTED
