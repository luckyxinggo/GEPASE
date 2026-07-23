from __future__ import annotations

import pytest
from pydantic import ValidationError

from gepase.optimizer.status import CandidateStatus, transition_candidate


def test_only_proposed_candidate_can_reach_terminal_gate_status() -> None:
    event = transition_candidate(
        "candidate-1",
        CandidateStatus.PROPOSED,
        CandidateStatus.ACCEPTED,
        reason_code="all_gates_passed",
    )
    assert event.to_status is CandidateStatus.ACCEPTED
    with pytest.raises(ValidationError):
        transition_candidate(
            "candidate-1",
            CandidateStatus.REJECTED,
            CandidateStatus.ACCEPTED,
            reason_code="illegal_revival",
        )


def test_inconclusive_candidate_can_resolve_after_held_out_evidence() -> None:
    accepted = transition_candidate(
        "candidate-deferred",
        CandidateStatus.INCONCLUSIVE,
        CandidateStatus.ACCEPTED,
        reason_code="held_out_strict_improvement",
    )
    rejected = transition_candidate(
        "candidate-deferred",
        CandidateStatus.INCONCLUSIVE,
        CandidateStatus.REJECTED,
        reason_code="held_out_regression",
    )
    assert accepted.from_status is CandidateStatus.INCONCLUSIVE
    assert accepted.to_status is CandidateStatus.ACCEPTED
    assert rejected.to_status is CandidateStatus.REJECTED

    with pytest.raises(ValidationError):
        transition_candidate(
            "candidate-deferred",
            CandidateStatus.INCONCLUSIVE,
            CandidateStatus.INVALID,
            reason_code="invalid_is_not_a_late_behavioral_decision",
        )
