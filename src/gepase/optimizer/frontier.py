"""Serializable view over official GEPA hybrid or instance Pareto state."""

from __future__ import annotations

from pydantic import Field

from gepase.optimizer.candidate import PackageCandidate
from gepase.optimizer.gepa_adapter import CandidateEvaluation
from gepase.optimizer.gepa_compat import build_gepa_state, select_candidate_indices
from gepase.schemas.common import FrozenModel


class FrontierSnapshot(FrozenModel):
    schema_version: str = "1.0.0"
    frontier_type: str
    iteration: int = Field(ge=0)
    mapping: dict[str, tuple[str, ...]]
    member_ids: tuple[str, ...]
    pareto_selected_id: str
    current_best_id: str
    aggregate_scores: dict[str, float]


def frontier_snapshot(
    candidates: list[PackageCandidate],
    evaluations: list[CandidateEvaluation],
    *,
    frontier_type: str,
    iteration: int,
    seed: int,
) -> FrontierSnapshot:
    state = build_gepa_state(candidates, evaluations, frontier_type=frontier_type)
    pareto_index, best_index = select_candidate_indices(state, seed=seed + iteration)
    mapping = {
        repr(key): tuple(sorted(candidates[index].candidate_id for index in indices))
        for key, indices in state.get_pareto_front_mapping().items()
    }
    members = tuple(sorted({candidate for values in mapping.values() for candidate in values}))
    return FrontierSnapshot(
        frontier_type=frontier_type,
        iteration=iteration,
        mapping=mapping,
        member_ids=members,
        pareto_selected_id=candidates[pareto_index].candidate_id,
        current_best_id=candidates[best_index].candidate_id,
        aggregate_scores={
            candidate.candidate_id: state.program_full_scores_val_set[index]
            for index, candidate in enumerate(candidates)
        },
    )
