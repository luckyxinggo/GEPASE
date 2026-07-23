"""Portable work-item and Agent submission contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypeAlias

from pydantic import Field, model_validator

from gepase.evals.evidence import ProviderFailureKind, TraceStep, UsageRecord
from gepase.evals.redaction import ensure_redacted
from gepase.evals.schema import EvidenceTier
from gepase.schemas.common import SCHEMA_VERSION, ArtifactRef, FrozenModel

Variant: TypeAlias = Literal["no-skill", "original", "candidate"]


class WorkStatus(StrEnum):
    PENDING = "pending"
    EXPORTED = "exported"
    COMPLETED = "completed"
    FAILED = "failed"


class PairingSnapshot(FrozenModel):
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_snapshot: str
    host_model_snapshot: str
    seed: int


class PackageAccessKind(StrEnum):
    AVAILABLE = "available"
    READ = "read"
    EXECUTED = "executed"


class PackageAccessEvent(FrozenModel):
    sequence: int = Field(ge=0)
    kind: PackageAccessKind
    path: str
    node_id: str | None = None
    bytes_loaded: int = Field(default=0, ge=0)
    tokens_loaded: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_relative_path(self) -> PackageAccessEvent:
        path = Path(self.path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("package access path must be repository-relative")
        return self


class EvalWorkItem(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    work_id: str
    pair_id: str
    task_id: str
    skill_id: str
    variant: Variant
    evidence_tier: EvidenceTier
    provider_id: str
    prompt: str
    fixture_ref: str
    fixture_refs: tuple[str, ...] = ()
    skill_ref: str | None = None
    package_graph_ref: str | None = None
    package_node_map: dict[str, str] = Field(default_factory=dict)
    requested_output: dict[str, str]
    candidate_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_plan_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    case_contract_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    split: Literal["train", "validation", "test"] | None = None
    pairing: PairingSnapshot
    required_capabilities: tuple[str, ...]
    timeout_seconds: int = Field(default=600, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_paths_and_variant(self) -> EvalWorkItem:
        for value in (
            self.fixture_ref,
            *self.fixture_refs,
            self.skill_ref,
            self.package_graph_ref,
        ):
            if value is None:
                continue
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("work-item paths must be repository-relative")
        if self.variant == "no-skill" and self.skill_ref is not None:
            raise ValueError("no-skill work item cannot expose a Skill package")
        if self.variant != "no-skill" and self.skill_ref is None:
            raise ValueError("with-skill work item requires skill_ref")
        if self.fixture_refs and self.fixture_ref not in self.fixture_refs:
            raise ValueError("fixture_ref must be included in fixture_refs")
        for path_value, node_id in self.package_node_map.items():
            path = Path(path_value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("package_node_map paths must be repository-relative")
            if not node_id.strip():
                raise ValueError("package_node_map node IDs cannot be empty")
        ensure_redacted(self.model_dump(mode="json"), field="work_item")
        return self


class ExecutorWorkItem(FrozenModel):
    """Oracle-free view exported to one isolated Agent Executor context."""

    schema_version: str = SCHEMA_VERSION
    work_id: str
    role: Literal["executor"] = "executor"
    task_id: str
    prompt: str
    fixture_refs: tuple[str, ...]
    requested_output: dict[str, str]
    skill_ref: str | None = None
    package_graph_ref: str | None = None
    package_node_map: dict[str, str] = Field(default_factory=dict)
    required_capabilities: tuple[str, ...]
    timeout_seconds: int = Field(ge=1)
    submission_schema_ref: str = "schemas/execution_bundle.schema.json"

    @model_validator(mode="after")
    def validate_executor_view(self) -> ExecutorWorkItem:
        for value in (
            *self.fixture_refs,
            self.skill_ref,
            self.package_graph_ref,
            self.submission_schema_ref,
        ):
            if value is None:
                continue
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Executor refs must be repository-relative")
        if self.skill_ref is None and (self.package_graph_ref or self.package_node_map):
            raise ValueError("no-skill Executor view cannot expose Package graph data")
        return self


def executor_view(item: EvalWorkItem) -> ExecutorWorkItem:
    """Remove pairing, variant, candidate, EvalPlan, and scoring identities."""
    return ExecutorWorkItem(
        work_id=item.work_id,
        task_id=item.task_id,
        prompt=item.prompt,
        fixture_refs=item.fixture_refs or (item.fixture_ref,),
        requested_output=item.requested_output,
        skill_ref=item.skill_ref,
        package_graph_ref=item.package_graph_ref,
        package_node_map=item.package_node_map,
        required_capabilities=item.required_capabilities,
        timeout_seconds=item.timeout_seconds,
    )


class ExecutionBundle(FrozenModel):
    """Task-native Agent execution evidence submitted to the Core ledger."""

    schema_version: str = SCHEMA_VERSION
    submission_id: str
    work_id: str
    provider_id: str
    host: str
    model: str
    host_task_id: str
    context_id: str | None = None
    artifact_root: str | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    transcript: ArtifactRef | None = None
    package_access: tuple[PackageAccessEvent, ...] = ()
    planned_trace: tuple[TraceStep, ...] = ()
    observed_trace: tuple[TraceStep, ...] = ()
    usage: UsageRecord
    uncertainty: float = Field(default=0, ge=0, le=1)
    proxy_score: float | None = Field(default=None, ge=0, le=1)
    proxy_score_method: str | None = None
    failure_kind: ProviderFailureKind | None = None
    failure_detail: str | None = None
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def validate_submission(self) -> ExecutionBundle:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at precedes started_at")
        if self.artifact_root is not None:
            path = Path(self.artifact_root)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("artifact_root must be repository-relative")
        if self.failure_kind is None and self.failure_detail is not None:
            raise ValueError("failure_detail requires failure_kind")
        if (self.proxy_score is None) != (self.proxy_score_method is None):
            raise ValueError("proxy_score and proxy_score_method must be provided together")
        ensure_redacted(self.model_dump(mode="json"), field="submission")
        return self


# Compatibility import for S2 artifacts and thin orchestrator adapters.  This
# is an alias to the same Pydantic class, not a second submission schema.
WorkSubmission: TypeAlias = ExecutionBundle


def canonical_hash(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def work_id_for(payload: dict[str, object]) -> str:
    return f"work-{canonical_hash(payload)[:24]}"


def submission_id_for(payload: dict[str, object]) -> str:
    return f"submission-{canonical_hash(payload)[:24]}"
