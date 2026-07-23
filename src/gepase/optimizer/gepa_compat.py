"""Thin use of official GEPA 0.1.4 state, selectors, and acceptance semantics."""

from __future__ import annotations

import random
from importlib.metadata import version

from gepa.core.state import GEPAState, ValsetEvaluation
from gepa.strategies.candidate_selector import (
    CurrentBestCandidateSelector,
    ParetoCandidateSelector,
)
from gepa.strategies.component_selector import RoundRobinReflectionComponentSelector

from gepase.optimizer.candidate import PackageCandidate
from gepase.optimizer.gepa_adapter import CandidateEvaluation

PINNED_GEPA_VERSION = "0.1.4"


def assert_compatible_gepa() -> dict[str, object]:
    installed = version("gepa")
    if installed != PINNED_GEPA_VERSION:
        raise RuntimeError(
            f"GEPASE S5 is validated against gepa=={PINNED_GEPA_VERSION}, found {installed}"
        )
    return {
        "version": installed,
        "state_schema_version": GEPAState._VALIDATION_SCHEMA_VERSION,
        "forked_upstream_files": 0,
    }


def _program(candidate: PackageCandidate) -> dict[str, str]:
    return {item.component_id: item.content for item in candidate.components}


def _valset(evaluation: CandidateEvaluation) -> ValsetEvaluation[dict[str, object], str]:
    return ValsetEvaluation(
        outputs_by_val_id={row.task_id: row.output for row in evaluation.rows},
        scores_by_val_id={row.task_id: row.score for row in evaluation.rows},
        objective_scores_by_val_id={
            row.task_id: dict(row.objective_scores) for row in evaluation.rows
        },
    )


def build_gepa_state(
    candidates: list[PackageCandidate],
    evaluations: list[CandidateEvaluation],
    *,
    frontier_type: str,
) -> GEPAState[dict[str, object], str]:
    assert_compatible_gepa()
    if not candidates or len(candidates) != len(evaluations):
        raise ValueError("GEPA state requires aligned candidate/evaluation rows")
    state = GEPAState(
        _program(candidates[0]),
        _valset(evaluations[0]),
        frontier_type=frontier_type,  # type: ignore[arg-type]
    )
    state.total_num_evals = len(evaluations[0].rows)
    for index, (candidate, evaluation) in enumerate(
        zip(candidates[1:], evaluations[1:], strict=True), start=1
    ):
        parent_indices = [
            candidate_index
            for candidate_index, known in enumerate(candidates[:index])
            if known.candidate_id in candidate.parent_ids
        ]
        if not parent_indices:
            raise ValueError("candidate parent is absent from accepted GEPA state")
        state.update_state_with_new_program(
            parent_indices,
            _program(candidate),
            _valset(evaluation),
            run_dir=None,
            num_metric_calls_by_discovery_of_new_program=len(evaluation.rows),
        )
        state.total_num_evals += len(evaluation.rows)
    if not state.is_consistent():
        raise RuntimeError("official GEPA state is inconsistent")
    return state


def select_candidate_indices(
    state: GEPAState[dict[str, object], str],
    *,
    seed: int,
) -> tuple[int, int]:
    pareto = ParetoCandidateSelector(random.Random(seed)).select_candidate_idx(state)
    current_best = CurrentBestCandidateSelector().select_candidate_idx(state)
    return pareto, current_best


def round_robin_components(
    candidate: PackageCandidate,
    *,
    cursor: int,
    count: int,
) -> tuple[tuple[str, ...], int]:
    state = GEPAState(
        _program(candidate),
        ValsetEvaluation(
            outputs_by_val_id={"component-selection": {}},
            scores_by_val_id={"component-selection": 0.0},
        ),
        frontier_type="instance",
    )
    state.total_num_evals = 1
    state.named_predictor_id_to_update_next_for_program_candidate[0] = cursor % len(
        candidate.components
    )
    selector = RoundRobinReflectionComponentSelector()
    selected: list[str] = []
    for _ in range(min(count, len(candidate.components))):
        component = selector(state, [], [], 0, _program(candidate))[0]
        if component not in selected:
            selected.append(component)
    next_cursor = state.named_predictor_id_to_update_next_for_program_candidate[0]
    return tuple(selected), next_cursor
