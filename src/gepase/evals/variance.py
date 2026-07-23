"""Bounded re-evaluation scheduling for high-variance paired evidence."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from gepase.evals.statistics import PairedStatistics
from gepase.schemas.common import FrozenModel


class VarianceAction(StrEnum):
    STABLE = "stable"
    REEVALUATE = "reevaluate"
    EXHAUSTED_INCONCLUSIVE = "exhausted_inconclusive"


class VariancePolicy(FrozenModel):
    max_std_delta: float = Field(default=0.2, ge=0)
    max_mean_uncertainty: float = Field(default=0.25, ge=0, le=1)
    max_reevaluations: int = Field(default=2, ge=0, le=10)
    require_positive_ci_for_stochastic_acceptance: bool = True


class VarianceDecision(FrozenModel):
    schema_version: str = "1.0.0"
    action: VarianceAction
    reason_codes: tuple[str, ...] = Field(min_length=1)
    reevaluations_used: int = Field(ge=0)
    reevaluations_remaining: int = Field(ge=0)


def variance_decision(
    statistics: PairedStatistics,
    *,
    mean_uncertainty: float,
    reevaluations_used: int,
    policy: VariancePolicy,
) -> VarianceDecision:
    reasons: list[str] = []
    if statistics.std_delta > policy.max_std_delta:
        reasons.append("high_delta_variance")
    if mean_uncertainty > policy.max_mean_uncertainty:
        reasons.append("high_evidence_uncertainty")
    low, high = statistics.bootstrap_95_ci
    if low <= 0 <= high and statistics.mean_delta != 0:
        reasons.append("confidence_interval_crosses_zero")
    unstable = bool(reasons)
    remaining = max(0, policy.max_reevaluations - reevaluations_used)
    if not unstable:
        return VarianceDecision(
            action=VarianceAction.STABLE,
            reason_codes=("variance_within_policy",),
            reevaluations_used=reevaluations_used,
            reevaluations_remaining=remaining,
        )
    if remaining:
        return VarianceDecision(
            action=VarianceAction.REEVALUATE,
            reason_codes=tuple(reasons),
            reevaluations_used=reevaluations_used,
            reevaluations_remaining=remaining,
        )
    return VarianceDecision(
        action=VarianceAction.EXHAUSTED_INCONCLUSIVE,
        reason_codes=tuple((*reasons, "reevaluation_budget_exhausted")),
        reevaluations_used=reevaluations_used,
        reevaluations_remaining=0,
    )
