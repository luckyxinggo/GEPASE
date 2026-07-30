from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gepase.optimizer.evolution_controller import _reconcile_active_session_accounting
from gepase.optimizer.session_runtime import (
    ActiveSessionBudgetPolicy,
    ActiveSessionRuntime,
    BudgetReservationError,
    ContinuationAction,
    HostAttemptReason,
    MeasurementKind,
    RoleBatchEstimate,
    RuntimeBarrier,
    RuntimeSessionStatus,
    UsageAllowance,
    build_continuation_decision,
    build_host_attempt_accounting,
)
from gepase.store.artifacts import canonical_json_bytes, sha256_bytes


def _usage(
    calls: int,
    tokens: int,
    active_ms: int,
    *,
    proposals: int = 0,
    candidates: int = 0,
    repairs: int = 0,
) -> UsageAllowance:
    return UsageAllowance(
        agent_calls=calls,
        estimated_tokens=tokens,
        active_wall_clock_ms=active_ms,
        proposals=proposals,
        candidates=candidates,
        repairs=repairs,
    )


def _policy(*, calls: int = 10) -> ActiveSessionBudgetPolicy:
    return ActiveSessionBudgetPolicy(
        initial_tranche=_usage(
            calls,
            100_000,
            10_800_000,
            proposals=4,
            candidates=5,
            repairs=10,
        ),
        maximum_continuation_increment=_usage(
            3, 30_000, 3_600_000, proposals=1, candidates=1
        ),
        max_concurrency=3,
        role_estimates={
            "executor": RoleBatchEstimate(
                max_estimated_tokens_per_work=10_000,
                timeout_ms_per_work=600_000,
                max_repair_attempts_per_work=1,
            )
        },
        required_barriers=(RuntimeBarrier.PACKAGE_COMPILED,),
    )


def test_cross_day_pause_does_not_consume_active_time_or_reset_usage(tmp_path) -> None:
    started = datetime(2026, 7, 28, 0, 0, tzinfo=UTC)
    runtime = ActiveSessionRuntime(
        tmp_path,
        run_id="run",
        config_hash="a" * 64,
        policy=_policy(),
    )
    runtime.create(now=started)
    checkpoint = runtime.pause(
        barrier=RuntimeBarrier.PACKAGE_COMPILED,
        evidence_hash="b" * 64,
        completed_work_ids=(),
        not_exported_work_ids=("work-1",),
        candidate_gate_summary={},
        next_batch_estimate=runtime.estimate_batch("executor", 1),
        continuation_risk_zh="继续后将导出一个 Executor work。",
        now=started + timedelta(hours=1),
    )
    next_day = started + timedelta(days=1, hours=1)
    paused_clock = runtime.clock(now=next_day)
    assert paused_clock == {
        "calendar_elapsed_ms": 90_000_000,
        "active_wall_clock_ms": 3_600_000,
        "paused_ms": 86_400_000,
        "cumulative_agent_duration_ms": 0,
    }

    checkpoint_path = tmp_path / "budget-checkpoints" / f"{checkpoint.checkpoint_id}.json"
    decision = build_continuation_decision(
        checkpoint,
        checkpoint_path,
        action=ContinuationAction.CONTINUE,
        approved_increment=_usage(1, 10_000, 600_000),
        reviewer="user",
        comment_zh="批准下一个有界批次。",
        decided_at=next_day,
    )
    first = runtime.apply_decision(decision, now=next_day)
    second = runtime.apply_decision(decision, now=next_day)
    assert first.approved == second.approved
    assert len(tuple((tmp_path / "continuation-decisions").glob("*.json"))) == 1
    resumed_clock = runtime.clock(now=next_day + timedelta(minutes=30))
    assert resumed_clock["active_wall_clock_ms"] == 5_400_000
    assert resumed_clock["paused_ms"] == 86_400_000


def test_complete_batch_is_blocked_before_export_and_gets_checkpoint(tmp_path) -> None:
    policy = _policy(calls=1).model_copy(
        update={"initial_tranche": _usage(1, 15_000, 10_800_000)}
    )
    runtime = ActiveSessionRuntime(
        tmp_path,
        run_id="run",
        config_hash="c" * 64,
        policy=policy,
    )
    runtime.create(now=datetime(2026, 7, 28, tzinfo=UTC))
    with pytest.raises(BudgetReservationError, match="complete work batch"):
        runtime.reserve(
            batch_id="executor:two",
            role="executor",
            work_ids=("work-a", "work-b"),
            now=datetime(2026, 7, 28, 0, 0, 1, tzinfo=UTC),
        )
    state = runtime.state()
    assert state.status is RuntimeSessionStatus.AWAITING_CONTINUATION
    assert state.open_reservations == ()
    checkpoint = next((tmp_path / "budget-checkpoints").glob("*.json"))
    assert '"budget_limit"' in checkpoint.read_text(encoding="utf-8")


def test_same_scope_proposal_repair_reserves_one_intent_and_two_agent_calls(
    tmp_path,
) -> None:
    policy = _policy().model_copy(
        update={
            "role_estimates": {
                "proposal": RoleBatchEstimate(
                    max_estimated_tokens_per_work=10_000,
                    timeout_ms_per_work=600_000,
                    max_repair_attempts_per_work=1,
                )
            }
        }
    )
    runtime = ActiveSessionRuntime(
        tmp_path,
        run_id="run",
        config_hash="9" * 64,
        policy=policy,
    )
    started = datetime(2026, 7, 30, tzinfo=UTC)
    runtime.create(now=started)
    initial = runtime.estimate_batch(
        "proposal", 1, proposal_intents=1, repair_attempts=0
    )
    runtime.reserve(
        batch_id="proposal:initial",
        role="proposal",
        work_ids=("proposal-initial",),
        upper_bound=initial,
        now=started,
    )
    runtime.settle(
        work_id="proposal-initial",
        actual_tokens=1_000,
        actual_duration_ms=100,
        proposals=1,
        now=started,
    )
    repair = runtime.estimate_batch(
        "proposal", 1, proposal_intents=0, repair_attempts=1
    )
    runtime.reserve(
        batch_id="proposal:repair",
        role="proposal",
        work_ids=("proposal-repair-1",),
        upper_bound=repair,
        now=started,
    )
    state = runtime.settle(
        work_id="proposal-repair-1",
        actual_tokens=1_000,
        actual_duration_ms=100,
        repairs=1,
        proposals=0,
        now=started,
    )

    assert state.used.agent_calls == 2
    assert state.used.proposals == 1
    assert state.used.repairs == 1
    assert state.used.candidates == 0


def test_reservation_settlement_and_internal_candidate_usage_are_idempotent(tmp_path) -> None:
    runtime = ActiveSessionRuntime(
        tmp_path,
        run_id="run",
        config_hash="d" * 64,
        policy=_policy(),
    )
    started = datetime(2026, 7, 28, tzinfo=UTC)
    runtime.create(now=started)
    runtime.reserve(
        batch_id="executor:one",
        role="executor",
        work_ids=("work-a",),
        now=started,
    )
    settled = runtime.settle(
        work_id="work-a",
        actual_tokens=11_000,
        actual_duration_ms=12_000,
        now=started,
    )
    repeated = runtime.settle(
        work_id="work-a",
        actual_tokens=11_000,
        actual_duration_ms=12_000,
        now=started,
    )
    assert settled.used.agent_calls == repeated.used.agent_calls == 1
    settlement = next((tmp_path / "reservation-settlements").glob("*.json"))
    assert '"token_variance": 1000' in settlement.read_text(encoding="utf-8")
    charged = runtime.record_internal_usage(
        accounting_id="candidate:c1",
        usage=_usage(0, 0, 0, candidates=1),
        now=started,
    )
    repeated_charge = runtime.record_internal_usage(
        accounting_id="candidate:c1",
        usage=_usage(0, 0, 0, candidates=1),
        now=started,
    )
    assert charged.used.candidates == repeated_charge.used.candidates == 1


def test_host_attempt_accounting_is_append_only_and_charges_one_context(tmp_path) -> None:
    runtime = ActiveSessionRuntime(
        tmp_path,
        run_id="run",
        config_hash="e" * 64,
        policy=_policy(),
    )
    started = datetime(2026, 7, 28, tzinfo=UTC)
    runtime.create(now=started)
    attempt = build_host_attempt_accounting(
        run_id="run",
        config_hash="e" * 64,
        role="executor",
        host_task_id="host-1",
        context_id="context-1",
        work_id="work-a",
        reason=HostAttemptReason.EXECUTION_REPAIR,
        usage=_usage(1, 10_000, 0, repairs=1),
        token_count_kind=MeasurementKind.ESTIMATED,
        agent_duration_ms=600_000,
        duration_kind=MeasurementKind.ESTIMATED,
        reason_zh="首次产物损坏; 已用新上下文完成一次修复执行。",
        evidence_refs=("artifacts/stages/GH-E1/audit.json",),
        recorded_at=started,
    )
    charged = runtime.record_host_attempt(attempt, now=started)
    repeated = runtime.record_host_attempt(attempt, now=started)
    assert charged.used.agent_calls == repeated.used.agent_calls == 1
    assert charged.used.estimated_tokens == repeated.used.estimated_tokens == 10_000
    assert charged.used.repairs == repeated.used.repairs == 1
    assert charged.cumulative_agent_duration_ms == 600_000
    stored = next((tmp_path / "host-attempt-accounting").glob("*.json"))
    assert HostAttemptReason.EXECUTION_REPAIR.value in stored.read_text(encoding="utf-8")


def test_final_runtime_accounting_reconciles_settlements_and_host_attempts(tmp_path) -> None:
    runtime = ActiveSessionRuntime(
        tmp_path,
        run_id="run",
        config_hash="f" * 64,
        policy=_policy(),
    )
    started = datetime(2026, 7, 29, tzinfo=UTC)
    runtime.create(now=started)
    runtime.record_internal_usage(
        accounting_id="candidate:c1",
        usage=_usage(0, 0, 0, candidates=1),
        now=started,
    )
    runtime.reserve(
        batch_id="executor:one",
        role="executor",
        work_ids=("work-a",),
        now=started,
    )
    runtime.settle(
        work_id="work-a",
        actual_tokens=1_000,
        actual_duration_ms=2_000,
        repairs=1,
        now=started,
    )
    attempt = build_host_attempt_accounting(
        run_id="run",
        config_hash="f" * 64,
        role="executor",
        host_task_id="host-extra",
        context_id="context-extra",
        work_id="work-b",
        reason=HostAttemptReason.EXECUTION_INTERRUPTED,
        usage=_usage(1, 500, 0),
        token_count_kind=MeasurementKind.ESTIMATED,
        agent_duration_ms=3_000,
        duration_kind=MeasurementKind.ESTIMATED,
        reason_zh="未形成 submission 的额外真实上下文。",
        evidence_refs=("state.md",),
        recorded_at=started,
    )
    state = runtime.record_host_attempt(attempt, now=started)

    summary = _reconcile_active_session_accounting(tmp_path, tmp_path, state)

    assert summary["reconciled"] is True
    assert summary["accepted_role_settlement_count"] == 1
    assert summary["host_attempt_count"] == 1
    assert summary["authoritative_session_usage"]["agent_calls"] == 2
    assert summary["authoritative_session_usage"]["estimated_tokens"] == 1_500
    assert summary["authoritative_session_usage"]["repairs"] == 1
    assert summary["candidate_internal_accounting_count"] == 1
    assert summary["authoritative_cumulative_agent_duration_ms"] == 5_000


def test_recovery_ingest_forces_fresh_checkpoint_and_rejects_stale_state(tmp_path) -> None:
    started = datetime(2026, 7, 29, tzinfo=UTC)
    runtime = ActiveSessionRuntime(
        tmp_path,
        run_id="run",
        config_hash="a" * 64,
        policy=_policy(),
    )
    runtime.create(now=started)
    runtime.reserve(
        batch_id="executor:recovery",
        role="executor",
        work_ids=("work-a", "work-b"),
        now=started,
    )
    attempts = []
    for index, work_id in enumerate(("work-a", "work-b"), start=1):
        attempt = build_host_attempt_accounting(
            run_id="run",
            config_hash="a" * 64,
            role="executor",
            host_task_id=f"host-{index}",
            context_id=f"context-{index}",
            work_id=work_id,
            reason=HostAttemptReason.SUBMISSION_VALIDATION_FAILURE,
            usage=_usage(1, 1_000, 0),
            token_count_kind=MeasurementKind.ESTIMATED,
            agent_duration_ms=100,
            duration_kind=MeasurementKind.REPORTED,
            reason_zh="既有 Host attempt 测试记录。",
            evidence_refs=("state.md",),
            recorded_at=started,
        )
        runtime.record_host_attempt(attempt, now=started)
        attempts.append(attempt)
    old = runtime.pause(
        barrier=RuntimeBarrier.PACKAGE_COMPILED,
        evidence_hash="b" * 64,
        completed_work_ids=(),
        not_exported_work_ids=(),
        candidate_gate_summary={},
        next_batch_estimate=None,
        continuation_risk_zh="旧 checkpoint 测试夹具。",
        now=started,
    )
    old_path = tmp_path / "budget-checkpoints" / f"{old.checkpoint_id}.json"
    old_decision = build_continuation_decision(
        old,
        old_path,
        action=ContinuationAction.CONTINUE,
        approved_increment=_usage(1, 1_000, 1_000),
        reviewer="user",
        comment_zh="旧决定只用于 stale 检验。",
        decided_at=started,
    )
    usage_before = runtime.state().used
    for work_id, attempt in zip(("work-a", "work-b"), attempts, strict=True):
        runtime.settle_preaccounted_work(
            work_id=work_id,
            host_attempt_accounting_ids=(attempt.accounting_id,),
            require_post_recovery_checkpoint=True,
            now=started,
        )
    pending = runtime.state()
    assert pending.used == usage_before
    assert pending.post_recovery_checkpoint_required
    assert set(pending.uncheckpointed_recovery_work_ids) == {"work-a", "work-b"}
    with pytest.raises(ValueError, match="fresh post-recovery checkpoint"):
        runtime.apply_decision(old_decision, now=started)

    fresh = runtime.pause(
        barrier=RuntimeBarrier.POST_RECOVERY_CHECKPOINT,
        evidence_hash="c" * 64,
        completed_work_ids=("work-a", "work-b"),
        not_exported_work_ids=("work-c", "work-d"),
        candidate_gate_summary={"recovery": "complete"},
        next_batch_estimate=runtime.estimate_batch("executor", 2),
        continuation_risk_zh="恢复已写入, 继续前需用户确认。",
        now=started,
    )
    current = runtime.state()
    assert not current.post_recovery_checkpoint_required
    assert current.uncheckpointed_recovery_work_ids == ()
    assert fresh.state_hash == sha256_bytes(
        canonical_json_bytes(current.model_dump(mode="json"))
    )
    freshness = runtime.audit_checkpoint_freshness(fresh.checkpoint_id, audited_at=started)
    assert freshness.valid
    with pytest.raises(ValueError, match="not the latest"):
        runtime.audit_checkpoint_freshness(old.checkpoint_id, audited_at=started)
    with pytest.raises(ValueError, match="stale checkpoint"):
        runtime.apply_decision(old_decision, now=started)
