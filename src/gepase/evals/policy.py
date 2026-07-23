"""Provider-neutral evaluation policy."""

from __future__ import annotations

from pydantic import Field, model_validator

from gepase.evals.schema import EvidenceTier
from gepase.schemas.common import FrozenModel


class EvalPolicy(FrozenModel):
    allowed_tiers: tuple[EvidenceTier, ...]
    minimum_acceptance_tier: EvidenceTier
    paired: bool = True
    seed: int = 42
    timeout_seconds: int = Field(default=600, ge=1)
    max_attempts: int = Field(default=1, ge=1, le=10)

    @model_validator(mode="after")
    def validate_minimum(self) -> EvalPolicy:
        if self.minimum_acceptance_tier not in self.allowed_tiers:
            raise ValueError("minimum acceptance tier must be allowed")
        return self
