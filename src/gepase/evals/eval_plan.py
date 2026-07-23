"""Typed contracts for reusable Skill EvalPlan onboarding and review."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from gepase.evals.evidence import UsageRecord
from gepase.evals.schema import EvidenceTier
from gepase.evals.work_items import PackageAccessEvent
from gepase.schemas.common import SCHEMA_VERSION, FrozenModel


class EvalPlanState(StrEnum):
    PACKAGE_PARSED = "package_parsed"
    EVAL_DRAFT_GENERATED = "eval_draft_generated"
    AUTOMATIC_CHECKS_PASSED = "automatic_checks_passed"
    AWAITING_REVIEW = "awaiting_review"
    REVIEW_IMPORTED = "review_imported"
    EVAL_PLAN_FROZEN = "eval_plan_frozen"
    EXECUTION_READY = "execution_ready"


class TriggerCaseKind(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEAR_BOUNDARY = "near_boundary"


class ReviewDecisionKind(StrEnum):
    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"
    REQUEST_REGENERATION = "request_regeneration"


class EvalPlanCheckStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    WARNING = "warning"


class SourceProvenance(FrozenModel):
    repository_url: str
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_subpath: str
    upstream_tree_hash: str = Field(pattern=r"^[0-9a-f]{40}$")
    upstream_manifest_ref: str
    vendored_ref: str
    package_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    license_spdx: str
    license_ref: str
    dependency_manifest_ref: str
    dependency_lock_ref: str
    retrieved_at: datetime

    @model_validator(mode="after")
    def relative_refs(self) -> SourceProvenance:
        for value in (
            self.source_subpath,
            self.upstream_manifest_ref,
            self.vendored_ref,
            self.license_ref,
            self.dependency_manifest_ref,
            self.dependency_lock_ref,
        ):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("source provenance paths must be relative")
        return self


class UpstreamTreeEntry(FrozenModel):
    path: str
    mode: Literal["100644", "100755"]
    git_blob_sha1: str = Field(pattern=r"^[0-9a-f]{40}$")

    @model_validator(mode="after")
    def relative_path(self) -> UpstreamTreeEntry:
        path = Path(self.path)
        if path.is_absolute() or ".." in path.parts or len(path.parts) == 0:
            raise ValueError("upstream tree entry path must be relative")
        return self


class UpstreamTreeManifest(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    repository_url: str
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_subpath: str
    upstream_tree_hash: str = Field(pattern=r"^[0-9a-f]{40}$")
    entries: tuple[UpstreamTreeEntry, ...]

    @model_validator(mode="after")
    def unique_relative_entries(self) -> UpstreamTreeManifest:
        source = Path(self.source_subpath)
        if source.is_absolute() or ".." in source.parts:
            raise ValueError("upstream source_subpath must be relative")
        paths = [entry.path for entry in self.entries]
        if not paths or len(paths) != len(set(paths)):
            raise ValueError("upstream tree entries must be non-empty and unique")
        return self


class EvalDesignBrief(FrozenModel):
    brief_id: str
    language: str = "zh-CN"
    minimum_trigger_cases_per_kind: int = Field(default=3, ge=1)
    minimum_functional_cases: int = Field(default=4, ge=2)
    minimum_train_cases: int = Field(default=2, ge=1)
    minimum_validation_cases: int = Field(default=2, ge=1)
    required_functional_families: tuple[str, ...]
    required_output_media_types: tuple[str, ...]
    coverage_notes_zh: tuple[str, ...]
    forbidden_design_shortcuts: tuple[str, ...]


class PackageDesignHint(FrozenModel):
    kind: str
    path: str
    label: str
    node_id: str


class EvalDesignerWorkItem(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    work_id: str
    role: Literal["eval_designer"] = "eval_designer"
    skill_id: str
    skill_ref: str
    package_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_graph_ref: str
    package_diagnostics_ref: str
    source_provenance_ref: str
    design_brief: EvalDesignBrief
    package_hints: tuple[PackageDesignHint, ...]
    submission_schema_ref: str
    required_package_reads: tuple[str, ...]
    forbidden_inputs: tuple[str, ...] = (
        "candidate identity",
        "expected winner",
        "sibling output",
        "search feedback",
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def relative_paths(self) -> EvalDesignerWorkItem:
        for value in (
            self.skill_ref,
            self.package_graph_ref,
            self.package_diagnostics_ref,
            self.source_provenance_ref,
            self.submission_schema_ref,
            *self.required_package_reads,
        ):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Eval Designer refs must be repository-relative")
        return self


class FixtureBinding(FrozenModel):
    ref: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str
    license: str
    purpose_zh: str

    @model_validator(mode="after")
    def relative_ref(self) -> FixtureBinding:
        path = Path(self.ref)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("fixture ref must be repository-relative")
        return self


class RequestedOutput(FrozenModel):
    filename: str
    media_type: str
    description_zh: str

    @model_validator(mode="after")
    def safe_filename(self) -> RequestedOutput:
        path = Path(self.filename)
        if path.is_absolute() or len(path.parts) != 1 or ".." in path.parts:
            raise ValueError("requested output filename must be a basename")
        return self


class FunctionalExpectation(FrozenModel):
    expectation_id: str
    category: Literal["content", "technical", "temporal", "safety", "efficiency"]
    statement_zh: str
    evidence_kind: Literal[
        "file_presence",
        "artifact_metadata",
        "frame_content",
        "temporal_sequence",
        "visual_inspection",
        "usage_record",
    ]
    deterministic: bool
    weight: float = Field(gt=0)


class RubricCriterion(FrozenModel):
    criterion_id: str
    label_zh: str
    description_zh: str
    weight: float = Field(gt=0, le=1)


class FunctionalEvidencePolicy(FrozenModel):
    minimum_tier: EvidenceTier = EvidenceTier.E2_DELEGATED
    deterministic_tier: EvidenceTier = EvidenceTier.E3_EXECUTABLE
    enable_e1: bool = False

    @model_validator(mode="after")
    def enforce_real_execution(self) -> FunctionalEvidencePolicy:
        if self.minimum_tier is not EvidenceTier.E2_DELEGATED:
            raise ValueError("functional onboarding requires E2 as the minimum tier")
        if self.deterministic_tier is not EvidenceTier.E3_EXECUTABLE:
            raise ValueError("functional onboarding requires E3 deterministic evidence")
        return self


class TriggerEvalCase(FrozenModel):
    case_type: Literal["trigger"] = "trigger"
    case_id: str
    query: str
    kind: TriggerCaseKind
    expected_trigger: bool
    rationale_zh: str
    split: Literal["train", "validation"]
    risk: Literal["low", "medium", "high"]

    @model_validator(mode="after")
    def expectation_matches_kind(self) -> TriggerEvalCase:
        if self.kind is TriggerCaseKind.POSITIVE and not self.expected_trigger:
            raise ValueError("positive trigger case must expect triggering")
        if self.kind is TriggerCaseKind.NEGATIVE and self.expected_trigger:
            raise ValueError("negative trigger case must not expect triggering")
        return self


class FunctionalEvalCase(FrozenModel):
    case_type: Literal["functional"] = "functional"
    case_id: str
    case_family: str
    prompt: str
    fixtures: tuple[FixtureBinding, ...]
    requested_output: RequestedOutput
    expected_output_zh: str
    expectations: tuple[FunctionalExpectation, ...]
    rubric: tuple[RubricCriterion, ...]
    required_capabilities: tuple[str, ...]
    difficulty: Literal["easy", "medium", "hard"]
    risk: Literal["low", "medium", "high"]
    leakage_group: str
    split: Literal["train", "validation"]
    evidence_policy: FunctionalEvidencePolicy = Field(default_factory=FunctionalEvidencePolicy)

    @model_validator(mode="after")
    def validate_quality_contract(self) -> FunctionalEvalCase:
        if not self.fixtures:
            raise ValueError("functional case requires at least one fixture")
        if not self.expectations:
            raise ValueError("functional case requires expectations")
        if not any(item.deterministic for item in self.expectations):
            raise ValueError("functional case requires deterministic evidence")
        if not self.rubric:
            raise ValueError("functional case requires a quality rubric")
        if abs(sum(item.weight for item in self.rubric) - 1.0) > 1e-9:
            raise ValueError("rubric weights must sum to 1")
        return self

    def executor_view(self) -> dict[str, Any]:
        """Return the only case fields an Executor may receive in R3."""
        return {
            "case_id": self.case_id,
            "prompt": self.prompt,
            "fixtures": [item.model_dump(mode="json") for item in self.fixtures],
            "requested_output": self.requested_output.model_dump(mode="json"),
            "required_capabilities": list(self.required_capabilities),
            "evidence_tier": self.evidence_policy.minimum_tier.value,
        }


class RoleRunProvenance(FrozenModel):
    host: str = Field(min_length=1)
    model: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    host_task_id: str = Field(min_length=1)
    usage: UsageRecord
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def ordered(self) -> RoleRunProvenance:
        if self.finished_at < self.started_at:
            raise ValueError("role run finished before it started")
        if not self.usage.nonempty:
            raise ValueError("role run requires non-empty usage")
        return self


class EvalDesignerSubmission(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    submission_id: str
    work_id: str
    skill_id: str
    package_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    role_run: RoleRunProvenance
    package_access: tuple[PackageAccessEvent, ...]
    trigger_cases: tuple[TriggerEvalCase, ...]
    functional_cases: tuple[FunctionalEvalCase, ...]
    design_notes_zh: tuple[str, ...]


class EvalPlanDraft(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    plan_id: str
    revision: int = Field(default=1, ge=1)
    package_id: str
    package_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    designer_submission_id: str
    designer_work_id: str
    trigger_cases: tuple[TriggerEvalCase, ...]
    functional_cases: tuple[FunctionalEvalCase, ...]
    design_notes_zh: tuple[str, ...]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EvalPlanCheck(FrozenModel):
    check_id: str
    status: EvalPlanCheckStatus
    detail_zh: str
    related_case_ids: tuple[str, ...] = ()


class EvalPlanCheckReport(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    draft_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid: bool
    checks: tuple[EvalPlanCheck, ...]
    metrics: dict[str, int | float | str | bool]
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EvalReviewDecision(FrozenModel):
    case_id: str
    case_type: Literal["trigger", "functional"]
    decision: ReviewDecisionKind
    edited_case: dict[str, Any] | None = None
    comment_zh: str = ""

    @model_validator(mode="after")
    def edit_contract(self) -> EvalReviewDecision:
        if self.decision is ReviewDecisionKind.EDIT and self.edited_case is None:
            raise ValueError("edit decision requires edited_case")
        if self.decision is not ReviewDecisionKind.EDIT and self.edited_case is not None:
            raise ValueError("edited_case is only allowed for edit decisions")
        if (
            self.decision
            in {
                ReviewDecisionKind.REJECT,
                ReviewDecisionKind.REQUEST_REGENERATION,
            }
            and not self.comment_zh.strip()
        ):
            raise ValueError("reject/regeneration decision requires a comment")
        return self


class EvalReviewSubmission(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    review_id: str
    plan_id: str
    draft_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer_id: str
    reviewer_kind: Literal["human", "maintainer", "agent-assisted"]
    decisions: tuple[EvalReviewDecision, ...]
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FrozenEvalPlan(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    plan_id: str
    revision: int = Field(ge=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_id: str
    package_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    draft_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_id: str
    review_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    trigger_cases: tuple[TriggerEvalCase, ...]
    functional_cases: tuple[FunctionalEvalCase, ...]
    frozen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EvalPlanCheckpoint(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    state: EvalPlanState
    package_id: str
    package_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    designer_work_id: str
    draft_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    review_id: str | None = None
    frozen_plan_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(default=1, ge=1)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
