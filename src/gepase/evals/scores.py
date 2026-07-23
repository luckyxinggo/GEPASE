"""Canonical task-level functional score vector.

The vector keeps deterministic checks, independent quality judgment, paired
Skill gain, reliability, efficiency, and package quality separate.  It has no
implicit scalar reward, so an assertion pass rate cannot masquerade as overall
Skill quality.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from gepase.schemas.common import SCHEMA_VERSION, FrozenModel


class TaskScoreVector(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    task_id: str = Field(min_length=1)
    pair_id: str = Field(min_length=1)
    variant: Literal["no-skill", "original", "candidate"]
    candidate_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_correctness: float = Field(ge=0.0, le=1.0)
    output_quality: float = Field(ge=0.0, le=1.0)
    skill_gain: float = Field(ge=-1.0, le=1.0)
    reliability: float = Field(ge=0.0, le=1.0)
    efficiency: float = Field(ge=0.0, le=1.0)
    package_quality: float = Field(ge=0.0, le=1.0)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    scoring_policy_ref: str = Field(min_length=1)

    @model_validator(mode="after")
    def evidence_is_unique(self) -> TaskScoreVector:
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("TaskScoreVector evidence_refs must be unique")
        return self

    @property
    def objectives(self) -> dict[str, float]:
        return {
            "task_correctness": self.task_correctness,
            "output_quality": self.output_quality,
            "skill_gain": self.skill_gain,
            "reliability": self.reliability,
            "efficiency": self.efficiency,
            "package_quality": self.package_quality,
        }

