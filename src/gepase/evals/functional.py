"""Typed R3 contracts for blind grading, comparison, analysis, and scoring.

These models extend the authoritative Eval Core.  They do not execute an Agent
Runtime and do not own optimizer state.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from gepase.evals.eval_plan import RoleRunProvenance, RubricCriterion
from gepase.evals.evidence import AssertionResult, ProviderFailureKind, UsageRecord
from gepase.evals.work_items import Variant
from gepase.schemas.common import SCHEMA_VERSION, ArtifactRef, FrozenModel


class FunctionalRunState(StrEnum):
    PLANNED = "planned"
    EXECUTION_COMPLETE = "execution_complete"
    GRADING_READY = "grading_ready"
    GRADING_COMPLETE = "grading_complete"
    COMPARISON_READY = "comparison_ready"
    COMPARISON_COMPLETE = "comparison_complete"
    ANALYSIS_READY = "analysis_ready"
    ANALYSIS_COMPLETE = "analysis_complete"
    SCORED = "scored"


class FunctionalScoringPolicy(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    policy_id: str
    frozen_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    oracle_ref: str
    task_correctness_weight: float = Field(default=0.55, ge=0, le=1)
    output_quality_weight: float = Field(default=0.45, ge=0, le=1)
    comparator_weight: float = Field(default=0.2, ge=0, le=1)
    duration_budget_ms: int = Field(default=600_000, ge=1)
    token_budget: int = Field(default=32_000, ge=1)
    tool_call_budget: int = Field(default=32, ge=1)
    artifact_size_budget_bytes: int = Field(default=1_500_000, ge=1)
    comparator_case_ids: tuple[str, ...]

    @model_validator(mode="after")
    def valid_policy(self) -> FunctionalScoringPolicy:
        if abs(self.task_correctness_weight + self.output_quality_weight - 1.0) > 1e-9:
            raise ValueError("correctness and quality weights must sum to 1")
        reference = self.oracle_ref.split(":", 1)[0]
        path = Path(reference)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("oracle_ref must be repository-relative")
        if len(self.comparator_case_ids) != len(set(self.comparator_case_ids)):
            raise ValueError("comparator_case_ids must be unique")
        return self


class GifInspection(FrozenModel):
    artifact_ref: str
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    frame_count: int = Field(ge=1)
    unique_frame_count: int = Field(ge=1)
    total_duration_ms: int = Field(ge=0)
    frame_durations_ms: tuple[int, ...]
    effective_fps: float = Field(ge=0)
    loop_count: int | None = None
    file_size_bytes: int = Field(ge=1)
    mean_adjacent_pixel_delta: float = Field(ge=0)
    first_last_pixel_delta: float = Field(ge=0)
    contact_sheet_ref: str
    measurements_ref: str


class DeterministicGradingBundle(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    bundle_id: str
    task_id: str
    pair_id: str
    variant: Variant
    source_record_id: str
    e3_record_id: str
    oracle_ref: str
    oracle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inspection: GifInspection
    assertion_results: tuple[AssertionResult, ...]
    weighted_score: float = Field(ge=0, le=1)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BlindArtifact(FrozenModel):
    blind_id: str
    artifact_root: str
    artifact: ArtifactRef
    contact_sheet_ref: str
    inspection_ref: str

    @model_validator(mode="after")
    def relative_refs(self) -> BlindArtifact:
        for value in (self.artifact_root, self.contact_sheet_ref, self.inspection_ref):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("blind artifact refs must be repository-relative")
        return self


class IndependentGraderWorkItem(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    grader_work_id: str
    role: Literal["independent_grader"] = "independent_grader"
    task_id: str
    task_prompt: str
    expected_output_zh: str
    rubric: tuple[RubricCriterion, ...]
    blind_artifact: BlindArtifact
    submission_schema_ref: str
    forbidden_inputs: tuple[str, ...] = (
        "variant identity",
        "candidate identity",
        "sibling output",
        "expected winner",
        "executor conversation",
    )


class CriterionGrade(FrozenModel):
    criterion_id: str
    score: float = Field(ge=0, le=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    rationale_zh: str = Field(min_length=1)


class IndependentGraderSubmission(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    submission_id: str
    grader_work_id: str
    role_run: RoleRunProvenance
    criterion_grades: tuple[CriterionGrade, ...]
    overall_score: float = Field(ge=0, le=1)
    factual_claims_zh: tuple[str, ...]
    feedback_zh: str = Field(min_length=1)


class ComparatorSide(FrozenModel):
    side_id: Literal["left", "right"]
    blind_artifact: BlindArtifact


class ComparatorWorkItem(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    comparator_work_id: str
    role: Literal["comparator"] = "comparator"
    task_id: str
    task_prompt: str
    expected_output_zh: str
    rubric: tuple[RubricCriterion, ...]
    left: ComparatorSide
    right: ComparatorSide
    order_label: Literal["AB", "BA"]
    submission_schema_ref: str
    forbidden_inputs: tuple[str, ...] = (
        "variant identity",
        "candidate identity",
        "expected winner",
        "executor conversation",
        "independent grader score",
        "deterministic assertion score",
    )


class ComparatorCriterion(FrozenModel):
    criterion_id: str
    preference: Literal["left", "right", "tie"]
    rationale_zh: str = Field(min_length=1)


class ComparatorSubmission(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    submission_id: str
    comparator_work_id: str
    role_run: RoleRunProvenance
    winner: Literal["left", "right", "tie"]
    confidence: float = Field(ge=0, le=1)
    criteria: tuple[ComparatorCriterion, ...]
    rationale_zh: str = Field(min_length=1)


class ComparatorReconciliation(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    task_id: str
    pair_id: str
    ab_submission_ref: str
    ba_submission_ref: str
    ab_original_outcome: Literal["win", "loss", "tie"]
    ba_original_outcome: Literal["win", "loss", "tie"]
    consistent: bool
    original_margin: float = Field(ge=-1, le=1)


class AnalysisNodeHint(FrozenModel):
    node_id: str
    path: str
    kind: str
    label: str


class AnalyzerEvidenceSummary(FrozenModel):
    variant: Literal["no-skill", "original"]
    execution_record_ref: str
    deterministic_bundle_ref: str
    independent_grade_ref: str
    task_correctness: float = Field(ge=0, le=1)
    output_quality: float = Field(ge=0, le=1)
    failed_expectation_ids: tuple[str, ...]
    grader_feedback_zh: str
    failure_kind: ProviderFailureKind | None = None
    package_access_ref: str | None = None


class AnalyzerWorkItem(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    analyzer_work_id: str
    role: Literal["analyzer"] = "analyzer"
    task_id: str
    pair_id: str
    task_prompt: str
    baseline: AnalyzerEvidenceSummary
    original: AnalyzerEvidenceSummary
    comparator_ref: str | None = None
    package_graph_ref: str
    node_hints: tuple[AnalysisNodeHint, ...]
    submission_schema_ref: str
    forbidden_inputs: tuple[str, ...] = (
        "candidate identity",
        "search history",
        "patch proposal",
        "grader conversation",
        "comparator conversation",
    )


class FailureAnalysis(FrozenModel):
    analysis_id: str
    variant: Literal["no-skill", "original"]
    issue_zh: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    target_node_ids: tuple[str, ...]
    confidence: float = Field(ge=0, le=1)
    recommendation_zh: str = Field(min_length=1)


class AnalyzerSubmission(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    submission_id: str
    analyzer_work_id: str
    role_run: RoleRunProvenance
    analyses: tuple[FailureAnalysis, ...]
    summary_zh: str = Field(min_length=1)


class PackageAccessAuditItem(FrozenModel):
    work_id: str
    variant: Variant
    valid: bool
    available_node_ids: tuple[str, ...]
    read_node_ids: tuple[str, ...]
    executed_node_ids: tuple[str, ...]
    bytes_loaded: int = Field(ge=0)
    tokens_loaded: int = Field(ge=0)
    unresolved_paths: tuple[str, ...]
    problems: tuple[str, ...]


class IsolationAudit(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    valid: bool
    executor_context_ids: tuple[str, ...]
    grader_context_ids: tuple[str, ...]
    comparator_context_ids: tuple[str, ...]
    analyzer_context_ids: tuple[str, ...]
    duplicate_context_ids: tuple[str, ...]
    oracle_leakage_findings: tuple[str, ...]
    sibling_leakage_findings: tuple[str, ...]
    candidate_identity_findings: tuple[str, ...]


class ReliabilitySummary(FrozenModel):
    sample_count: int = Field(ge=1)
    mean: float = Field(ge=0, le=1)
    std: float = Field(ge=0)
    minimum: float = Field(ge=0, le=1)
    maximum: float = Field(ge=0, le=1)
    failure_rate: float = Field(ge=0, le=1)
    outlier_count: int = Field(ge=0)


class FunctionalPairSummary(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    task_id: str
    pair_id: str
    split: Literal["train", "validation", "test"]
    no_skill_vector_ref: str
    original_vector_ref: str
    correctness_delta: float = Field(ge=-1, le=1)
    quality_delta: float = Field(ge=-1, le=1)
    paired_basis_delta: float = Field(ge=-1, le=1)
    comparator_margin: float | None = Field(default=None, ge=-1, le=1)
    skill_gain: float = Field(ge=-1, le=1)
    comparator_consistent: bool | None = None


class FunctionalRunSummary(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    frozen_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scoring_policy_ref: str
    pair_summaries: tuple[FunctionalPairSummary, ...]
    reliability: dict[str, ReliabilitySummary]
    usage: dict[str, UsageRecord]
    trigger_metrics_ref: str | None = None
    trigger_mixed_into_functional: Literal[False] = False
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def stable_role_id(prefix: str, payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{hashlib.sha256(canonical.encode()).hexdigest()[:24]}"


def clamp(value: float, minimum: float = -1.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))
