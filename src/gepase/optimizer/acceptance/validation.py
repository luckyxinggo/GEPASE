"""Gate 3 held-out E2/E3 paired validation with category regression floors."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from pydantic import Field

from gepase.evals.schema import EvidenceTier
from gepase.evals.scores import TaskScoreVector
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
    task_score_secondary_minimum_effect: dict[str, float] = Field(
        default_factory=lambda: {"task_score_efficiency": 0.02}
    )
    secondary_regression_floor: float = Field(default=-0.01, le=0)
    bootstrap_samples: int = Field(default=5_000, ge=100)
    seed: int = 42


class ValidationGateDecision(FrozenModel):
    gate: GateResult
    statistics: PairedStatistics
    category_deltas: dict[str, float]
    minimum_tier_complete: bool


class TaskScoreSecondaryEvidence(FrozenModel):
    """Recomputable mapping from paired vectors to Gate secondary objectives."""

    schema_version: str = "1.0.0"
    source_axis: str = "TaskScoreVector.efficiency"
    aggregate: str = "mean_paired_delta"
    primary_axes: tuple[str, str] = ("task_correctness", "output_quality")
    improvements: dict[str, float]
    per_task_deltas: dict[str, float]
    evidence_refs: tuple[str, ...]


def _task_score_vector(project_root: Path, reference: str) -> TaskScoreVector:
    relative = Path(reference)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("TaskScoreVector evidence ref must be repository-relative")
    path = (project_root.resolve() / relative).resolve(strict=True)
    if not path.is_relative_to(project_root.resolve()):
        raise ValueError("TaskScoreVector evidence ref escapes the project")
    return TaskScoreVector.model_validate_json(path.read_text(encoding="utf-8"))


def derive_task_score_secondary_evidence(
    project_root: Path,
    rows: tuple[PairedScore, ...],
) -> TaskScoreSecondaryEvidence:
    """Derive the registered efficiency objective without reusing primary quality axes."""

    if not rows:
        raise ValueError("secondary objective derivation requires paired TaskScoreVectors")
    per_task: dict[str, float] = {}
    evidence_refs: set[str] = set()
    for row in rows:
        parent = _task_score_vector(project_root, row.parent_record_id)
        candidate = _task_score_vector(project_root, row.candidate_record_id)
        if parent.task_id != row.task_id or candidate.task_id != row.task_id:
            raise ValueError("TaskScoreVector task does not match its PairedScore")
        # The parent reference and candidate are separate executions and therefore
        # normally have different execution pair ids.  Their typed PairedScore refs
        # are the binding; task, variant, and scoring-policy checks keep that binding
        # fail-closed without inventing a shared execution identity.
        if row.parent_record_id == row.candidate_record_id:
            raise ValueError("secondary evidence cannot reuse one TaskScoreVector twice")
        if candidate.variant != "candidate" or parent.variant == "no-skill":
            raise ValueError("secondary evidence requires candidate versus Skill parent vectors")
        if parent.scoring_policy_ref != candidate.scoring_policy_ref:
            raise ValueError("paired TaskScoreVectors use different scoring policies")
        if row.task_id in per_task:
            raise ValueError("secondary evidence contains duplicate task ids")
        per_task[row.task_id] = candidate.efficiency - parent.efficiency
        evidence_refs.update((row.parent_record_id, row.candidate_record_id))
    improvement = _mean(list(per_task.values()))
    return TaskScoreSecondaryEvidence(
        improvements={"task_score_efficiency": improvement},
        per_task_deltas=dict(sorted(per_task.items())),
        evidence_refs=tuple(sorted(evidence_refs)),
    )


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
    registered_secondary = {
        **policy.secondary_minimum_effect,
        **policy.task_score_secondary_minimum_effect,
    }
    unknown_secondary = set(secondary) - set(registered_secondary)
    if unknown_secondary:
        raise ValueError(f"unregistered secondary objectives: {sorted(unknown_secondary)}")
    secondary_regression = any(
        value < policy.secondary_regression_floor for value in secondary.values()
    )
    secondary_wins = tuple(
        sorted(
            key for key, value in secondary.items() if value >= registered_secondary[key]
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
                "secondary_minimum_effect": registered_secondary,
                "secondary_axis_distinct_from_category": {
                    "axis": "TaskScoreVector.efficiency",
                    "category_example": "quality_efficiency",
                },
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
