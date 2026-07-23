"""Bounded typed conflict-resolution work for an external Agent host."""

from __future__ import annotations

import hashlib

from gepase.optimizer.merge.models import (
    MergeConflict,
    MergeResolutionSubmission,
    MergeResolutionWorkItem,
)


def build_resolution_work_item(
    *,
    parent_set_id: str,
    lca_candidate_id: str,
    base_snapshot_hash: str,
    base_content_hash: str,
    conflicts: tuple[MergeConflict, ...],
    node_preconditions: dict[str, str],
) -> MergeResolutionWorkItem:
    if not conflicts:
        raise ValueError("conflict resolution work requires conflicts")
    allowed_paths = tuple(sorted({item.path for item in conflicts}))
    allowed_nodes = tuple(sorted({node for item in conflicts for node in item.node_ids}))
    if set(node_preconditions) != set(allowed_nodes):
        raise ValueError("conflict resolution requires exact LCA node preconditions")
    identity = "|".join(
        (parent_set_id, lca_candidate_id, *(item.conflict_id for item in conflicts))
    )
    return MergeResolutionWorkItem(
        work_id=f"merge-resolution-{hashlib.sha256(identity.encode()).hexdigest()[:24]}",
        parent_set_id=parent_set_id,
        lca_candidate_id=lca_candidate_id,
        conflicts=conflicts,
        allowed_paths=allowed_paths,
        allowed_node_ids=allowed_nodes,
        allowed_preconditions=node_preconditions,
        base_snapshot_hash=base_snapshot_hash,
        base_content_hash=base_content_hash,
    )


def validate_resolution_submission(
    work: MergeResolutionWorkItem,
    submission: MergeResolutionSubmission,
) -> tuple[object, ...]:
    if submission.work_id != work.work_id:
        raise ValueError("resolution submission work identity mismatch")
    if submission.assertions_seen or submission.sibling_outputs_seen:
        raise ValueError("resolution Agent accessed forbidden evaluator information")
    expected_conflicts = {item.conflict_id for item in work.conflicts}
    if set(submission.resolved_conflict_ids) != expected_conflicts:
        raise ValueError("resolution must account for every conflict exactly once")
    allowed_paths = set(work.allowed_paths)
    allowed_nodes = set(work.allowed_node_ids)
    for operation in submission.operations:
        target = getattr(operation, "target_node_id", None)
        if operation.path not in allowed_paths:
            raise ValueError(f"resolution edits unrelated path: {operation.path}")
        if target is not None and str(target) not in allowed_nodes:
            raise ValueError(f"resolution edits unrelated node: {target}")
        if (
            target is not None
            and operation.precondition_hash != work.allowed_preconditions[str(target)]
        ):
            raise ValueError(f"resolution carries a stale precondition for {target}")
        if target is None and operation.precondition_hash != "absent":
            raise ValueError("additive resolution must use an absent precondition")
    return tuple(submission.operations)
