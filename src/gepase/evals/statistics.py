"""Deterministic paired statistics for held-out acceptance decisions."""

from __future__ import annotations

import math
import random
from statistics import mean, median, pstdev

from pydantic import Field, model_validator

from gepase.schemas.common import FrozenModel


class PairedScore(FrozenModel):
    task_id: str
    category: str
    risk_level: str
    parent_score: float
    candidate_score: float
    evidence_tier: str
    minimum_acceptance_tier: str
    parent_record_id: str
    candidate_record_id: str
    uncertainty: float = Field(default=0.0, ge=0, le=1)
    repeat_index: int = Field(default=0, ge=0)

    @property
    def delta(self) -> float:
        return self.candidate_score - self.parent_score


class McNemarResult(FrozenModel):
    discordant_parent_only: int = Field(ge=0)
    discordant_candidate_only: int = Field(ge=0)
    exact_two_sided_p: float = Field(ge=0, le=1)


class PairedStatistics(FrozenModel):
    schema_version: str = "1.0.0"
    n: int = Field(ge=1)
    seed: int
    bootstrap_samples: int = Field(ge=100)
    mean_parent: float
    mean_candidate: float
    mean_delta: float
    median_delta: float
    std_delta: float = Field(ge=0)
    bootstrap_95_ci: tuple[float, float]
    wins: int = Field(ge=0)
    ties: int = Field(ge=0)
    losses: int = Field(ge=0)
    mcnemar: McNemarResult | None = None

    @model_validator(mode="after")
    def counts_match(self) -> PairedStatistics:
        if self.wins + self.ties + self.losses != self.n:
            raise ValueError("paired outcome counts do not match n")
        if self.bootstrap_95_ci[0] > self.bootstrap_95_ci[1]:
            raise ValueError("bootstrap interval is reversed")
        return self


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _binomial_cdf(k: int, n: int) -> float:
    return sum(math.comb(n, index) for index in range(k + 1)) / (2**n)


def _mcnemar(rows: tuple[PairedScore, ...]) -> McNemarResult | None:
    if not all(
        row.parent_score in {0.0, 1.0} and row.candidate_score in {0.0, 1.0} for row in rows
    ):
        return None
    parent_only = sum(row.parent_score == 1.0 and row.candidate_score == 0.0 for row in rows)
    candidate_only = sum(row.parent_score == 0.0 and row.candidate_score == 1.0 for row in rows)
    discordant = parent_only + candidate_only
    if discordant == 0:
        p_value = 1.0
    else:
        p_value = min(1.0, 2 * _binomial_cdf(min(parent_only, candidate_only), discordant))
    return McNemarResult(
        discordant_parent_only=parent_only,
        discordant_candidate_only=candidate_only,
        exact_two_sided_p=p_value,
    )


def paired_statistics(
    rows: tuple[PairedScore, ...],
    *,
    seed: int = 42,
    bootstrap_samples: int = 5_000,
) -> PairedStatistics:
    if not rows:
        raise ValueError("paired statistics require at least one row")
    if bootstrap_samples < 100:
        raise ValueError("bootstrap_samples must be at least 100")
    deltas = [row.delta for row in rows]
    randomizer = random.Random(seed)
    bootstrap = [mean(randomizer.choice(deltas) for _ in deltas) for _ in range(bootstrap_samples)]
    return PairedStatistics(
        n=len(rows),
        seed=seed,
        bootstrap_samples=bootstrap_samples,
        mean_parent=mean(row.parent_score for row in rows),
        mean_candidate=mean(row.candidate_score for row in rows),
        mean_delta=mean(deltas),
        median_delta=median(deltas),
        std_delta=pstdev(deltas),
        bootstrap_95_ci=(_quantile(bootstrap, 0.025), _quantile(bootstrap, 0.975)),
        wins=sum(value > 0 for value in deltas),
        ties=sum(value == 0 for value in deltas),
        losses=sum(value < 0 for value in deltas),
        mcnemar=_mcnemar(rows),
    )
