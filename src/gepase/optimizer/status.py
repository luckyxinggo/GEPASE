"""Append-only candidate acceptance state machine."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field, model_validator

from gepase.schemas.common import FrozenModel


class CandidateStatus(StrEnum):
    SEED = "seed"
    PROPOSED = "proposed"
    INVALID = "invalid"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    ACCEPTED = "accepted"


_ALLOWED = {
    CandidateStatus.PROPOSED: {
        CandidateStatus.INVALID,
        CandidateStatus.REJECTED,
        CandidateStatus.INCONCLUSIVE,
        CandidateStatus.ACCEPTED,
    },
    # INCONCLUSIVE is a deferred evidence decision, not a terminal state.
    # Once the missing held-out evidence arrives it may resolve strictly to a
    # deployable acceptance or a rejection. INVALID/REJECTED remain terminal.
    CandidateStatus.INCONCLUSIVE: {
        CandidateStatus.REJECTED,
        CandidateStatus.ACCEPTED,
    },
}


class CandidateStatusEvent(FrozenModel):
    schema_version: str = "1.0.0"
    candidate_id: str
    from_status: CandidateStatus
    to_status: CandidateStatus
    reason_code: str = Field(min_length=1)
    gate_decision_id: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @model_validator(mode="after")
    def legal_transition(self) -> CandidateStatusEvent:
        if self.to_status not in _ALLOWED.get(self.from_status, set()):
            raise ValueError(f"illegal candidate transition: {self.from_status}->{self.to_status}")
        return self


def transition_candidate(
    candidate_id: str,
    current: CandidateStatus,
    target: CandidateStatus,
    *,
    reason_code: str,
    gate_decision_id: str | None = None,
) -> CandidateStatusEvent:
    return CandidateStatusEvent(
        candidate_id=candidate_id,
        from_status=current,
        to_status=target,
        reason_code=reason_code,
        gate_decision_id=gate_decision_id,
    )
