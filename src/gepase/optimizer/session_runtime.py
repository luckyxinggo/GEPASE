"""Active-session accounting, work reservation, and user continuation checkpoints.

This module is part of the existing optimizer runtime contract.  It never
executes work; it prevents Core from exporting a batch that cannot fit in the
currently approved bounded tranche.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from gepase.schemas.common import FrozenModel
from gepase.store.artifacts import atomic_write, canonical_json_bytes, sha256_bytes


class RuntimeSessionStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    AWAITING_CONTINUATION = "awaiting_continuation"
    STOPPED = "stopped"
    ABORTED = "aborted"
    COMPLETE = "complete"


class ContinuationAction(StrEnum):
    CONTINUE = "continue"
    STOP_AND_REPORT = "stop_and_report"
    ABORT_BEFORE_EFFECT_CLAIM = "abort_before_effect_claim"


class HostAttemptReason(StrEnum):
    """Why a real Agent-host context did not become a settled work submission.

    This is deliberately narrower than ``ReservationSettlement``.  A host
    attempt that failed before Core could ingest a work submission must never
    be represented as a fabricated WorkSubmission or as an extra reservation.
    """

    DISPATCH_CONTRACT_FAILURE = "dispatch_contract_failure"
    SUBMISSION_VALIDATION_FAILURE = "submission_validation_failure"
    EXECUTION_INTERRUPTED = "execution_interrupted"
    EXECUTION_REPAIR = "execution_repair"


class MeasurementKind(StrEnum):
    REPORTED = "reported"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


class RuntimeBarrier(StrEnum):
    PACKAGE_COMPILED = "package_compiled"
    REFERENCE_EXECUTION_COMPLETE = "reference_execution_complete"
    REFERENCE_SEALED = "reference_sealed"
    PROPOSAL_SCOPE_READY = "proposal_scope_ready"
    CANDIDATE_TRAIN_COMPLETE = "candidate_train_complete"
    CANDIDATE_VALIDATION_COMPLETE = "candidate_validation_complete"
    MERGE_RESOLVED = "merge_resolved"
    BEFORE_FINAL_REPORT = "before_final_report"
    BUDGET_LIMIT = "budget_limit"
    POST_RECOVERY_CHECKPOINT = "post_recovery_checkpoint"


class UsageAllowance(FrozenModel):
    agent_calls: int = Field(ge=0)
    estimated_tokens: int = Field(ge=0)
    active_wall_clock_ms: int = Field(ge=0)
    proposals: int = Field(default=0, ge=0)
    candidates: int = Field(default=0, ge=0)
    repairs: int = Field(default=0, ge=0)

    def add(self, other: UsageAllowance) -> UsageAllowance:
        return UsageAllowance(
            **{
                name: int(getattr(self, name)) + int(getattr(other, name))
                for name in type(self).model_fields
            }
        )

    def subtract(self, other: UsageAllowance) -> UsageAllowance:
        values = {
            name: int(getattr(self, name)) - int(getattr(other, name))
            for name in type(self).model_fields
        }
        if any(value < 0 for value in values.values()):
            raise ValueError("usage allowance subtraction cannot become negative")
        return UsageAllowance(**values)

    def within(self, limit: UsageAllowance) -> bool:
        return all(
            int(getattr(self, name)) <= int(getattr(limit, name))
            for name in type(self).model_fields
        )


class RoleBatchEstimate(FrozenModel):
    max_estimated_tokens_per_work: int = Field(ge=1)
    timeout_ms_per_work: int = Field(ge=1)
    max_repair_attempts_per_work: int = Field(default=0, ge=0, le=2)


class ActiveSessionBudgetPolicy(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    initial_tranche: UsageAllowance
    maximum_continuation_increment: UsageAllowance
    max_concurrency: int = Field(default=3, ge=1, le=32)
    role_estimates: dict[str, RoleBatchEstimate] = Field(min_length=1)
    required_barriers: tuple[RuntimeBarrier, ...]
    require_user_continuation: Literal[True] = True
    cumulative_usage_never_resets: Literal[True] = True

    @model_validator(mode="after")
    def unique_barriers(self) -> ActiveSessionBudgetPolicy:
        if len(self.required_barriers) != len(set(self.required_barriers)):
            raise ValueError("runtime barriers must be unique")
        return self


class RuntimeBudgetBinding(FrozenModel):
    """Bind a nested Eval run to its owning active-session runtime."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    owner_run_id: str = Field(min_length=1)
    owner_run_ref: str = Field(min_length=1)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: ActiveSessionBudgetPolicy

    @model_validator(mode="after")
    def relative_owner(self) -> RuntimeBudgetBinding:
        path = Path(self.owner_run_ref)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("runtime owner reference must be repository-relative")
        return self


class WorkBatchReservation(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    reservation_id: str
    batch_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    work_ids: tuple[str, ...] = Field(min_length=1)
    unsettled_work_ids: tuple[str, ...] = Field(min_length=1)
    upper_bound: UsageAllowance
    created_at: datetime

    @model_validator(mode="after")
    def work_identity(self) -> WorkBatchReservation:
        if len(self.work_ids) != len(set(self.work_ids)):
            raise ValueError("reservation work_ids must be unique")
        if not set(self.unsettled_work_ids) <= set(self.work_ids):
            raise ValueError("unsettled work must belong to the reserved batch")
        return self


class ReservationSettlement(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    reservation_id: str = Field(min_length=1)
    work_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    reserved_upper_bound_share: UsageAllowance
    actual: UsageAllowance
    token_variance: int
    agent_duration_ms: int = Field(ge=0)
    accounting_mode: Literal["direct_submission", "preaccounted_host_attempts"] = (
        "direct_submission"
    )
    host_attempt_accounting_ids: tuple[str, ...] = ()
    approved_tranche_exceeded_after_settlement: bool
    settled_at: datetime

    @model_validator(mode="after")
    def accounting_source_is_unambiguous(self) -> ReservationSettlement:
        if self.accounting_mode == "direct_submission":
            if self.host_attempt_accounting_ids:
                raise ValueError("direct settlement cannot cite preaccounted HostAttempts")
            return self
        if not self.host_attempt_accounting_ids:
            raise ValueError("preaccounted settlement requires HostAttempt IDs")
        if len(self.host_attempt_accounting_ids) != len(set(self.host_attempt_accounting_ids)):
            raise ValueError("preaccounted HostAttempt IDs must be unique")
        if any(int(getattr(self.actual, name)) for name in UsageAllowance.model_fields):
            raise ValueError("preaccounted settlement cannot charge runtime usage again")
        if self.agent_duration_ms:
            raise ValueError("preaccounted settlement cannot charge Agent duration again")
        return self


class HostAttemptAccounting(FrozenModel):
    """Append-only accounting for one Agent-host context outside Core ingest.

    The record exists solely for a failed dispatch or a failed execution that
    was repaired before a valid WorkSubmission could be accepted.  It cannot
    carry proposals or candidates, and is attached to the owner run's single
    active-session ledger.
    """

    schema_version: Literal["1.0.0"] = "1.0.0"
    accounting_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    role: str = Field(min_length=1)
    host_task_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    work_id: str | None = Field(default=None, min_length=1)
    reason: HostAttemptReason
    usage: UsageAllowance
    token_count_kind: MeasurementKind
    agent_duration_ms: int = Field(ge=0)
    duration_kind: MeasurementKind
    reason_zh: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    recorded_at: datetime

    @model_validator(mode="after")
    def is_a_single_non_submission_context(self) -> HostAttemptAccounting:
        if self.usage.agent_calls != 1:
            raise ValueError("host attempt accounting must represent exactly one context")
        if self.usage.active_wall_clock_ms != 0:
            raise ValueError("host attempt active wall time is tracked by the session clock")
        if self.usage.proposals or self.usage.candidates:
            raise ValueError("host attempt accounting cannot create proposals or candidates")
        if (
            self.reason
            in {
                HostAttemptReason.DISPATCH_CONTRACT_FAILURE,
                HostAttemptReason.SUBMISSION_VALIDATION_FAILURE,
                HostAttemptReason.EXECUTION_INTERRUPTED,
            }
            and self.usage.repairs
        ):
            raise ValueError("failed dispatch/submission cannot consume repair budget")
        if self.reason is HostAttemptReason.EXECUTION_REPAIR and self.usage.repairs != 1:
            raise ValueError("an execution repair must consume exactly one repair")
        for reference in self.evidence_refs:
            path = Path(reference)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("host attempt evidence refs must be repository-relative")
        return self


class BudgetCheckpoint(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    checkpoint_id: str
    run_id: str = Field(min_length=1)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    barrier: RuntimeBarrier
    state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    used: UsageAllowance
    reserved: UsageAllowance
    approved: UsageAllowance
    completed_work_ids: tuple[str, ...]
    in_progress_work_ids: tuple[str, ...]
    not_exported_work_ids: tuple[str, ...]
    candidate_gate_summary: dict[str, str]
    next_batch_estimate: UsageAllowance | None = None
    continuation_risk_zh: str = Field(min_length=1)
    previous_decision_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class CheckpointFreshnessAudit(FrozenModel):
    """Proof that a checkpoint names the exact currently persisted runtime state."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    audit_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_id: str = Field(min_length=1)
    checkpoint_ref: str = Field(min_length=1)
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_latest_checkpoint: Literal[True] = True
    no_uncheckpointed_recovery: Literal[True] = True
    valid: Literal[True] = True
    audited_at: datetime

    @model_validator(mode="after")
    def exact_state_binding(self) -> CheckpointFreshnessAudit:
        path = Path(self.checkpoint_ref)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("checkpoint freshness ref must be repository-relative")
        if self.checkpoint_state_hash != self.runtime_state_hash:
            raise ValueError("checkpoint does not bind the current runtime state")
        return self


class BudgetContinuationDecision(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    decision_id: str
    run_id: str = Field(min_length=1)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_id: str = Field(min_length=1)
    checkpoint_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: ContinuationAction
    approved_increment: UsageAllowance
    reviewer: str = Field(min_length=1)
    comment_zh: str = Field(min_length=1)
    previous_decision_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    decided_at: datetime

    @model_validator(mode="after")
    def bounded_action(self) -> BudgetContinuationDecision:
        if self.action is not ContinuationAction.CONTINUE and any(
            int(getattr(self.approved_increment, name)) for name in UsageAllowance.model_fields
        ):
            raise ValueError("stop/abort decisions cannot approve extra budget")
        if self.action is ContinuationAction.CONTINUE and not any(
            int(getattr(self.approved_increment, name)) for name in UsageAllowance.model_fields
        ):
            raise ValueError("continue decision requires a bounded positive increment")
        return self


class ActiveSessionState(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    run_id: str = Field(min_length=1)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: RuntimeSessionStatus
    created_at: datetime
    last_transition_at: datetime
    active_accumulated_ms: int = Field(default=0, ge=0)
    paused_accumulated_ms: int = Field(default=0, ge=0)
    cumulative_agent_duration_ms: int = Field(default=0, ge=0)
    used: UsageAllowance
    approved: UsageAllowance
    open_reservations: tuple[WorkBatchReservation, ...] = ()
    completed_work_ids: tuple[str, ...] = ()
    internal_accounting_ids: tuple[str, ...] = ()
    checkpoint_ids: tuple[str, ...] = ()
    latest_checkpoint_id: str | None = None
    latest_decision_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    post_recovery_checkpoint_required: bool = False
    uncheckpointed_recovery_work_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def recovery_checkpoint_state_is_consistent(self) -> ActiveSessionState:
        if len(self.uncheckpointed_recovery_work_ids) != len(
            set(self.uncheckpointed_recovery_work_ids)
        ):
            raise ValueError("uncheckpointed recovery work IDs must be unique")
        if self.post_recovery_checkpoint_required != bool(
            self.uncheckpointed_recovery_work_ids
        ):
            raise ValueError("post-recovery checkpoint flag and work set disagree")
        return self


class BudgetReservationError(ValueError):
    """Raised before export when a complete batch does not fit the tranche."""


def _now(value: datetime | None) -> datetime:
    return value or datetime.now(UTC)


def _elapsed_ms(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() * 1000))


def _hash_model(value: FrozenModel) -> str:
    return sha256_bytes(canonical_json_bytes(value.model_dump(mode="json")))


class ActiveSessionRuntime:
    STATE_NAME = "runtime-session.json"

    def __init__(
        self,
        run_dir: Path,
        *,
        run_id: str,
        config_hash: str,
        policy: ActiveSessionBudgetPolicy,
    ) -> None:
        self.run_dir = run_dir.resolve()
        self.run_id = run_id
        self.config_hash = config_hash
        self.policy = policy

    @property
    def state_path(self) -> Path:
        return self.run_dir / self.STATE_NAME

    def create(self, *, now: datetime | None = None) -> ActiveSessionState:
        if self.state_path.exists():
            raise FileExistsError("runtime session already exists")
        current = _now(now)
        state = ActiveSessionState(
            run_id=self.run_id,
            config_hash=self.config_hash,
            status=RuntimeSessionStatus.ACTIVE,
            created_at=current,
            last_transition_at=current,
            used=UsageAllowance(agent_calls=0, estimated_tokens=0, active_wall_clock_ms=0),
            approved=self.policy.initial_tranche,
        )
        self._write(state)
        return state

    def state(self) -> ActiveSessionState:
        state = ActiveSessionState.model_validate_json(self.state_path.read_text(encoding="utf-8"))
        if state.run_id != self.run_id or state.config_hash != self.config_hash:
            raise ValueError("runtime session belongs to another run/config")
        return state

    def clock(self, *, now: datetime | None = None) -> dict[str, int]:
        state = self.state()
        current = _now(now)
        transition = _elapsed_ms(state.last_transition_at, current)
        active = state.active_accumulated_ms
        paused = state.paused_accumulated_ms
        if state.status is RuntimeSessionStatus.ACTIVE:
            active += transition
        elif state.status in {
            RuntimeSessionStatus.PAUSED,
            RuntimeSessionStatus.AWAITING_CONTINUATION,
        }:
            paused += transition
        return {
            "calendar_elapsed_ms": _elapsed_ms(state.created_at, current),
            "active_wall_clock_ms": active,
            "paused_ms": paused,
            "cumulative_agent_duration_ms": state.cumulative_agent_duration_ms,
        }

    def estimate_batch(
        self,
        role: str,
        work_count: int,
        *,
        proposal_intents: int | None = None,
        repair_attempts: int | None = None,
    ) -> UsageAllowance:
        if work_count < 1:
            raise ValueError("batch estimate requires at least one work item")
        estimate = self.policy.role_estimates.get(role)
        if estimate is None:
            raise ValueError(f"role has no frozen batch estimate: {role}")
        resolved_proposals = work_count if role == "proposal" else 0
        if proposal_intents is not None:
            if role != "proposal" and proposal_intents:
                raise ValueError("only proposal work may reserve mutation intents")
            if proposal_intents < 0 or proposal_intents > work_count:
                raise ValueError("proposal intent estimate exceeds the batch")
            resolved_proposals = proposal_intents
        resolved_repairs = estimate.max_repair_attempts_per_work * work_count
        if repair_attempts is not None:
            if repair_attempts < 0 or repair_attempts > work_count:
                raise ValueError("repair estimate exceeds the batch")
            resolved_repairs = repair_attempts
        waves = math.ceil(work_count / self.policy.max_concurrency)
        return UsageAllowance(
            agent_calls=work_count,
            estimated_tokens=estimate.max_estimated_tokens_per_work * work_count,
            active_wall_clock_ms=estimate.timeout_ms_per_work * waves,
            proposals=resolved_proposals,
            repairs=resolved_repairs,
        )

    def finish(
        self,
        *,
        status: Literal[RuntimeSessionStatus.COMPLETE, RuntimeSessionStatus.ABORTED],
        now: datetime | None = None,
    ) -> ActiveSessionState:
        state = self._advance_clock(self.state(), _now(now))
        if state.open_reservations:
            raise ValueError("runtime cannot finish with unsettled work reservations")
        updated = state.model_copy(update={"status": status})
        self._write(updated)
        return updated

    def reserve(
        self,
        *,
        batch_id: str,
        role: str,
        work_ids: tuple[str, ...],
        upper_bound: UsageAllowance | None = None,
        now: datetime | None = None,
    ) -> WorkBatchReservation:
        state = self._advance_clock(self.state(), _now(now))
        existing = next(
            (item for item in state.open_reservations if item.batch_id == batch_id), None
        )
        requested = upper_bound or self.estimate_batch(role, len(work_ids))
        if existing is not None:
            if (
                existing.role != role
                or existing.work_ids != work_ids
                or existing.upper_bound != requested
            ):
                raise ValueError("batch_id reused with a different reservation")
            self._write(state)
            return existing
        if state.status is not RuntimeSessionStatus.ACTIVE:
            self._write(state)
            raise BudgetReservationError(
                "runtime is paused; a valid continuation decision is required before export"
            )
        reserved = self.reserved_usage(state)
        projected = state.used.add(reserved).add(requested)
        if not projected.within(state.approved):
            state = state.model_copy(update={"status": RuntimeSessionStatus.AWAITING_CONTINUATION})
            self._write(state)
            runtime_evidence = {
                "run_id": self.run_id,
                "config_hash": self.config_hash,
                "state": state.model_dump(mode="json"),
                "blocked_batch_id": batch_id,
                "blocked_work_ids": list(work_ids),
                "requested": requested.model_dump(mode="json"),
            }
            self.pause(
                barrier=RuntimeBarrier.BUDGET_LIMIT,
                evidence_hash=sha256_bytes(canonical_json_bytes(runtime_evidence)),
                completed_work_ids=state.completed_work_ids,
                not_exported_work_ids=work_ids,
                candidate_gate_summary={},
                next_batch_estimate=requested,
                continuation_risk_zh=(
                    "下一原子 work batch 超过当前已批准 tranche; 未导出任何半批任务。"
                ),
                now=now,
            )
            raise BudgetReservationError(
                "complete work batch exceeds the currently approved bounded tranche"
            )
        identity = {
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "batch_id": batch_id,
            "role": role,
            "work_ids": list(work_ids),
            "upper_bound": requested.model_dump(mode="json"),
        }
        reservation = WorkBatchReservation(
            reservation_id=f"reservation-{sha256_bytes(canonical_json_bytes(identity))[:24]}",
            batch_id=batch_id,
            role=role,
            work_ids=work_ids,
            unsettled_work_ids=work_ids,
            upper_bound=requested,
            created_at=_now(now),
        )
        self._write(
            state.model_copy(
                update={
                    "open_reservations": (*state.open_reservations, reservation),
                }
            )
        )
        return reservation

    def settle(
        self,
        *,
        work_id: str,
        actual_tokens: int,
        actual_duration_ms: int,
        repairs: int = 0,
        proposals: int = 0,
        candidates: int = 0,
        now: datetime | None = None,
    ) -> ActiveSessionState:
        actual_values = (
            actual_tokens,
            actual_duration_ms,
            repairs,
            proposals,
            candidates,
        )
        if any(value < 0 for value in actual_values):
            raise ValueError("actual usage values cannot be negative")
        state = self._advance_clock(self.state(), _now(now))
        if work_id in state.completed_work_ids:
            self._write(state)
            return state
        match = next(
            (item for item in state.open_reservations if work_id in item.unsettled_work_ids),
            None,
        )
        if match is None:
            raise ValueError("settled work has no in-flight reservation")
        updated_reservations: list[WorkBatchReservation] = []
        for reservation in state.open_reservations:
            if reservation.reservation_id != match.reservation_id:
                updated_reservations.append(reservation)
                continue
            remaining = tuple(item for item in reservation.unsettled_work_ids if item != work_id)
            if remaining:
                updated_reservations.append(
                    reservation.model_copy(update={"unsettled_work_ids": remaining})
                )
        reserved_share = UsageAllowance(
            **{
                name: math.ceil(int(getattr(match.upper_bound, name)) / len(match.work_ids))
                for name in UsageAllowance.model_fields
            }
        )
        actual = UsageAllowance(
            agent_calls=1,
            estimated_tokens=actual_tokens,
            active_wall_clock_ms=0,
            repairs=repairs,
            proposals=proposals,
            candidates=candidates,
        )
        updated = state.model_copy(
            update={
                "used": state.used.add(actual),
                "cumulative_agent_duration_ms": (
                    state.cumulative_agent_duration_ms + actual_duration_ms
                ),
                "open_reservations": tuple(updated_reservations),
                "completed_work_ids": (*state.completed_work_ids, work_id),
            }
        )
        self._write(updated)
        settlement = ReservationSettlement(
            reservation_id=match.reservation_id,
            work_id=work_id,
            role=match.role,
            reserved_upper_bound_share=reserved_share,
            actual=actual,
            token_variance=actual_tokens - reserved_share.estimated_tokens,
            agent_duration_ms=actual_duration_ms,
            approved_tranche_exceeded_after_settlement=not updated.used.add(
                self.reserved_usage(updated)
            ).within(updated.approved),
            settled_at=_now(now),
        )
        atomic_write(
            self.run_dir / "reservation-settlements" / f"{work_id}.json",
            canonical_json_bytes(settlement.model_dump(mode="json")),
        )
        return updated

    def settle_preaccounted_work(
        self,
        *,
        work_id: str,
        host_attempt_accounting_ids: tuple[str, ...],
        require_post_recovery_checkpoint: bool = False,
        now: datetime | None = None,
    ) -> ActiveSessionState:
        """Release one work reservation without double-counting Agent use.

        The cited HostAttemptAccounting records must already belong to this
        runtime and work.  This path is deliberately valid while paused because
        it performs no execution and grants no new budget.
        """

        self.validate_preaccounted_failure(
            work_id=work_id,
            host_attempt_accounting_ids=host_attempt_accounting_ids,
        )
        state = self._advance_clock(self.state(), _now(now))
        settlement_path = self.run_dir / "reservation-settlements" / f"{work_id}.json"
        if work_id in state.completed_work_ids:
            if not settlement_path.is_file():
                raise ValueError("completed work is missing its reservation settlement")
            stored = ReservationSettlement.model_validate_json(
                settlement_path.read_text(encoding="utf-8")
            )
            if (
                stored.accounting_mode != "preaccounted_host_attempts"
                or stored.host_attempt_accounting_ids != host_attempt_accounting_ids
            ):
                raise ValueError("repeated terminal settlement does not match stored evidence")
            self._write(state)
            return state
        match = next(
            (item for item in state.open_reservations if work_id in item.unsettled_work_ids),
            None,
        )
        if match is None:
            raise ValueError("terminalized work has no in-flight reservation")
        updated_reservations: list[WorkBatchReservation] = []
        for reservation in state.open_reservations:
            if reservation.reservation_id != match.reservation_id:
                updated_reservations.append(reservation)
                continue
            remaining = tuple(item for item in reservation.unsettled_work_ids if item != work_id)
            if remaining:
                updated_reservations.append(
                    reservation.model_copy(update={"unsettled_work_ids": remaining})
                )
        reserved_share = UsageAllowance(
            **{
                name: math.ceil(int(getattr(match.upper_bound, name)) / len(match.work_ids))
                for name in UsageAllowance.model_fields
            }
        )
        zero = UsageAllowance(agent_calls=0, estimated_tokens=0, active_wall_clock_ms=0)
        updated = state.model_copy(
            update={
                "status": RuntimeSessionStatus.AWAITING_CONTINUATION
                if require_post_recovery_checkpoint
                else state.status,
                "open_reservations": tuple(updated_reservations),
                "completed_work_ids": (*state.completed_work_ids, work_id),
                "post_recovery_checkpoint_required": True
                if require_post_recovery_checkpoint
                else state.post_recovery_checkpoint_required,
                "uncheckpointed_recovery_work_ids": (
                    *state.uncheckpointed_recovery_work_ids,
                    work_id,
                )
                if require_post_recovery_checkpoint
                else state.uncheckpointed_recovery_work_ids,
            }
        )
        settlement = ReservationSettlement(
            reservation_id=match.reservation_id,
            work_id=work_id,
            role=match.role,
            reserved_upper_bound_share=reserved_share,
            actual=zero,
            token_variance=-reserved_share.estimated_tokens,
            agent_duration_ms=0,
            accounting_mode="preaccounted_host_attempts",
            host_attempt_accounting_ids=host_attempt_accounting_ids,
            approved_tranche_exceeded_after_settlement=not updated.used.add(
                self.reserved_usage(updated)
            ).within(updated.approved),
            settled_at=_now(now),
        )
        if settlement_path.exists():
            stored = ReservationSettlement.model_validate_json(
                settlement_path.read_text(encoding="utf-8")
            )
            comparable = (
                stored.reservation_id,
                stored.work_id,
                stored.role,
                stored.reserved_upper_bound_share,
                stored.actual,
                stored.accounting_mode,
                stored.host_attempt_accounting_ids,
            )
            expected = (
                settlement.reservation_id,
                settlement.work_id,
                settlement.role,
                settlement.reserved_upper_bound_share,
                settlement.actual,
                settlement.accounting_mode,
                settlement.host_attempt_accounting_ids,
            )
            if comparable != expected:
                raise ValueError("append-only reservation settlement already differs")
        else:
            atomic_write(
                settlement_path,
                canonical_json_bytes(settlement.model_dump(mode="json")),
            )
        self._write(updated)
        return updated

    def settle_preaccounted_failure(
        self,
        *,
        work_id: str,
        host_attempt_accounting_ids: tuple[str, ...],
        now: datetime | None = None,
    ) -> ActiveSessionState:
        """Compatibility name for typed-failure callers."""

        return self.settle_preaccounted_work(
            work_id=work_id,
            host_attempt_accounting_ids=host_attempt_accounting_ids,
            now=now,
        )

    def validate_preaccounted_failure(
        self,
        *,
        work_id: str,
        host_attempt_accounting_ids: tuple[str, ...],
    ) -> None:
        """Read-only validation used before committing a typed failure record."""

        if not host_attempt_accounting_ids or len(host_attempt_accounting_ids) != len(
            set(host_attempt_accounting_ids)
        ):
            raise ValueError("preaccounted settlement requires distinct HostAttempt IDs")
        state = self.state()
        if work_id not in state.completed_work_ids and not any(
            work_id in reservation.unsettled_work_ids for reservation in state.open_reservations
        ):
            raise ValueError("terminalized work has no in-flight reservation")
        if not set(host_attempt_accounting_ids) <= set(state.internal_accounting_ids):
            raise ValueError("HostAttempt usage is not already recorded in runtime state")
        for accounting_id in host_attempt_accounting_ids:
            path = self.run_dir / "host-attempt-accounting" / f"{accounting_id}.json"
            if not path.is_file():
                raise ValueError("preaccounted HostAttempt record is missing")
            accounting = HostAttemptAccounting.model_validate_json(path.read_text(encoding="utf-8"))
            if accounting.work_id != work_id:
                raise ValueError("preaccounted HostAttempt belongs to another work")

    def record_internal_usage(
        self,
        *,
        accounting_id: str,
        usage: UsageAllowance,
        now: datetime | None = None,
    ) -> ActiveSessionState:
        """Charge non-Agent proposal/candidate counters without resetting usage."""

        state = self._advance_clock(self.state(), _now(now))
        if accounting_id in state.internal_accounting_ids:
            self._write(state)
            return state
        if state.status is not RuntimeSessionStatus.ACTIVE:
            self._write(state)
            raise BudgetReservationError("runtime is not active for internal accounting")
        projected = state.used.add(self.reserved_usage(state)).add(usage)
        if not projected.within(state.approved):
            self._write(
                state.model_copy(update={"status": RuntimeSessionStatus.AWAITING_CONTINUATION})
            )
            raise BudgetReservationError("internal usage exceeds the approved tranche")
        updated = state.model_copy(
            update={
                "used": state.used.add(usage),
                "internal_accounting_ids": (
                    *state.internal_accounting_ids,
                    accounting_id,
                ),
            }
        )
        self._write(updated)
        return updated

    def record_host_attempt(
        self,
        accounting: HostAttemptAccounting,
        *,
        now: datetime | None = None,
    ) -> ActiveSessionState:
        """Append one real, non-ingested Agent-host context to this run's ledger.

        This is intentionally distinct from both reservation settlement and
        internal proposal/candidate accounting: no work item is marked complete
        and no synthetic submission is created.  Idempotency is content-based
        and the typed record is kept beside the single runtime session state.
        """

        if accounting.run_id != self.run_id or accounting.config_hash != self.config_hash:
            raise ValueError("host attempt accounting belongs to another run/config")
        payload = accounting.model_dump(mode="json")
        identity = {key: value for key, value in payload.items() if key != "accounting_id"}
        expected_id = f"host-attempt-{sha256_bytes(canonical_json_bytes(identity))[:24]}"
        if accounting.accounting_id != expected_id:
            raise ValueError("host attempt accounting_id is not content-addressed")
        path = self.run_dir / "host-attempt-accounting" / f"{accounting.accounting_id}.json"
        state = self._advance_clock(self.state(), _now(now))
        if accounting.accounting_id in state.internal_accounting_ids:
            if not path.is_file():
                raise ValueError("host attempt is listed in state but its record is missing")
            stored = HostAttemptAccounting.model_validate_json(path.read_text(encoding="utf-8"))
            if stored != accounting:
                raise ValueError("append-only host attempt accounting was changed")
            self._write(state)
            return state
        if path.exists():
            raise ValueError("host attempt record exists but is absent from runtime state")
        if state.status is not RuntimeSessionStatus.ACTIVE:
            self._write(state)
            raise BudgetReservationError("runtime is not active for host attempt accounting")
        projected = state.used.add(self.reserved_usage(state)).add(accounting.usage)
        if not projected.within(state.approved):
            self._write(
                state.model_copy(update={"status": RuntimeSessionStatus.AWAITING_CONTINUATION})
            )
            raise BudgetReservationError("host attempt exceeds the approved tranche")
        atomic_write(path, canonical_json_bytes(payload))
        updated = state.model_copy(
            update={
                "used": state.used.add(accounting.usage),
                "cumulative_agent_duration_ms": (
                    state.cumulative_agent_duration_ms + accounting.agent_duration_ms
                ),
                "internal_accounting_ids": (
                    *state.internal_accounting_ids,
                    accounting.accounting_id,
                ),
            }
        )
        self._write(updated)
        return updated

    def pause(
        self,
        *,
        barrier: RuntimeBarrier,
        evidence_hash: str,
        completed_work_ids: tuple[str, ...],
        not_exported_work_ids: tuple[str, ...],
        candidate_gate_summary: dict[str, str],
        next_batch_estimate: UsageAllowance | None,
        continuation_risk_zh: str,
        now: datetime | None = None,
    ) -> BudgetCheckpoint:
        if (
            barrier not in self.policy.required_barriers
            and barrier
            not in {
                RuntimeBarrier.BUDGET_LIMIT,
                RuntimeBarrier.POST_RECOVERY_CHECKPOINT,
            }
        ):
            raise ValueError("checkpoint barrier is not frozen in the runtime policy")
        current = _now(now)
        state = self._advance_clock(self.state(), current)
        is_recovery_checkpoint = barrier is RuntimeBarrier.POST_RECOVERY_CHECKPOINT
        if is_recovery_checkpoint and not state.post_recovery_checkpoint_required:
            raise ValueError("post-recovery checkpoint has no unsettled recovery state")
        if not is_recovery_checkpoint and state.post_recovery_checkpoint_required:
            raise ValueError("recovery ingest requires a fresh post-recovery checkpoint")
        in_progress_work_ids = tuple(
            sorted(
                {
                    work_id
                    for reservation in state.open_reservations
                    for work_id in reservation.unsettled_work_ids
                }
            )
        )
        identity = {
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "barrier": barrier.value,
            "evidence_hash": evidence_hash,
            "previous_decision_hash": state.latest_decision_hash,
            "completed_work_ids": sorted(set(completed_work_ids)),
            "in_progress_work_ids": list(in_progress_work_ids),
            "not_exported_work_ids": sorted(set(not_exported_work_ids)),
            "created_at": current.isoformat(),
        }
        checkpoint_id = (
            f"budget-checkpoint-{sha256_bytes(canonical_json_bytes(identity))[:24]}"
        )
        state = state.model_copy(
            update={
                "status": RuntimeSessionStatus.AWAITING_CONTINUATION,
                "latest_checkpoint_id": checkpoint_id,
                "checkpoint_ids": (*state.checkpoint_ids, checkpoint_id),
                "post_recovery_checkpoint_required": False
                if is_recovery_checkpoint
                else state.post_recovery_checkpoint_required,
                "uncheckpointed_recovery_work_ids": ()
                if is_recovery_checkpoint
                else state.uncheckpointed_recovery_work_ids,
            }
        )
        state_hash = _hash_model(state)
        checkpoint = BudgetCheckpoint(
            checkpoint_id=checkpoint_id,
            run_id=self.run_id,
            config_hash=self.config_hash,
            barrier=barrier,
            state_hash=state_hash,
            evidence_hash=evidence_hash,
            used=state.used.model_copy(
                update={"active_wall_clock_ms": state.active_accumulated_ms}
            ),
            reserved=self.reserved_usage(state),
            approved=state.approved,
            completed_work_ids=tuple(sorted(set(completed_work_ids))),
            in_progress_work_ids=in_progress_work_ids,
            not_exported_work_ids=tuple(sorted(set(not_exported_work_ids))),
            candidate_gate_summary=dict(sorted(candidate_gate_summary.items())),
            next_batch_estimate=next_batch_estimate,
            continuation_risk_zh=continuation_risk_zh,
            previous_decision_hash=state.latest_decision_hash,
            created_at=current,
        )
        path = self.run_dir / "budget-checkpoints" / f"{checkpoint.checkpoint_id}.json"
        payload = canonical_json_bytes(checkpoint.model_dump(mode="json"))
        if path.exists() and path.read_bytes() != payload:
            raise ValueError("append-only checkpoint already differs")
        if not path.exists():
            atomic_write(path, payload)
        self._write(state)
        self._write_review_html(checkpoint)
        return checkpoint

    def apply_decision(
        self,
        decision: BudgetContinuationDecision,
        *,
        now: datetime | None = None,
    ) -> ActiveSessionState:
        state = self.state()
        existing_path = next(
            (self.run_dir / "continuation-decisions").glob(f"*-{decision.decision_id}.json"),
            None,
        )
        if existing_path is not None:
            stored = BudgetContinuationDecision.model_validate_json(
                existing_path.read_text(encoding="utf-8")
            )
            if stored != decision:
                raise ValueError("append-only continuation decision was changed")
            return state
        if state.post_recovery_checkpoint_required:
            raise ValueError("a fresh post-recovery checkpoint is required before continuation")
        if state.status is not RuntimeSessionStatus.AWAITING_CONTINUATION:
            raise ValueError("continuation decision requires an awaiting checkpoint")
        if decision.run_id != self.run_id or decision.config_hash != self.config_hash:
            raise ValueError("continuation decision belongs to another run/config")
        if decision.checkpoint_id != state.latest_checkpoint_id:
            raise ValueError("continuation decision is bound to a stale checkpoint")
        checkpoint_path = self.run_dir / "budget-checkpoints" / f"{decision.checkpoint_id}.json"
        checkpoint = BudgetCheckpoint.model_validate_json(
            checkpoint_path.read_text(encoding="utf-8")
        )
        if checkpoint.state_hash != _hash_model(state):
            raise ValueError("continuation checkpoint is stale for the current runtime state")
        if sha256_bytes(checkpoint_path.read_bytes()) != decision.checkpoint_hash:
            raise ValueError("continuation checkpoint hash mismatch")
        if decision.evidence_hash != checkpoint.evidence_hash:
            raise ValueError("continuation evidence hash mismatch")
        if decision.previous_decision_hash != state.latest_decision_hash:
            raise ValueError("continuation decision chain is stale")
        if not decision.approved_increment.within(self.policy.maximum_continuation_increment):
            raise ValueError("continuation increment exceeds the frozen per-decision bound")
        payload = decision.model_dump(mode="json")
        identity = {key: value for key, value in payload.items() if key != "decision_id"}
        expected_id = f"continuation-{sha256_bytes(canonical_json_bytes(identity))[:24]}"
        if decision.decision_id != expected_id:
            raise ValueError("continuation decision_id is not content-addressed")
        state = self._advance_clock(state, _now(now))
        decision_hash = _hash_model(decision)
        sequence = len(tuple((self.run_dir / "continuation-decisions").glob("*.json"))) + 1
        path = (
            self.run_dir / "continuation-decisions" / f"{sequence:04d}-{decision.decision_id}.json"
        )
        atomic_write(path, canonical_json_bytes(payload))
        if decision.action is ContinuationAction.CONTINUE:
            status = RuntimeSessionStatus.ACTIVE
            approved = state.approved.add(decision.approved_increment)
        elif decision.action is ContinuationAction.STOP_AND_REPORT:
            status = RuntimeSessionStatus.STOPPED
            approved = state.approved
        else:
            status = RuntimeSessionStatus.ABORTED
            approved = state.approved
        updated = state.model_copy(
            update={
                "status": status,
                "approved": approved,
                "latest_decision_hash": decision_hash,
                "last_transition_at": _now(now),
            }
        )
        self._write(updated)
        return updated

    def audit_checkpoint_freshness(
        self,
        checkpoint_id: str,
        *,
        audited_at: datetime | None = None,
    ) -> CheckpointFreshnessAudit:
        """Fail closed unless a checkpoint binds the exact current runtime state."""

        state = self.state()
        checkpoint_path = self.run_dir / "budget-checkpoints" / f"{checkpoint_id}.json"
        if not checkpoint_path.is_file():
            raise ValueError("checkpoint freshness audit cannot find the checkpoint")
        checkpoint_bytes = checkpoint_path.read_bytes()
        checkpoint = BudgetCheckpoint.model_validate_json(checkpoint_bytes)
        if checkpoint.run_id != self.run_id or checkpoint.config_hash != self.config_hash:
            raise ValueError("checkpoint belongs to another run/config")
        if state.latest_checkpoint_id != checkpoint_id:
            raise ValueError("checkpoint is not the latest runtime checkpoint")
        if state.post_recovery_checkpoint_required or state.uncheckpointed_recovery_work_ids:
            raise ValueError("runtime has recovery ingest not covered by a fresh checkpoint")
        runtime_state_hash = _hash_model(state)
        if checkpoint.state_hash != runtime_state_hash:
            raise ValueError("checkpoint state_hash does not match persisted runtime state")
        checkpoint_ref = f"budget-checkpoints/{checkpoint_id}.json"
        checkpoint_sha256 = sha256_bytes(checkpoint_bytes)
        identity = {
            "run_id": self.run_id,
            "config_hash": self.config_hash,
            "checkpoint_id": checkpoint_id,
            "checkpoint_sha256": checkpoint_sha256,
            "runtime_state_hash": runtime_state_hash,
            "evidence_hash": checkpoint.evidence_hash,
        }
        return CheckpointFreshnessAudit(
            audit_id=(
                f"checkpoint-freshness-{sha256_bytes(canonical_json_bytes(identity))[:24]}"
            ),
            run_id=self.run_id,
            config_hash=self.config_hash,
            checkpoint_id=checkpoint_id,
            checkpoint_ref=checkpoint_ref,
            checkpoint_sha256=checkpoint_sha256,
            checkpoint_state_hash=checkpoint.state_hash,
            runtime_state_hash=runtime_state_hash,
            evidence_hash=checkpoint.evidence_hash,
            audited_at=_now(audited_at),
        )

    def reserved_usage(self, state: ActiveSessionState | None = None) -> UsageAllowance:
        current = state or self.state()
        total = UsageAllowance(agent_calls=0, estimated_tokens=0, active_wall_clock_ms=0)
        for reservation in current.open_reservations:
            remaining = len(reservation.unsettled_work_ids)
            if remaining == len(reservation.work_ids):
                share = reservation.upper_bound
            else:
                ratio = remaining / len(reservation.work_ids)
                share = UsageAllowance(
                    **{
                        name: math.ceil(int(getattr(reservation.upper_bound, name)) * ratio)
                        for name in UsageAllowance.model_fields
                    }
                )
            total = total.add(share)
        return total

    def _advance_clock(self, state: ActiveSessionState, current: datetime) -> ActiveSessionState:
        elapsed = _elapsed_ms(state.last_transition_at, current)
        updates: dict[str, object] = {"last_transition_at": current}
        if state.status is RuntimeSessionStatus.ACTIVE:
            active = state.active_accumulated_ms + elapsed
            updates["active_accumulated_ms"] = active
            updates["used"] = state.used.model_copy(update={"active_wall_clock_ms": active})
        elif state.status in {
            RuntimeSessionStatus.PAUSED,
            RuntimeSessionStatus.AWAITING_CONTINUATION,
        }:
            updates["paused_accumulated_ms"] = state.paused_accumulated_ms + elapsed
        return state.model_copy(update=updates)

    def _write(self, state: ActiveSessionState) -> None:
        atomic_write(self.state_path, canonical_json_bytes(state.model_dump(mode="json")))
        self._sync_owner_lifecycle(state)

    def _sync_owner_lifecycle(self, state: ActiveSessionState) -> None:
        """Keep the owning strict run checkpoint current across nested Eval calls."""

        from gepase.run_lifecycle import (
            RunLifecycle,
            RunLifecycleRecord,
            RunLifecycleStatus,
        )

        record_path = self.run_dir / RunLifecycle.RECORD_NAME
        if not record_path.is_file():
            return
        record = RunLifecycleRecord.model_validate_json(record_path.read_text(encoding="utf-8"))
        if record.config_hash != self.config_hash or record.run_id != self.run_id:
            raise ValueError("runtime session and owner lifecycle identity disagree")
        if record.owner == "evolution":
            critical = (
                "resolved-config.json",
                "evolution-state.json",
                "candidates.sqlite3",
                "checkpoint.json",
                self.STATE_NAME,
            )
        else:
            values = ["run-metadata.json", "ledger.sqlite3", "ledger-snapshot.json"]
            for relative in (
                "resolved-reference-config.json",
                "runtime-budget-binding.json",
                self.STATE_NAME,
            ):
                if (self.run_dir / relative).is_file():
                    values.append(relative)
            critical = tuple(values)
        if any(not (self.run_dir / relative).is_file() for relative in critical):
            return
        lifecycle_status = (
            RunLifecycleStatus.PAUSED
            if state.status
            in {
                RuntimeSessionStatus.PAUSED,
                RuntimeSessionStatus.AWAITING_CONTINUATION,
                RuntimeSessionStatus.STOPPED,
            }
            else RunLifecycleStatus.ABORTED
            if state.status is RuntimeSessionStatus.ABORTED
            else RunLifecycleStatus.COMPLETE
            if state.status is RuntimeSessionStatus.COMPLETE
            else RunLifecycleStatus.ACTIVE
        )
        RunLifecycle(
            self.run_dir,
            run_id=self.run_id,
            owner=record.owner,
            expected_config_hash=self.config_hash,
        ).checkpoint(
            config_hash=self.config_hash,
            status=lifecycle_status,
            critical_files=critical,
        )

    def _write_review_html(self, checkpoint: BudgetCheckpoint) -> None:
        html = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>GEPASE 运行检查点</title>
<style>
body{{font:16px/1.65 system-ui;max-width:960px;margin:40px auto;padding:0 24px;color:#18302b}}
code{{background:#eef5f2;padding:2px 6px}}
table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #ccd9d4;padding:8px;text-align:left}}
</style>
<h1>GEPASE 运行检查点</h1>
<p>运行 <code>{checkpoint.run_id}</code> 已在 <b>{checkpoint.barrier.value}</b> 暂停。
此页面只读, 不能修改 Core 状态。</p>
<table>
<tr><th>已使用调用</th><td>{checkpoint.used.agent_calls}</td></tr>
<tr><th>已使用估算 Token</th><td>{checkpoint.used.estimated_tokens}</td></tr>
<tr><th>Active 时间</th><td>{checkpoint.used.active_wall_clock_ms} ms</td></tr>
<tr><th>在途 work</th><td>{len(checkpoint.in_progress_work_ids)}</td></tr>
<tr><th>未导出 work</th><td>{len(checkpoint.not_exported_work_ids)}</td></tr>
</table>
<h2>继续风险</h2><p>{checkpoint.continuation_risk_zh}</p>
<p>继续、停止并报告或在效果声明前中止, 都必须提交绑定本
checkpoint/config/evidence hash 的追加式决定。</p></html>"""
        atomic_write(
            self.run_dir / "budget-checkpoints" / f"{checkpoint.checkpoint_id}.html",
            html.encode("utf-8"),
        )


def build_continuation_decision(
    checkpoint: BudgetCheckpoint,
    checkpoint_path: Path,
    *,
    action: ContinuationAction,
    approved_increment: UsageAllowance,
    reviewer: str,
    comment_zh: str,
    decided_at: datetime | None = None,
) -> BudgetContinuationDecision:
    payload = {
        "schema_version": "1.0.0",
        "run_id": checkpoint.run_id,
        "config_hash": checkpoint.config_hash,
        "checkpoint_id": checkpoint.checkpoint_id,
        "checkpoint_hash": sha256_bytes(checkpoint_path.read_bytes()),
        "evidence_hash": checkpoint.evidence_hash,
        "action": action,
        "approved_increment": approved_increment.model_dump(mode="json"),
        "reviewer": reviewer,
        "comment_zh": comment_zh,
        "previous_decision_hash": checkpoint.previous_decision_hash,
        "decided_at": _now(decided_at).isoformat(),
    }
    draft = BudgetContinuationDecision(decision_id="pending", **payload)
    identity = draft.model_dump(mode="json", exclude={"decision_id"})
    decision_id = f"continuation-{sha256_bytes(canonical_json_bytes(identity))[:24]}"
    return draft.model_copy(update={"decision_id": decision_id})


def build_host_attempt_accounting(
    *,
    run_id: str,
    config_hash: str,
    role: str,
    host_task_id: str,
    context_id: str,
    work_id: str | None,
    reason: HostAttemptReason,
    usage: UsageAllowance,
    token_count_kind: MeasurementKind,
    agent_duration_ms: int,
    duration_kind: MeasurementKind,
    reason_zh: str,
    evidence_refs: tuple[str, ...],
    recorded_at: datetime | None = None,
) -> HostAttemptAccounting:
    """Build a content-addressed record for an already-observed host context."""

    payload = {
        "schema_version": "1.0.0",
        "run_id": run_id,
        "config_hash": config_hash,
        "role": role,
        "host_task_id": host_task_id,
        "context_id": context_id,
        "work_id": work_id,
        "reason": reason,
        "usage": usage.model_dump(mode="json"),
        "token_count_kind": token_count_kind,
        "agent_duration_ms": agent_duration_ms,
        "duration_kind": duration_kind,
        "reason_zh": reason_zh,
        "evidence_refs": evidence_refs,
        "recorded_at": _now(recorded_at).isoformat(),
    }
    draft = HostAttemptAccounting(accounting_id="pending", **payload)
    identity = draft.model_dump(mode="json", exclude={"accounting_id"})
    accounting_id = f"host-attempt-{sha256_bytes(canonical_json_bytes(identity))[:24]}"
    return draft.model_copy(update={"accounting_id": accounting_id})
