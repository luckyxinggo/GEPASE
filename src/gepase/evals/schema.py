"""S1 benchmark and Skill package contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from gepase.schemas.common import SCHEMA_VERSION, FrozenModel


class EvidenceTier(StrEnum):
    E0_STATIC = "E0"
    E1_SIMULATED = "E1"
    E2_DELEGATED = "E2"
    E3_EXECUTABLE = "E3"


class AssertionSpec(FrozenModel):
    assertion_id: str
    family: Literal[
        "file_exists",
        "file_contains",
        "json_equals",
        "json_range",
        "forbidden_text",
        "html_contract",
    ]
    weight: float = Field(default=1.0, gt=0)
    parameters: dict[str, Any]


class CaseProvenance(FrozenModel):
    kind: Literal["handcrafted", "synthetic", "public", "historical"]
    reference: str
    license: str
    generator_version: str | None = None


class TaskCase(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    id: str
    skill_id: str
    capability_manifest_ref: str
    prompt: str
    input: dict[str, Any]
    fixture_ref: str
    fixture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    allowed_evidence_tiers: tuple[EvidenceTier, ...]
    minimum_acceptance_tier: EvidenceTier
    assertions: tuple[AssertionSpec, ...]
    judge_rubric_ref: str | None = None
    category: str
    difficulty: Literal["easy", "medium", "hard"]
    risk_level: Literal["low", "medium", "high"]
    required_capability: tuple[str, ...]
    leakage_group: str
    split: Literal["train", "validation", "test"]
    deterministic_weight: float = Field(ge=0, le=1)
    judge_weight: float = Field(ge=0, le=1)
    provenance: CaseProvenance

    @model_validator(mode="after")
    def validate_evidence_and_score(self) -> TaskCase:
        if self.minimum_acceptance_tier not in self.allowed_evidence_tiers:
            raise ValueError("minimum_acceptance_tier must be allowed")
        if abs(self.deterministic_weight + self.judge_weight - 1.0) > 1e-9:
            raise ValueError("deterministic_weight and judge_weight must sum to 1")
        if not self.assertions:
            raise ValueError("a TaskCase must define at least one deterministic assertion")
        return self


class SkillSourceManifest(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    source_id: str
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_count: int = Field(ge=1)
    total_bytes: int = Field(ge=1)
    visibility: Literal["private-local", "public-benchmark"]
    mutation_policy: Literal["read-only", "generated-copy"]


class SkillCapabilityManifest(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    skill_id: str
    source_manifest_ref: str
    labels: tuple[str, ...]
    required_tools: tuple[str, ...]
    required_services: tuple[str, ...]
    required_secrets: tuple[str, ...]
    side_effects: tuple[str, ...]
    fixture_strategy: str
    replay_strategy: str
    supported_evidence_tiers: tuple[EvidenceTier, ...]
    degradation_reasons: tuple[str, ...]


class BenchmarkPackage(FrozenModel):
    skill_id: str
    skill_path: str
    dataset_path: str
    capability_manifest_ref: str
    benchmark_card_ref: str
    provenance_ref: str
    license: str
    case_count: int = Field(ge=1)


class SplitManifest(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    version: str
    train: tuple[str, ...]
    validation: tuple[str, ...]
    test: tuple[str, ...]
    split_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    test_access_policy: Literal["isolated-read-only"] = "isolated-read-only"


class BenchmarkManifest(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    version: str
    name: str
    packages: tuple[BenchmarkPackage, ...]
    split_manifest_ref: str
    rubric_refs: tuple[str, ...]
    created_by: str
