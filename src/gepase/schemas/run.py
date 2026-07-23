"""Run manifest schema."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import Field

from gepase.schemas.common import SCHEMA_VERSION, BudgetSpec, FrozenModel


class RunManifest(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    git_commit: str
    source_tree_hash: str
    dependency_lock_hash: str
    config_hash: str
    provider_kind: str
    agent_host: str | None = None
    model: str | None = None
    seed: int
    budget: BudgetSpec
    environment: dict[str, Any]
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
