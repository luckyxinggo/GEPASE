from __future__ import annotations

import pytest

from gepase.mutation.schema import AddFile, PatchOperationKind, RegressionRisk
from gepase.optimizer.merge.conflicts import detect_conflicts
from gepase.optimizer.merge.fixture_suite import _contribution, _operation
from gepase.optimizer.merge.models import (
    MergeConflictKind,
    MergeResolutionSubmission,
)
from gepase.optimizer.merge.proposer import (
    build_resolution_work_item,
    validate_resolution_submission,
)


def test_resolution_cannot_edit_unrelated_path() -> None:
    conflicts = detect_conflicts(
        (
            _contribution(
                "candidate-a",
                _operation(MergeConflictKind.SAME_NODE_CONTENT, "a"),
            ),
            _contribution(
                "candidate-b",
                _operation(MergeConflictKind.SAME_NODE_CONTENT, "b"),
            ),
        )
    )
    work = build_resolution_work_item(
        parent_set_id="set",
        lca_candidate_id="root",
        base_snapshot_hash="0" * 64,
        base_content_hash="1" * 64,
        conflicts=conflicts,
        node_preconditions={
            node_id: "0" * 64
            for node_id in {node for item in conflicts for node in item.node_ids}
        },
    )
    submission = MergeResolutionSubmission(
        work_id=work.work_id,
        operations=(
            AddFile(
                operation_id="op-unrelated",
                op=PatchOperationKind.ADD_FILE,
                path="references/unrelated.md",
                precondition_hash="absent",
                evidence_refs=("fixture",),
                expected_benefit="none",
                regression_risk=RegressionRisk.HIGH,
                rationale="malicious unrelated edit",
                content="# unrelated",
            ),
        ),
        resolved_conflict_ids=tuple(item.conflict_id for item in conflicts),
        rationale="invalid",
    )
    with pytest.raises(ValueError, match="unrelated path"):
        validate_resolution_submission(work, submission)


def test_resolution_rejects_stale_lca_precondition() -> None:
    left = _operation(MergeConflictKind.SAME_NODE_CONTENT, "a")
    right = _operation(MergeConflictKind.SAME_NODE_CONTENT, "b")
    conflicts = detect_conflicts(
        (
            _contribution("candidate-a", left),
            _contribution("candidate-b", right),
        )
    )
    work = build_resolution_work_item(
        parent_set_id="set",
        lca_candidate_id="root",
        base_snapshot_hash="0" * 64,
        base_content_hash="1" * 64,
        conflicts=conflicts,
        node_preconditions={"node-shared": "0" * 64},
    )
    submission = MergeResolutionSubmission(
        work_id=work.work_id,
        operations=(left,),
        resolved_conflict_ids=tuple(item.conflict_id for item in conflicts),
        rationale="stale parent operation must not be accepted as an LCA resolution",
    )
    with pytest.raises(ValueError, match="stale precondition"):
        validate_resolution_submission(work, submission)
