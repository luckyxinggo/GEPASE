"""Strongly typed project configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderConfig(StrictModel):
    kind: Literal["mock", "agent_native", "replay", "headless"] = "mock"
    name: str = "deterministic-mock"
    model: str | None = None
    endpoint: str | None = None
    credential_env: str | None = None

    @model_validator(mode="after")
    def validate_headless_boundary(self) -> ProviderConfig:
        if self.kind == "headless" and not all(
            (self.model, self.endpoint, self.credential_env)
        ):
            raise ValueError(
                "headless providers require model, endpoint, and credential_env references"
            )
        return self


RoleName = Literal[
    "eval_designer",
    "executor",
    "independent_grader",
    "comparator",
    "analyzer",
    "reflection",
    "proposer",
]


class RoleProviderConfig(StrictModel):
    """Provider-neutral role routing; execution remains outside the Python Core."""

    default: ProviderConfig = ProviderConfig(kind="agent_native", name="host-agent")
    roles: dict[RoleName, ProviderConfig] = Field(default_factory=dict)


class EvalPolicyConfig(StrictModel):
    allowed_tiers: tuple[str, ...] = ("E0",)
    minimum_acceptance_tier: str = "E0"
    paired: bool = True
    max_attempts: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def validate_minimum_tier(self) -> EvalPolicyConfig:
        if self.minimum_acceptance_tier not in self.allowed_tiers:
            raise ValueError("minimum_acceptance_tier must be included in allowed_tiers")
        return self


class DatasetConfig(StrictModel):
    manifest: str
    split: str = "train"


class OptimizerConfig(StrictModel):
    name: str = "none"
    seed: int = 42


class BudgetConfig(StrictModel):
    max_work_items: int = Field(default=100, ge=1)
    max_high_fidelity_items: int = Field(default=0, ge=0)
    max_tokens: int | None = Field(default=None, ge=1)


class ReportConfig(StrictModel):
    formats: tuple[Literal["json", "markdown", "html"], ...] = ("json",)
    redact: bool = True


class ProjectConfig(StrictModel):
    project: str = "gepase"
    provider: ProviderConfig = ProviderConfig()
    role_providers: RoleProviderConfig | None = None
    eval_policy: EvalPolicyConfig = EvalPolicyConfig()
    dataset: DatasetConfig
    optimizer: OptimizerConfig = OptimizerConfig()
    budget: BudgetConfig = BudgetConfig()
    report: ReportConfig = ReportConfig()
