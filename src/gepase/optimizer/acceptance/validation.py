"""Gate 3 held-out E2/E3 paired validation with category regression floors."""

from __future__ import annotations

from collections import defaultdict

from pydantic import Field

from gepase.evals.schema import EvidenceTier
from gepase.evals.statistics import PairedScore, PairedStatistics, paired_statistics
from gepase.optimizer.acceptance.models import (
    GateLevel,
    GateOutcome,
    GateResult,
    GateUsage,
)
from gepase.schemas.common import FrozenModel

_TIER_RANK = {
    EvidenceTier.E0_STATIC.value: 0,
    EvidenceTier.E1_SIMULATED.value: 1,
    EvidenceTier.E2_DELEGATED.value: 2,
    EvidenceTier.E3_EXECUTABLE.value: 3,
}


class ValidationPolicy(FrozenModel):
    minimum_primary_delta: float = 0.0
    quality_noninferiority_margin: float = Field(default=0.0, ge=0)
    category_regression_floor: float = Field(default=-0.05, le=0)
    high_risk_regression_floor: float = Field(default=0.0, le=0)
    secondary_minimum_effect: dict[str, float] = Field(
        default_factory=lambda: {
            "latency": 0.02,
            "tokens": 0.02,
            "tool_calls": 0.02,
            "complexity": 0.02,
        }
    )
    secondary_regression_floor: float = Field(default=-0.01, le=0)
    bootstrap_samples: int = Field(default=5_000, ge=100)
    seed: int = 42


class ValidationGateDecision(FrozenModel):
    gate: GateResult
    statistics: PairedStatistics
    category_deltas: dict[str, float]
    minimum_tier_complete: bool


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def run_validation_gate(
    rows: tuple[PairedScore, ...],
    *,
    policy: ValidationPolicy,
    secondary_objective_improvements: dict[str, float] | None = None,
    secondary_evidence_refs: tuple[str, ...] = (),
) -> ValidationGateDecision:
    if not rows:
        raise ValueError("Gate 3 requires held-out paired rows")
    if any(item.task_id.startswith("test-") for item in rows):
        raise ValueError("Gate 3 cannot access the test split")
    tier_complete = all(
        _TIER_RANK.get(item.evidence_tier, -1) >= _TIER_RANK.get(item.minimum_acceptance_tier, 999)
        for item in rows
    )
    statistics = paired_statistics(
        rows, seed=policy.seed, bootstrap_samples=policy.bootstrap_samples
    )
    by_category: defaultdict[str, list[float]] = defaultdict(list)
    by_risk: defaultdict[str, list[float]] = defaultdict(list)
    for item in rows:
        by_category[item.category].append(item.delta)
        by_risk[item.risk_level].append(item.delta)
    category_deltas = {key: _mean(value) for key, value in sorted(by_category.items())}
    risk_deltas = {key: _mean(value) for key, value in sorted(by_risk.items())}
    category_regression = any(
        value < policy.category_regression_floor for value in category_deltas.values()
    )
    high_risk_regression = any(
        value < policy.high_risk_regression_floor
        for key, value in risk_deltas.items()
        if key in {"high", "critical"}
    )
    secondary = secondary_objective_improvements or {}
    unknown_secondary = set(secondary) - set(policy.secondary_minimum_effect)
    if unknown_secondary:
        raise ValueError(f"unregistered secondary objectives: {sorted(unknown_secondary)}")
    secondary_regression = any(
        value < policy.secondary_regression_floor for value in secondary.values()
    )
    secondary_wins = tuple(
        sorted(
            key for key, value in secondary.items() if value >= policy.secondary_minimum_effect[key]
        )
    )
    quality_noninferior = statistics.mean_delta >= -policy.quality_noninferiority_margin
    if not tier_complete:
        outcome = GateOutcome.INCONCLUSIVE
        reasons = ("minimum_acceptance_tier_missing",)
    elif category_regression or high_risk_regression or secondary_regression:
        outcome = GateOutcome.FAILED
        reasons = ("protected_objective_regression",)
    elif statistics.mean_delta > policy.minimum_primary_delta:
        outcome = GateOutcome.PASSED
        reasons = (
            "pareto_route_a_primary_improved",
            "regression_floors_satisfied",
        )
    elif quality_noninferior and secondary_wins:
        outcome = GateOutcome.PASSED
        reasons = (
            "pareto_route_b_quality_noninferior",
            "secondary_objective_strictly_improved",
            *tuple(f"secondary_win:{key}" for key in secondary_wins),
        )
    elif statistics.mean_delta == policy.minimum_primary_delta:
        outcome = GateOutcome.INCONCLUSIVE
        reasons = ("held_out_no_strict_improvement",)
    else:
        outcome = GateOutcome.FAILED
        reasons = ("held_out_primary_regression",)
    return ValidationGateDecision(
        gate=GateResult(
            level=GateLevel.GATE_3_VALIDATION,
            outcome=outcome,
            reason_codes=reasons,
            human_summary={
                GateOutcome.PASSED: (
                    "Held-out E2/E3 validation strictly improved without floor violations."
                ),
                GateOutcome.FAILED: (
                    "Held-out validation regressed overall or within a protected category."
                ),
                GateOutcome.INCONCLUSIVE: "Held-out validation is insufficient for acceptance.",
                GateOutcome.NOT_RUN: "Held-out validation was not run.",
            }[outcome],
            evidence_refs=tuple(
                sorted(
                    {
                        *(item.parent_record_id for item in rows),
                        *(item.candidate_record_id for item in rows),
                        *secondary_evidence_refs,
                    }
                )
            ),
            checks={
                "minimum_tier_complete": tier_complete,
                "mean_delta": statistics.mean_delta,
                "bootstrap_95_ci": statistics.bootstrap_95_ci,
                "category_deltas": category_deltas,
                "risk_deltas": risk_deltas,
                "category_regression_floor": policy.category_regression_floor,
                "high_risk_regression_floor": policy.high_risk_regression_floor,
                "quality_noninferiority_margin": policy.quality_noninferiority_margin,
                "quality_noninferior": quality_noninferior,
                "secondary_objective_improvements": secondary,
                "secondary_minimum_effect": policy.secondary_minimum_effect,
                "secondary_regression_floor": policy.secondary_regression_floor,
                "secondary_wins": secondary_wins,
                "pareto_route": (
                    "A"
                    if outcome is GateOutcome.PASSED
                    and "pareto_route_a_primary_improved" in reasons
                    else (
                        "B"
                        if outcome is GateOutcome.PASSED
                        and "pareto_route_b_quality_noninferior" in reasons
                        else None
                    )
                ),
            },
            usage=GateUsage(
                metric_calls=len(rows) * 2,
                e2_calls=sum(item.evidence_tier == "E2" for item in rows) * 2,
                e3_calls=sum(item.evidence_tier == "E3" for item in rows) * 2,
            ),
            target_calls=len(rows) * 2,
        ),
        statistics=statistics,
        category_deltas=category_deltas,
        minimum_tier_complete=tier_complete,
    )
