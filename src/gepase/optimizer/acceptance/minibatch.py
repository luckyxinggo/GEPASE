"""Gate 2 paired E2/E3 train minibatch screening."""

from __future__ import annotations

from pydantic import Field

from gepase.evals.statistics import PairedScore, PairedStatistics, paired_statistics
from gepase.optimizer.acceptance.models import (
    GateLevel,
    GateOutcome,
    GateResult,
    GateUsage,
)
from gepase.schemas.common import FrozenModel


class MinibatchPolicy(FrozenModel):
    minimum_mean_delta: float = 0.0
    rejection_delta: float = Field(default=-0.02, le=0)
    maximum_loss_fraction: float = Field(default=0.25, ge=0, le=1)
    bootstrap_samples: int = Field(default=2_000, ge=100)
    seed: int = 42


class MinibatchGateDecision(FrozenModel):
    gate: GateResult
    statistics: PairedStatistics
    promote_to_validation: bool
    high_fidelity_recommended: bool


def run_minibatch_gate(
    rows: tuple[PairedScore, ...],
    *,
    policy: MinibatchPolicy,
) -> MinibatchGateDecision:
    if not rows:
        raise ValueError("Gate 2 requires paired minibatch rows")
    if any(item.evidence_tier not in {"E2", "E3"} for item in rows):
        raise ValueError("Gate 2 accepts only real E2/E3 train evidence")
    statistics = paired_statistics(
        rows, seed=policy.seed, bootstrap_samples=policy.bootstrap_samples
    )
    loss_fraction = statistics.losses / statistics.n
    failed = (
        statistics.mean_delta < policy.rejection_delta
        or loss_fraction > policy.maximum_loss_fraction
    )
    promising = statistics.mean_delta >= policy.minimum_mean_delta and not failed
    uncertainty = sum(item.uncertainty for item in rows) / len(rows)
    high_fidelity = (
        uncertainty >= 0.2 or statistics.bootstrap_95_ci[0] <= 0 <= statistics.bootstrap_95_ci[1]
    )
    outcome = GateOutcome.PASSED if promising else GateOutcome.FAILED
    reason = (
        ("minibatch_regression",)
        if failed
        else (
            "minibatch_promising",
            "held_out_validation_required",
        )
        if promising
        else ("train_no_strict_improvement",)
    )
    gate = GateResult(
        level=GateLevel.GATE_2_MINIBATCH,
        outcome=outcome,
        reason_codes=reason,
        human_summary=(
            "Paired train evidence strictly improves enough to enter held-out validation."
            if promising
            else (
                "Paired train evidence shows regression beyond the admission policy."
                if failed
                else "Paired train evidence has no pre-registered strict improvement."
            )
        ),
        evidence_refs=tuple(
            sorted(
                {
                    *(item.parent_record_id for item in rows),
                    *(item.candidate_record_id for item in rows),
                }
            )
        ),
        checks={
            "mean_delta": statistics.mean_delta,
            "minimum_mean_delta": policy.minimum_mean_delta,
            "loss_fraction": loss_fraction,
            "bootstrap_95_ci": statistics.bootstrap_95_ci,
            "mean_uncertainty": uncertainty,
            "real_train_evidence_only": True,
        },
        usage=GateUsage(
            metric_calls=len(rows) * 2,
            e2_calls=sum(item.evidence_tier == "E2" for item in rows) * 2,
            e3_calls=sum(item.evidence_tier == "E3" for item in rows) * 2,
        ),
        target_calls=len(rows) * 2,
    )
    return MinibatchGateDecision(
        gate=gate,
        statistics=statistics,
        promote_to_validation=promising,
        high_fidelity_recommended=high_fidelity,
    )
