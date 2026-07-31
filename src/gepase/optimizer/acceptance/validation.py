"""Gate 3 held-out E2/E3 paired validation with category regression floors."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Literal

from pydantic import Field, model_validator

from gepase.evals.evidence import EvaluationRecord
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
from gepase.store.artifacts import canonical_json_bytes, sha256_bytes

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


RelativeEfficiencyAxis = Literal[
    "duration_ms", "tool_calls", "tokens", "artifact_size_bytes"
]


class RelativeEfficiencyPolicy(FrozenModel):
    """Versioned held-out resource policy measured relative to original Skill."""

    schema_version: Literal["2.0.0"] = "2.0.0"
    policy_id: Literal["relative_efficiency_v2"] = "relative_efficiency_v2"
    comparable_axes: tuple[RelativeEfficiencyAxis, ...] = Field(
        default=(
            "duration_ms",
            "tool_calls",
            "tokens",
        ),
        min_length=1,
    )
    artifact_size_mode: Literal["report_only", "registered_constraint"] = "report_only"
    max_relative_cost_ratio: float = Field(default=2.0, gt=0)
    aggregate: Literal["median_per_axis_then_equal_weight_mean"] = (
        "median_per_axis_then_equal_weight_mean"
    )
    score_mapping: Literal["one_over_one_plus_ratio"] = "one_over_one_plus_ratio"
    frontier_objectives: tuple[str, str] = (
        "validation_primary_delta",
        "relative_efficiency_score",
    )
    frontier_method: Literal["pareto_layers"] = "pareto_layers"
    frontier_tie_break: tuple[str, str, str] = (
        "validation_primary_delta_desc",
        "relative_efficiency_score_desc",
        "candidate_id_asc",
    )
    unknown_efficiency_policy: Literal[
        "no_cross_availability_dominance_known_first_on_tie"
    ] = "no_cross_availability_dominance_known_first_on_tie"
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def hash_and_axis_contract(self) -> RelativeEfficiencyPolicy:
        if len(self.comparable_axes) != len(set(self.comparable_axes)):
            raise ValueError("relative-efficiency axes must be unique")
        if (
            "artifact_size_bytes" in self.comparable_axes
            and self.artifact_size_mode != "registered_constraint"
        ):
            raise ValueError("artifact size is report-only unless explicitly registered")
        payload = self.model_dump(mode="json", exclude={"policy_hash"})
        if sha256_bytes(canonical_json_bytes(payload)) != self.policy_hash:
            raise ValueError("relative-efficiency policy hash does not match its payload")
        return self


def build_relative_efficiency_policy(
    *,
    comparable_axes: tuple[RelativeEfficiencyAxis, ...] = (
        "duration_ms",
        "tool_calls",
        "tokens",
    ),
    artifact_size_mode: Literal["report_only", "registered_constraint"] = "report_only",
    max_relative_cost_ratio: float = 2.0,
) -> RelativeEfficiencyPolicy:
    payload = {
        "schema_version": "2.0.0",
        "policy_id": "relative_efficiency_v2",
        "comparable_axes": list(comparable_axes),
        "artifact_size_mode": artifact_size_mode,
        "max_relative_cost_ratio": max_relative_cost_ratio,
        "aggregate": "median_per_axis_then_equal_weight_mean",
        "score_mapping": "one_over_one_plus_ratio",
        "frontier_objectives": [
            "validation_primary_delta",
            "relative_efficiency_score",
        ],
        "frontier_method": "pareto_layers",
        "frontier_tie_break": [
            "validation_primary_delta_desc",
            "relative_efficiency_score_desc",
            "candidate_id_asc",
        ],
        "unknown_efficiency_policy": (
            "no_cross_availability_dominance_known_first_on_tie"
        ),
    }
    return RelativeEfficiencyPolicy(
        **payload,
        policy_hash=sha256_bytes(canonical_json_bytes(payload)),
    )


class RelativeEfficiencyAxisObservation(FrozenModel):
    axis: RelativeEfficiencyAxis
    candidate_value: float | None = Field(default=None, ge=0)
    original_value: float | None = Field(default=None, ge=0)
    candidate_measurement_kind: str | None = None
    original_measurement_kind: str | None = None
    included: bool
    exclusion_reason: Literal[
        "missing",
        "zero_original",
        "measurement_kind_mismatch",
        "unavailable",
        "report_only",
    ] | None = None
    ratio: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def inclusion_is_complete(self) -> RelativeEfficiencyAxisObservation:
        if self.included:
            if self.ratio is None or self.exclusion_reason is not None:
                raise ValueError("included relative-efficiency axis requires only a ratio")
        elif self.ratio is not None or self.exclusion_reason is None:
            raise ValueError("excluded relative-efficiency axis requires only a reason")
        return self


class RelativeEfficiencyTaskEvidence(FrozenModel):
    task_id: str
    candidate_vector_ref: str
    original_vector_ref: str
    candidate_record_ref: str
    original_record_ref: str
    axes: tuple[RelativeEfficiencyAxisObservation, ...]


class RelativeEfficiencyAxisAggregate(FrozenModel):
    axis: RelativeEfficiencyAxis
    task_ratios: dict[str, float]
    excluded_tasks: dict[str, str]
    median_ratio: float | None = Field(default=None, ge=0)


class RelativeEfficiencyEvidence(FrozenModel):
    """Content-addressed paired resource evidence; it never rewrites TaskScoreVector."""

    schema_version: Literal["2.0.0"] = "2.0.0"
    evidence_id: str
    candidate_id: str
    reference_key_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_variant: Literal["original"] = "original"
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tasks: tuple[RelativeEfficiencyTaskEvidence, ...] = Field(min_length=1)
    axis_aggregates: tuple[RelativeEfficiencyAxisAggregate, ...]
    relative_cost_ratio: float | None = Field(default=None, ge=0)
    relative_efficiency_score: float | None = Field(default=None, ge=0, le=1)
    availability: Literal["comparable", "unavailable"]
    evidence_refs: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def score_and_identity(self) -> RelativeEfficiencyEvidence:
        task_ids = [item.task_id for item in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("relative-efficiency evidence repeats a task")
        expected_axes = {
            "duration_ms",
            "tool_calls",
            "tokens",
            "artifact_size_bytes",
        }
        for task in self.tasks:
            axes = [item.axis for item in task.axes]
            if len(axes) != len(set(axes)) or set(axes) != expected_axes:
                raise ValueError("relative-efficiency task axes are incomplete or repeated")
        aggregate_axes = [item.axis for item in self.axis_aggregates]
        if len(aggregate_axes) != len(set(aggregate_axes)):
            raise ValueError("relative-efficiency aggregate axes repeat")
        task_id_set = set(task_ids)
        for aggregate in self.axis_aggregates:
            if set(aggregate.task_ratios) & set(aggregate.excluded_tasks):
                raise ValueError("relative-efficiency task is both included and excluded")
            if set(aggregate.task_ratios) | set(aggregate.excluded_tasks) != task_id_set:
                raise ValueError("relative-efficiency aggregate does not cover every task")
            expected_median = (
                float(median(aggregate.task_ratios.values()))
                if aggregate.task_ratios
                else None
            )
            if aggregate.median_ratio != expected_median:
                raise ValueError("relative-efficiency aggregate median is not reproducible")
        available = [
            item.median_ratio
            for item in self.axis_aggregates
            if item.median_ratio is not None
        ]
        expected_ratio = sum(available) / len(available) if available else None
        if self.relative_cost_ratio != expected_ratio:
            raise ValueError("relative-efficiency aggregate ratio is not reproducible")
        comparable = self.relative_cost_ratio is not None
        if comparable != (self.relative_efficiency_score is not None):
            raise ValueError("relative cost ratio and score availability disagree")
        if comparable != (self.availability == "comparable"):
            raise ValueError("relative-efficiency availability disagrees with aggregate")
        if comparable:
            assert self.relative_cost_ratio is not None
            assert self.relative_efficiency_score is not None
            expected = 1.0 / (1.0 + self.relative_cost_ratio)
            if abs(self.relative_efficiency_score - expected) > 1e-12:
                raise ValueError("relative-efficiency score does not match 1/(1+ratio)")
        payload = self.model_dump(mode="json", exclude={"evidence_id"})
        expected_id = f"relative-efficiency-{sha256_bytes(canonical_json_bytes(payload))[:24]}"
        if self.evidence_id != expected_id:
            raise ValueError("relative-efficiency evidence_id is not content-addressed")
        return self


class RelativeEfficiencyFrontierPoint(FrozenModel):
    candidate_id: str
    validation_primary_delta: float
    relative_efficiency_score: float | None = Field(default=None, ge=0, le=1)


class RelativeEfficiencyFrontierRank(FrozenModel):
    candidate_id: str
    pareto_layer: int = Field(ge=1)
    display_rank: int = Field(ge=1)
    validation_primary_delta: float
    relative_efficiency_score: float | None = Field(default=None, ge=0, le=1)
    efficiency_evidence_complete: bool


class RelativeEfficiencyFrontierRanking(FrozenModel):
    schema_version: Literal["2.0.0"] = "2.0.0"
    objective_order: tuple[str, str] = (
        "validation_primary_delta",
        "relative_efficiency_score",
    )
    ranks: tuple[RelativeEfficiencyFrontierRank, ...]


def rank_relative_efficiency_frontier(
    points: tuple[RelativeEfficiencyFrontierPoint, ...],
) -> RelativeEfficiencyFrontierRanking:
    """Stable Pareto layers; unknown efficiency never dominates a known value."""

    if len({item.candidate_id for item in points}) != len(points):
        raise ValueError("relative-efficiency frontier points must be unique")

    def dominates(
        left: RelativeEfficiencyFrontierPoint,
        right: RelativeEfficiencyFrontierPoint,
    ) -> bool:
        if left.relative_efficiency_score is None or right.relative_efficiency_score is None:
            if left.relative_efficiency_score is None and right.relative_efficiency_score is None:
                return left.validation_primary_delta > right.validation_primary_delta
            return False
        return (
            left.validation_primary_delta >= right.validation_primary_delta
            and left.relative_efficiency_score >= right.relative_efficiency_score
            and (
                left.validation_primary_delta > right.validation_primary_delta
                or left.relative_efficiency_score > right.relative_efficiency_score
            )
        )

    remaining = list(points)
    layered: list[tuple[RelativeEfficiencyFrontierPoint, int]] = []
    layer = 1
    while remaining:
        current = [
            item
            for item in remaining
            if not any(dominates(other, item) for other in remaining if other is not item)
        ]
        for item in current:
            layered.append((item, layer))
            remaining.remove(item)
        layer += 1
    ordered = sorted(
        layered,
        key=lambda row: (
            row[1],
            -row[0].validation_primary_delta,
            row[0].relative_efficiency_score is None,
            -(row[0].relative_efficiency_score or 0.0),
            row[0].candidate_id,
        ),
    )
    return RelativeEfficiencyFrontierRanking(
        ranks=tuple(
            RelativeEfficiencyFrontierRank(
                candidate_id=item.candidate_id,
                pareto_layer=pareto_layer,
                display_rank=index,
                validation_primary_delta=item.validation_primary_delta,
                relative_efficiency_score=item.relative_efficiency_score,
                efficiency_evidence_complete=item.relative_efficiency_score is not None,
            )
            for index, (item, pareto_layer) in enumerate(ordered, 1)
        )
    )


def _execution_record(
    project_root: Path,
    vector: TaskScoreVector,
) -> tuple[EvaluationRecord, str]:
    matches: list[tuple[EvaluationRecord, str]] = []
    for reference in vector.evidence_refs:
        relative = Path(reference)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("relative-efficiency evidence ref must be repository-relative")
        path = (project_root.resolve() / relative).resolve()
        if not path.is_relative_to(project_root.resolve()) or not path.is_file():
            continue
        try:
            record = EvaluationRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if (
            record.evidence_tier is EvidenceTier.E2_DELEGATED
            and record.task_id == vector.task_id
            and record.variant == vector.variant
        ):
            matches.append((record, reference))
    if len(matches) != 1:
        raise ValueError("TaskScoreVector must bind exactly one E2 resource record")
    return matches[0]


def _task_native_artifact_size(
    project_root: Path,
    record: EvaluationRecord,
    record_ref: str,
) -> float | None:
    record_path = (project_root / record_ref).resolve(strict=True)
    eval_root = record_path.parent.parent
    work_path = eval_root / "work-items" / f"{record.work_id}.json"
    if not work_path.is_file():
        return None
    value = json.loads(work_path.read_text(encoding="utf-8"))
    requested = value.get("requested_output", {}).get("filename")
    matches = [item for item in record.artifacts if item.path == requested]
    return float(matches[0].size_bytes) if len(matches) == 1 else None


def derive_relative_efficiency_evidence(
    project_root: Path,
    rows: tuple[PairedScore, ...],
    *,
    candidate_id: str,
    reference_run_ref: str,
    reference_key_hash: str,
    policy: RelativeEfficiencyPolicy,
) -> RelativeEfficiencyEvidence:
    """Derive v2 cost ratios from existing E2 usage on the exact original anchor."""

    if not rows:
        raise ValueError("relative-efficiency evidence requires held-out paired rows")
    root = project_root.resolve()
    reference_run = (root / reference_run_ref).resolve(strict=True)
    if not reference_run.is_relative_to(root):
        raise ValueError("relative-efficiency reference run escapes the project")
    task_rows: list[RelativeEfficiencyTaskEvidence] = []
    evidence_refs: set[str] = set()
    seen: set[str] = set()
    for row in rows:
        if row.task_id in seen:
            raise ValueError("relative-efficiency evidence repeats a task")
        seen.add(row.task_id)
        candidate_vector = _task_score_vector(root, row.candidate_record_id)
        original_vector = _task_score_vector(root, row.parent_record_id)
        original_path = (root / row.parent_record_id).resolve(strict=True)
        if (
            candidate_vector.task_id != row.task_id
            or original_vector.task_id != row.task_id
            or candidate_vector.variant != "candidate"
            or original_vector.variant != "original"
            or not original_path.is_relative_to(reference_run)
            or candidate_vector.scoring_policy_ref != original_vector.scoring_policy_ref
        ):
            raise ValueError("relative-efficiency pair is not candidate versus exact original")
        candidate_record, candidate_record_ref = _execution_record(root, candidate_vector)
        original_record, original_record_ref = _execution_record(root, original_vector)
        candidate_tokens = (
            candidate_record.usage.input_tokens + candidate_record.usage.output_tokens
        )
        original_tokens = original_record.usage.input_tokens + original_record.usage.output_tokens
        raw_values: dict[RelativeEfficiencyAxis, tuple[float | None, float | None]] = {
            "duration_ms": (
                float(candidate_record.usage.duration_ms),
                float(original_record.usage.duration_ms),
            ),
            "tool_calls": (
                float(candidate_record.usage.tool_calls),
                float(original_record.usage.tool_calls),
            ),
            "tokens": (float(candidate_tokens), float(original_tokens)),
            "artifact_size_bytes": (
                _task_native_artifact_size(root, candidate_record, candidate_record_ref),
                _task_native_artifact_size(root, original_record, original_record_ref),
            ),
        }
        observations: list[RelativeEfficiencyAxisObservation] = []
        for axis in ("duration_ms", "tool_calls", "tokens", "artifact_size_bytes"):
            candidate_value, original_value = raw_values[axis]
            candidate_kind = (
                candidate_record.usage.token_count_kind if axis == "tokens" else "recorded"
            )
            original_kind = (
                original_record.usage.token_count_kind if axis == "tokens" else "recorded"
            )
            reason: str | None = None
            if axis not in policy.comparable_axes:
                reason = "report_only" if axis == "artifact_size_bytes" else "missing"
            elif candidate_value is None or original_value is None:
                reason = "missing"
            elif axis == "tokens" and (
                candidate_kind == "unavailable" or original_kind == "unavailable"
            ):
                reason = "unavailable"
            elif axis == "tokens" and candidate_kind != original_kind:
                reason = "measurement_kind_mismatch"
            elif original_value == 0:
                reason = "zero_original"
            ratio = None
            if reason is None:
                assert candidate_value is not None
                assert original_value is not None
                ratio = candidate_value / original_value
            observations.append(
                RelativeEfficiencyAxisObservation(
                    axis=axis,  # type: ignore[arg-type]
                    candidate_value=candidate_value,
                    original_value=original_value,
                    candidate_measurement_kind=candidate_kind,
                    original_measurement_kind=original_kind,
                    included=reason is None,
                    exclusion_reason=reason,  # type: ignore[arg-type]
                    ratio=ratio,
                )
            )
        task_rows.append(
            RelativeEfficiencyTaskEvidence(
                task_id=row.task_id,
                candidate_vector_ref=row.candidate_record_id,
                original_vector_ref=row.parent_record_id,
                candidate_record_ref=candidate_record_ref,
                original_record_ref=original_record_ref,
                axes=tuple(observations),
            )
        )
        evidence_refs.update(
            (
                row.candidate_record_id,
                row.parent_record_id,
                candidate_record_ref,
                original_record_ref,
            )
        )

    aggregates: list[RelativeEfficiencyAxisAggregate] = []
    available_medians: list[float] = []
    for axis in policy.comparable_axes:
        ratios: dict[str, float] = {}
        excluded: dict[str, str] = {}
        for task in task_rows:
            observation = next(item for item in task.axes if item.axis == axis)
            if observation.included:
                assert observation.ratio is not None
                ratios[task.task_id] = observation.ratio
            else:
                assert observation.exclusion_reason is not None
                excluded[task.task_id] = observation.exclusion_reason
        axis_median = median(ratios.values()) if ratios else None
        if axis_median is not None:
            available_medians.append(float(axis_median))
        aggregates.append(
            RelativeEfficiencyAxisAggregate(
                axis=axis,
                task_ratios=dict(sorted(ratios.items())),
                excluded_tasks=dict(sorted(excluded.items())),
                median_ratio=axis_median,
            )
        )
    relative_cost_ratio = (
        sum(available_medians) / len(available_medians) if available_medians else None
    )
    evidence_payload = {
        "schema_version": "2.0.0",
        "candidate_id": candidate_id,
        "reference_key_hash": reference_key_hash,
        "reference_variant": "original",
        "policy_hash": policy.policy_hash,
        "tasks": [
            item.model_dump(mode="json")
            for item in sorted(task_rows, key=lambda item: item.task_id)
        ],
        "axis_aggregates": [item.model_dump(mode="json") for item in aggregates],
        "relative_cost_ratio": relative_cost_ratio,
        "relative_efficiency_score": (
            1.0 / (1.0 + relative_cost_ratio)
            if relative_cost_ratio is not None
            else None
        ),
        "availability": "comparable" if relative_cost_ratio is not None else "unavailable",
        "evidence_refs": sorted(evidence_refs),
    }
    return RelativeEfficiencyEvidence.model_validate(
        {
            **evidence_payload,
            "evidence_id": (
                "relative-efficiency-"
                + sha256_bytes(canonical_json_bytes(evidence_payload))[:24]
            ),
        }
    )


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
    relative_efficiency_policy: RelativeEfficiencyPolicy | None = None,
    relative_efficiency_evidence: RelativeEfficiencyEvidence | None = None,
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
    if (relative_efficiency_policy is None) != (relative_efficiency_evidence is None):
        raise ValueError("relative-efficiency policy and evidence must be provided together")
    secondary = secondary_objective_improvements or {}
    if relative_efficiency_evidence is not None:
        assert relative_efficiency_policy is not None
        if relative_efficiency_evidence.policy_hash != relative_efficiency_policy.policy_hash:
            raise ValueError("relative-efficiency evidence uses another policy")
        if {item.task_id for item in relative_efficiency_evidence.tasks} != {
            item.task_id for item in rows
        }:
            raise ValueError("relative-efficiency evidence does not cover the Gate task set")
        pair_refs = {
            item.task_id: (item.candidate_record_id, item.parent_record_id) for item in rows
        }
        if any(
            pair_refs[item.task_id]
            != (item.candidate_vector_ref, item.original_vector_ref)
            for item in relative_efficiency_evidence.tasks
        ):
            raise ValueError("relative-efficiency evidence does not bind the Gate pairs")
        if tuple(item.axis for item in relative_efficiency_evidence.axis_aggregates) != (
            relative_efficiency_policy.comparable_axes
        ):
            raise ValueError("relative-efficiency aggregate axes do not match its policy")
        if "task_score_efficiency" in secondary:
            raise ValueError("v2 cannot reuse the v1 absolute TaskScoreVector efficiency axis")
        registered_secondary = dict(policy.secondary_minimum_effect)
    else:
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
    extreme_relative_cost_regression = bool(
        relative_efficiency_evidence is not None
        and relative_efficiency_evidence.relative_cost_ratio is not None
        and relative_efficiency_policy is not None
        and relative_efficiency_evidence.relative_cost_ratio
        >= relative_efficiency_policy.max_relative_cost_ratio
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
    elif (
        category_regression
        or high_risk_regression
        or secondary_regression
        or extreme_relative_cost_regression
    ):
        outcome = GateOutcome.FAILED
        reasons = tuple(
            reason
            for reason, active in (
                (
                    "protected_objective_regression",
                    category_regression or high_risk_regression or secondary_regression,
                ),
                ("extreme_relative_cost_regression", extreme_relative_cost_regression),
            )
            if active
        )
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
                        *(
                            relative_efficiency_evidence.evidence_refs
                            if relative_efficiency_evidence is not None
                            else ()
                        ),
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
                "relative_efficiency": (
                    {
                        "policy_hash": relative_efficiency_policy.policy_hash,
                        "evidence_id": relative_efficiency_evidence.evidence_id,
                        "reference_key_hash": relative_efficiency_evidence.reference_key_hash,
                        "relative_cost_ratio": relative_efficiency_evidence.relative_cost_ratio,
                        "relative_efficiency_score": (
                            relative_efficiency_evidence.relative_efficiency_score
                        ),
                        "availability": relative_efficiency_evidence.availability,
                        "max_relative_cost_ratio": (
                            relative_efficiency_policy.max_relative_cost_ratio
                        ),
                        "extreme_relative_cost_regression": (
                            extreme_relative_cost_regression
                        ),
                        "v1_absolute_efficiency_axis_used": False,
                    }
                    if relative_efficiency_evidence is not None
                    and relative_efficiency_policy is not None
                    else None
                ),
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
