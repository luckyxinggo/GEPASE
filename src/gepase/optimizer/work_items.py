"""Portable reflection work and Agent-native proposal submission contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import Field, model_validator

from gepase.evals.evidence import UsageRecord
from gepase.evals.redaction import ensure_redacted
from gepase.schemas.common import FrozenModel

ReflectionCallback = Callable[["ReflectionWorkItem"], tuple[tuple["ComponentEdit", ...], int]]


class ReflectionStatus(StrEnum):
    PENDING = "pending"
    EXPORTED = "exported"
    COMPLETED = "completed"
    FAILED = "failed"


class ComponentEdit(FrozenModel):
    component_id: str
    previous_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    replacement_content: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def redacted(self) -> ComponentEdit:
        ensure_redacted(self.model_dump(mode="json"), field="component_edit")
        return self


class ReflectionWorkItem(FrozenModel):
    schema_version: str = "1.0.0"
    work_id: str
    run_id: str
    iteration: int = Field(ge=0)
    candidate_id: str
    candidate_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    component_ids: tuple[str, ...] = Field(min_length=1)
    reflective_dataset: dict[str, list[dict[str, object]]]
    omitted_sections: tuple[dict[str, object], ...] = ()
    asi_token_budget: int = Field(ge=1)
    asi_token_estimate: int = Field(ge=0)
    required_evidence_coverage: float = Field(ge=0, le=1)
    planned_observed_confusion: int = Field(ge=0)
    proposer_contract: str = "s5-component-replacement-v1"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_dataset(self) -> ReflectionWorkItem:
        if set(self.reflective_dataset) != set(self.component_ids):
            raise ValueError("reflective dataset keys must equal component_ids")
        ensure_redacted(self.model_dump(mode="json"), field="reflection_work_item")
        return self


class ReflectionSubmission(FrozenModel):
    schema_version: str = "1.0.0"
    submission_id: str
    work_id: str
    host: str
    model: str
    host_task_id: str
    edits: tuple[ComponentEdit, ...] = Field(min_length=1)
    usage: UsageRecord
    started_at: datetime
    finished_at: datetime
    failure_detail: str | None = None

    @model_validator(mode="after")
    def validate_submission(self) -> ReflectionSubmission:
        if self.finished_at < self.started_at:
            raise ValueError("reflection finished_at precedes started_at")
        if not self.usage.nonempty:
            raise ValueError("reflection submission requires non-empty usage")
        if len({item.component_id for item in self.edits}) != len(self.edits):
            raise ValueError("reflection submission contains duplicate component edits")
        ensure_redacted(self.model_dump(mode="json"), field="reflection_submission")
        return self


def _canonical_hash(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def reflection_work_id(
    run_id: str,
    iteration: int,
    candidate_id: str,
    component_ids: tuple[str, ...],
) -> str:
    return "reflection-" + _canonical_hash(
        {
            "run": run_id,
            "iteration": iteration,
            "candidate": candidate_id,
            "components": list(component_ids),
        }
    )[:24]


def build_reflection_submission(
    work: ReflectionWorkItem,
    edits: tuple[ComponentEdit, ...],
    *,
    host: str,
    model: str,
    host_task_id: str,
    duration_ms: int,
) -> ReflectionSubmission:
    finished = datetime.now(UTC)
    payload = {
        "work_id": work.work_id,
        "host": host,
        "model": model,
        "host_task_id": host_task_id,
        "edits": [item.model_dump(mode="json") for item in edits],
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    return ReflectionSubmission(
        submission_id="reflection-submission-" + _canonical_hash(payload)[:24],
        work_id=work.work_id,
        host=host,
        model=model,
        host_task_id=host_task_id,
        edits=edits,
        usage=UsageRecord(
            input_tokens=max(1, len(json.dumps(work.reflective_dataset, ensure_ascii=False)) // 4),
            output_tokens=max(1, len(serialized) // 4),
            duration_ms=duration_ms,
            token_count_kind="estimated",
        ),
        started_at=finished - timedelta(milliseconds=duration_ms),
        finished_at=finished,
    )
