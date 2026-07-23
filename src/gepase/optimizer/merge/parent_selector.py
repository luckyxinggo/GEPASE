"""Train-only Pareto-complementary merge-parent selection."""

from __future__ import annotations

import json
from pathlib import Path

from gepase.optimizer.evolution.models import MergeParentSetSnapshot
from gepase.optimizer.merge.models import (
    ParentSelectionScore,
    SelectedMergeParentSet,
)
from gepase.optimizer.merge.parent_contract import validate_merge_parent_set

FORBIDDEN_SELECTION_KEYS = {
    "validation",
    "heldout",
    "held_out",
    "test",
    "deployable",
    "accepted",
    "gate_3",
}


def _selection_score(snapshot: MergeParentSetSnapshot) -> ParentSelectionScore:
    report = validate_merge_parent_set(snapshot)
    if not report.merge_input_compatible:
        raise ValueError(
            f"parent set {snapshot.parent_set_id} failed consumer revalidation: "
            f"{[item.value for item in report.reason_codes]}"
        )
    contributions = tuple(parent.contribution for parent in snapshot.parents)
    task_keys = [set(item.task_keys) for item in contributions]
    objective_keys = [set(item.objective_keys) for item in contributions]
    component_keys = [set(item.component_ids) for item in contributions]
    closures = [set(item.closure_ids) for item in contributions]
    union = set().union(*closures)
    overlap = set.intersection(*closures) if closures else set()
    exclusive_tasks = sum(
        len(keys - set().union(*(other for other in task_keys if other is not keys)))
        for keys in task_keys
    )
    exclusive_objectives = sum(
        len(keys - set().union(*(other for other in objective_keys if other is not keys)))
        for keys in objective_keys
    )
    exclusive_components = len(set().union(*component_keys))
    overlap_ratio = len(overlap) / len(union) if union else 1.0
    lca_distance = sum(len(parent.ancestor_chain) - 1 for parent in snapshot.parents)
    structural_risk = overlap_ratio + 0.05 * max(0, len(snapshot.parents) - 2)
    score = (
        3.0 * exclusive_tasks
        + 2.0 * exclusive_objectives
        + 0.25 * exclusive_components
        + min(len(union), 10_000) / 10_000
        + 0.1 * lca_distance
        - 2.0 * structural_risk
    )
    first = snapshot.parents[0].identity
    return ParentSelectionScore(
        parent_set_id=snapshot.parent_set_id,
        package_id=first.package_id,
        parent_candidate_ids=tuple(parent.identity.candidate_id for parent in snapshot.parents),
        exclusive_task_wins=exclusive_tasks,
        exclusive_objective_wins=exclusive_objectives,
        exclusive_component_count=exclusive_components,
        closure_union_size=len(union),
        closure_overlap_size=len(overlap),
        closure_overlap_ratio=overlap_ratio,
        structural_risk=structural_risk,
        lca_distance=lca_distance,
        score=score,
        contract_revalidated=True,
    )


def select_merge_parent_sets(
    handoff_path: Path,
    *,
    package_allowlist: set[str] | None = None,
    limit: int | None = None,
) -> tuple[SelectedMergeParentSet, ...]:
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    if not isinstance(handoff, dict):
        raise ValueError("merge handoff must be a JSON object")
    forbidden = {
        key
        for key in handoff
        if any(marker in key.casefold() for marker in FORBIDDEN_SELECTION_KEYS)
        and key not in {"deployable_selection_features", "held_out_selection_features"}
    }
    if forbidden:
        raise ValueError(f"handoff exposes forbidden breeding fields: {sorted(forbidden)}")
    if handoff.get("deployable_selection_features") not in ([], 0, None):
        raise ValueError("deployable features must not influence merge selection")
    if handoff.get("held_out_selection_features") not in ([], 0, None):
        raise ValueError("held-out features must not influence merge selection")
    selected: list[SelectedMergeParentSet] = []
    for raw in handoff.get("parent_sets", []):
        snapshot = MergeParentSetSnapshot.model_validate(raw)
        package_id = snapshot.parents[0].identity.package_id
        if package_allowlist is not None and package_id not in package_allowlist:
            continue
        selected.append(SelectedMergeParentSet(snapshot=snapshot, score=_selection_score(snapshot)))
    selected.sort(
        key=lambda item: (
            -item.score.score,
            item.score.structural_risk,
            item.score.package_id,
            item.snapshot.parent_set_id,
        )
    )
    if limit is not None:
        selected = selected[:limit]
    return tuple(selected)
