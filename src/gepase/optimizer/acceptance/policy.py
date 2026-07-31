"""Conservative and exploratory verdict policies over Gate 0-3."""

from __future__ import annotations

from pydantic import Field

from gepase.evals.variance import VarianceAction, VarianceDecision
from gepase.optimizer.acceptance.models import (
    AcceptancePolicyKind,
    GateLevel,
    GateOutcome,
    GateResult,
)
from gepase.optimizer.status import CandidateStatus
from gepase.schemas.common import FrozenModel


class AcceptancePolicy(FrozenModel):
    kind: AcceptancePolicyKind = AcceptancePolicyKind.CONSERVATIVE
    maximum_efficiency_regression: float = Field(default=0.15, ge=0)
    maximum_complexity_regression: float = Field(default=0.15, ge=0)


class PolicyVerdict(FrozenModel):
    verdict: CandidateStatus
    reason_codes: tuple[str, ...] = Field(min_length=1)
    human_summary: str
    frontier_eligible: bool
    exploration_pool_eligible: bool
    gate_4: GateResult


def decide_acceptance(
    gates: tuple[GateResult, ...],
    *,
    policy: AcceptancePolicy,
    variance: VarianceDecision | None = None,
    efficiency_regression: float = 0.0,
    complexity_regression: float = 0.0,
    relative_efficiency_v2_active: bool = False,
) -> PolicyVerdict:
    by_level = {item.level: item for item in gates}
    gate0 = by_level.get(GateLevel.GATE_0_SCHEMA)
    gate1 = by_level.get(GateLevel.GATE_1_STATIC)
    gate2 = by_level.get(GateLevel.GATE_2_MINIBATCH)
    gate3 = by_level.get(GateLevel.GATE_3_VALIDATION)
    if gate0 is None or gate0.outcome is GateOutcome.FAILED:
        gate0_reasons = set(gate0.reason_codes if gate0 else ())
        if gate0_reasons & {
            "stale_parent",
            "stale_precondition",
            "rejected_patch_repetition",
        }:
            verdict = CandidateStatus.REJECTED
            reasons = tuple(sorted(gate0_reasons)) or ("gate_0_rejected",)
        else:
            verdict = CandidateStatus.INVALID
            reasons = ("gate_0_invalid",)
    elif gate1 is None or gate1.outcome is GateOutcome.FAILED:
        verdict = CandidateStatus.REJECTED
        reasons = ("gate_1_static_failure",)
    elif gate2 is None or gate2.outcome is GateOutcome.FAILED:
        verdict = CandidateStatus.REJECTED
        reasons = ("gate_2_minibatch_regression",)
    elif gate3 is None or gate3.outcome is GateOutcome.NOT_RUN:
        verdict = CandidateStatus.INCONCLUSIVE
        reasons = ("gate_3_not_run",)
    elif gate3.outcome is GateOutcome.FAILED:
        verdict = CandidateStatus.REJECTED
        reasons = tuple(gate3.reason_codes)
    elif gate3.outcome is GateOutcome.INCONCLUSIVE:
        reasons = tuple(gate3.reason_codes)
        verdict = (
            CandidateStatus.REJECTED
            if "held_out_no_strict_improvement" in reasons
            else CandidateStatus.INCONCLUSIVE
        )
    elif variance and variance.action is not VarianceAction.STABLE:
        verdict = CandidateStatus.INCONCLUSIVE
        reasons = tuple(variance.reason_codes)
    elif (
        not relative_efficiency_v2_active
        and efficiency_regression > policy.maximum_efficiency_regression
    ):
        verdict = CandidateStatus.REJECTED
        reasons = ("efficiency_regression",)
    elif complexity_regression > policy.maximum_complexity_regression:
        verdict = CandidateStatus.REJECTED
        reasons = ("complexity_regression",)
    else:
        verdict = CandidateStatus.ACCEPTED
        reasons = ("all_gates_passed", "held_out_strict_improvement")
    frontier = verdict is CandidateStatus.ACCEPTED
    exploration = (
        policy.kind is AcceptancePolicyKind.EXPLORATORY and verdict is CandidateStatus.INCONCLUSIVE
    )
    outcome = (
        GateOutcome.PASSED
        if frontier
        else (
            GateOutcome.INCONCLUSIVE
            if verdict is CandidateStatus.INCONCLUSIVE
            else GateOutcome.FAILED
        )
    )
    return PolicyVerdict(
        verdict=verdict,
        reason_codes=reasons,
        human_summary=(
            "Candidate accepted into the deployable frontier."
            if frontier
            else (
                "Candidate remains isolated pending stronger evidence."
                if verdict is CandidateStatus.INCONCLUSIVE
                else "Candidate rejected and excluded from the frontier."
            )
        ),
        frontier_eligible=frontier,
        exploration_pool_eligible=exploration,
        gate_4=GateResult(
            level=GateLevel.GATE_4_FRONTIER,
            outcome=outcome,
            reason_codes=reasons,
            human_summary=(
                "Frontier admission granted."
                if frontier
                else "Frontier admission denied or deferred."
            ),
            checks={
                "policy": policy.kind.value,
                "efficiency_regression": efficiency_regression,
                "relative_efficiency_v2_active": relative_efficiency_v2_active,
                "v1_maximum_efficiency_regression_used": not relative_efficiency_v2_active,
                "complexity_regression": complexity_regression,
                "frontier_eligible": frontier,
                "exploration_pool_eligible": exploration,
            },
            target_calls=0,
        ),
    )
