"""Typed merge conflict detection over LCA-relative patch operations."""

from __future__ import annotations

import hashlib
import re
from itertools import combinations

from gepase.mutation.schema import (
    AddFile,
    DeleteFile,
    InsertReference,
    PatchOperation,
    ReplacePythonFunction,
    ReplaceTextFile,
    UpdateFrontmatter,
)
from gepase.optimizer.merge.models import (
    MergeConflict,
    MergeConflictKind,
    ParentContribution,
)


def _body(operation: PatchOperation) -> str:
    return str(getattr(operation, "replacement", getattr(operation, "content", "")))


def _target(operation: PatchOperation) -> str | None:
    value = getattr(operation, "target_node_id", None)
    return str(value) if value is not None else None


def _function_signature(operation: PatchOperation) -> str | None:
    if not isinstance(operation, ReplacePythonFunction):
        return None
    match = re.search(r"(?m)^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(([^)]*)\)", _body(operation))
    if not match:
        return None
    arguments = re.sub(r"\s+", "", match.group(2))
    return f"{match.group(1)}({arguments})"


def _conflict_kind(left: PatchOperation, right: PatchOperation) -> MergeConflictKind | None:
    left_target, right_target = _target(left), _target(right)
    if left.path == right.path and isinstance(left, DeleteFile) != isinstance(right, DeleteFile):
        return MergeConflictKind.DELETE_MODIFY
    if left_target is not None and left_target == right_target:
        if _body(left) != _body(right) or left.op is not right.op:
            if isinstance(left, UpdateFrontmatter) or isinstance(right, UpdateFrontmatter):
                return MergeConflictKind.FRONTMATTER
            return MergeConflictKind.SAME_NODE_CONTENT
        return None
    if left.path != right.path:
        return None
    if isinstance(left, (AddFile, InsertReference)) and isinstance(
        right, (AddFile, InsertReference)
    ):
        if isinstance(left, InsertReference) and isinstance(right, InsertReference):
            return (
                None if _body(left) == _body(right) else MergeConflictKind.INCOMPATIBLE_DEPENDENCY
            )
        return (
            None
            if left.op is right.op and _body(left) == _body(right)
            else MergeConflictKind.PATH_COLLISION
        )
    if isinstance(left, ReplaceTextFile) or isinstance(right, ReplaceTextFile):
        return MergeConflictKind.PATH_COLLISION
    if isinstance(left, UpdateFrontmatter) and isinstance(right, UpdateFrontmatter):
        return MergeConflictKind.FRONTMATTER
    left_signature = _function_signature(left)
    right_signature = _function_signature(right)
    same_function = (
        left_signature
        and right_signature
        and left_signature.split("(")[0] == right_signature.split("(")[0]
    )
    if same_function:
        if left_signature != right_signature:
            return MergeConflictKind.INTERFACE_SIGNATURE
    return None


def detect_conflicts(
    contributions: tuple[ParentContribution, ...],
) -> tuple[MergeConflict, ...]:
    rows: list[MergeConflict] = []
    for left_contribution, right_contribution in combinations(contributions, 2):
        for left in left_contribution.operations:
            for right in right_contribution.operations:
                kind = _conflict_kind(left, right)
                if kind is None:
                    continue
                parent_ids = (
                    left_contribution.parent_candidate_id,
                    right_contribution.parent_candidate_id,
                )
                operation_ids = (left.operation_id, right.operation_id)
                payload = "|".join(
                    (
                        kind.value,
                        left.path,
                        *sorted(parent_ids),
                        *sorted(operation_ids),
                    )
                )
                rows.append(
                    MergeConflict(
                        conflict_id=f"merge-conflict-{hashlib.sha256(payload.encode()).hexdigest()[:24]}",
                        kind=kind,
                        path=left.path,
                        node_ids=tuple(
                            sorted(
                                {
                                    item
                                    for item in (_target(left), _target(right))
                                    if item is not None
                                }
                            )
                        ),
                        parent_candidate_ids=parent_ids,
                        operation_ids=operation_ids,
                        evidence_refs=tuple(
                            sorted(
                                {
                                    *left.evidence_refs,
                                    *right.evidence_refs,
                                    *left_contribution.evidence_refs,
                                    *right_contribution.evidence_refs,
                                }
                            )
                        ),
                        detail=(
                            f"{kind.value} between {left.operation_id} and "
                            f"{right.operation_id} on {left.path}"
                        ),
                    )
                )
    return tuple(sorted(rows, key=lambda item: item.conflict_id))
