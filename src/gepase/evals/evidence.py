"""Multi-fidelity evidence records with strict planned/observed separation."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from gepase.evals.redaction import ensure_redacted
from gepase.evals.schema import EvidenceTier
from gepase.evals.scores import TaskScoreVector
from gepase.schemas.common import SCHEMA_VERSION, ArtifactRef, FrozenModel


class TraceCompleteness(StrEnum):
    NONE = "none"
    PLANNED_ONLY = "planned_only"
    PARTIAL = "partial"
    COMPLETE = "complete"


class ProviderFailureKind(StrEnum):
    INVALID_SUBMISSION = "invalid_submission"
    DUPLICATE = "duplicate"
    TIMEOUT = "timeout"
    PARTIAL_ARTIFACT = "partial_artifact"
    INTERRUPTED = "interrupted"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    TASK_FAILURE = "task_failure"
    ENVIRONMENT_UNAVAILABLE = "environment_unavailable"


class TraceStep(FrozenModel):
    sequence: int = Field(ge=0)
    action: str
    target: str | None = None
    tool: str | None = None
    outcome: str | None = None

    @model_validator(mode="after")
    def redact_trace(self) -> TraceStep:
        ensure_redacted(self.model_dump(mode="json"), field="trace")
        return self


class UsageRecord(FrozenModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)
    token_count_kind: Literal["reported", "estimated", "unavailable"] = "unavailable"

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_price_field(cls, value: Any) -> Any:
        """Read historical artifacts without retaining obsolete price accounting."""
        if isinstance(value, dict) and "cost" in value:
            value = dict(value)
            value.pop("cost", None)
        return value

    @property
    def nonempty(self) -> bool:
        return any((self.input_tokens, self.output_tokens, self.tool_calls, self.duration_ms))


class EvidenceProvenance(FrozenModel):
    origin: Literal[
        "static",
        "simulation",
        "agent-native",
        "assertion",
        "replay",
        "mock",
    ]
    provider_id: str
    host: str | None = None
    model: str | None = None
    host_task_id: str | None = None
    context_id: str | None = None
    submission_id: str | None = None
    generated_by: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AssertionResult(FrozenModel):
    assertion_id: str
    family: str
    passed: bool
    weight: float = Field(gt=0)
    detail: str = ""
    evidence_refs: tuple[str, ...] = ()
    measurements: dict[str, Any] = Field(default_factory=dict)


class EvaluationRecord(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    record_id: str
    work_id: str
    pair_id: str
    task_id: str
    skill_id: str
    variant: Literal["no-skill", "original", "candidate"]
    evidence_tier: EvidenceTier
    candidate_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_snapshot: str
    host_model_snapshot: str
    seed: int
    planned_trace: tuple[TraceStep, ...] = ()
    observed_trace: tuple[TraceStep, ...] = ()
    trace_completeness: TraceCompleteness
    artifact_root: str | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    assertion_results: tuple[AssertionResult, ...] = ()
    score: float | None = Field(default=None, ge=0, le=1)
    task_score_vector: TaskScoreVector | None = None
    usage: UsageRecord = Field(default_factory=UsageRecord)
    uncertainty: float = Field(default=0, ge=0, le=1)
    provenance: EvidenceProvenance
    source_record_refs: tuple[str, ...] = ()
    failure_kind: ProviderFailureKind | None = None
    failure_detail: str | None = None

    @model_validator(mode="after")
    def validate_fidelity(self) -> EvaluationRecord:
        if self.artifact_root is not None:
            artifact_path = Path(self.artifact_root)
            if artifact_path.is_absolute() or ".." in artifact_path.parts:
                raise ValueError("artifact_root must be repository-relative")
        if self.evidence_tier in {EvidenceTier.E0_STATIC, EvidenceTier.E1_SIMULATED}:
            if self.observed_trace:
                raise ValueError("E0/E1 observed_trace must be empty")
            if self.trace_completeness not in {
                TraceCompleteness.NONE,
                TraceCompleteness.PLANNED_ONLY,
            }:
                raise ValueError("E0/E1 cannot claim partial or complete observed trace")
        if self.evidence_tier in {EvidenceTier.E2_DELEGATED, EvidenceTier.E3_EXECUTABLE}:
            provenance = self.provenance
            if not all(
                (
                    provenance.host,
                    provenance.model,
                    provenance.host_task_id,
                    provenance.submission_id,
                )
            ):
                raise ValueError("E2/E3 require host/model/task/submission provenance")
            if not self.usage.nonempty:
                raise ValueError("E2/E3 require non-empty usage")
        if self.evidence_tier is EvidenceTier.E2_DELEGATED and self.failure_kind is None:
            if not self.artifact_root or not self.artifacts or not self.observed_trace:
                raise ValueError("successful E2 requires observed artifact evidence")
        if self.evidence_tier is EvidenceTier.E3_EXECUTABLE:
            if not self.assertion_results or not self.source_record_refs:
                raise ValueError("E3 requires assertion results and a source record")
        if self.failure_kind is None and self.failure_detail is not None:
            raise ValueError("failure_detail requires failure_kind")
        return self


def record_score(assertions: tuple[AssertionResult, ...]) -> float:
    total = sum(item.weight for item in assertions)
    if total == 0:
        return 0.0
    return sum(item.weight for item in assertions if item.passed) / total


def record_payload(record: EvaluationRecord) -> dict[str, Any]:
    """Stable JSON-ready representation for artifacts and cache."""
    return record.model_dump(mode="json")
