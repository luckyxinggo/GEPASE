"""Cross-stage schemas that are stable from S0 onward."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0.0"


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactRef(FrozenModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = "application/json"
    size_bytes: int = Field(ge=0)


class BudgetSpec(FrozenModel):
    max_work_items: int = Field(ge=1)
    max_high_fidelity_items: int = Field(default=0, ge=0)
    max_tokens: int | None = Field(default=None, ge=1)


class GateResult(FrozenModel):
    gate_id: str
    status: Literal["passed", "failed", "not_run", "external_validation"]
    evidence: tuple[str, ...] = ()
    detail: str = ""


class StageReport(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    stage_id: str
    status: Literal["pending", "in_progress", "external_validation", "complete", "blocked"]
    started_from_commit: str
    finished_commit: str
    source_tree_hash: str
    input_artifacts: tuple[ArtifactRef, ...] = ()
    output_artifacts: tuple[ArtifactRef, ...] = ()
    commands: tuple[str, ...] = ()
    gate_results: tuple[GateResult, ...] = ()
    real_agent_runs: int = Field(default=0, ge=0)
    headless_provider_runs: int = Field(default=0, ge=0)
    metrics: dict[str, float | int | str | bool] = Field(default_factory=dict)
    known_issues: tuple[str, ...] = ()
    design_decisions: tuple[str, ...] = ()
    unlocks: tuple[str, ...] = ()
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
