"""Frozen configuration for a fresh paired reference on the existing Eval Engine."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from gepase.optimizer.session_runtime import ActiveSessionBudgetPolicy, RuntimeBarrier
from gepase.schemas.common import FrozenModel
from gepase.store.artifacts import canonical_json_bytes, sha256_bytes


class ReferenceIsolationPolicy(FrozenModel):
    host: str = Field(min_length=1)
    model: str = Field(min_length=1)
    fresh_context_per_role_work_item: Literal[True] = True
    share_role_conversation_history: Literal[False] = False
    max_concurrency: int = Field(default=3, ge=1, le=32)


class ReferenceExecutionConfig(FrozenModel):
    """Pre-registered inputs; execution still uses MultiFidelityEvalEngine."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    run_id: str = Field(min_length=1)
    lifecycle_mode: Literal["create_new"] = "create_new"
    frozen_eval_plan_ref: str = Field(min_length=1)
    scoring_policy_ref: str = Field(min_length=1)
    skill_ref: str = Field(min_length=1)
    package_graph_ref: str = Field(min_length=1)
    variants: tuple[Literal["no-skill", "original"], ...]
    splits: tuple[Literal["train", "validation"], ...]
    evidence_tiers: tuple[Literal["E0", "E2", "E3"], ...]
    seed: int
    timeout_seconds: int = Field(ge=1)
    isolation: ReferenceIsolationPolicy
    headless_enabled: Literal[False] = False
    active_session_budget_policy: ActiveSessionBudgetPolicy
    checkpoint_schema: Literal["gepase.BudgetCheckpoint/1.0.0"]
    continuation_schema: Literal["gepase.BudgetContinuationDecision/1.0.0"]

    @model_validator(mode="after")
    def frozen_reference_contract(self) -> ReferenceExecutionConfig:
        for value in (
            self.frozen_eval_plan_ref,
            self.scoring_policy_ref,
            self.skill_ref,
            self.package_graph_ref,
        ):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("reference config refs must be repository-relative")
        if set(self.variants) != {"no-skill", "original"} or len(self.variants) != 2:
            raise ValueError("fresh reference requires exactly no-skill/original")
        if set(self.splits) != {"train", "validation"} or len(self.splits) != 2:
            raise ValueError("fresh reference requires complete train/validation")
        if set(self.evidence_tiers) != {"E0", "E2", "E3"}:
            raise ValueError("fresh reference requires frozen E0/E2/E3")
        policy = self.active_session_budget_policy
        if policy.max_concurrency != self.isolation.max_concurrency:
            raise ValueError("reference concurrency policies disagree")
        required = {
            RuntimeBarrier.PACKAGE_COMPILED,
            RuntimeBarrier.REFERENCE_EXECUTION_COMPLETE,
            RuntimeBarrier.REFERENCE_SEALED,
        }
        if not required <= set(policy.required_barriers):
            raise ValueError("reference config omits a required checkpoint barrier")
        return self

    @property
    def config_hash(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))


def load_reference_execution_config(path: Path) -> ReferenceExecutionConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("reference execution config must be a mapping")
    # Normalize through JSON so YAML-specific scalar objects never enter identity.
    return ReferenceExecutionConfig.model_validate_json(json.dumps(raw))
