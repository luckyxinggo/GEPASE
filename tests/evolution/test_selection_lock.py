from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gepase.optimizer.evolution.models import (
    EvolutionCandidateIdentity,
    ExclusiveContribution,
    MergeParentCandidate,
)
from gepase.optimizer.evolution.selection_lock import (
    audit_breeding_isolation,
    build_breeding_snapshot,
    build_validation_selection_lock,
    verify_selection_lock,
)


def _family() -> tuple[
    tuple[MergeParentCandidate, ...],
    tuple[EvolutionCandidateIdentity, ...],
]:
    root = EvolutionCandidateIdentity(
        candidate_id="root",
        package_id="package",
        source_package_ref="skills/package",
        source_snapshot_hash="a" * 64,
        lineage_root_candidate_id="root",
        generation=0,
        operator="seed",
        content_hash="1" * 64,
    )
    candidates = []
    lineage = [root]
    for index in range(2):
        child = EvolutionCandidateIdentity(
            candidate_id=f"candidate-{index}",
            package_id="package",
            source_package_ref="skills/package",
            source_snapshot_hash="a" * 64,
            lineage_root_candidate_id="root",
            parent_ids=("root",),
            branch_id=f"branch-{index}",
            branch_root_candidate_id=f"candidate-{index}",
            generation=1,
            operator="reflective_mutation",
            content_hash=f"{index + 2:064x}",
            failure_cluster_ids=(f"cluster-{index}",),
        )
        lineage.append(child)
        candidates.append(
            MergeParentCandidate(
                identity=child,
                patch_id=f"patch-{index}",
                ancestor_chain=("root", child.candidate_id),
                contribution=ExclusiveContribution(
                    task_keys=(f"task-{index}",),
                    component_ids=(f"component-{index}",),
                ),
                train_evidence_refs=(f"train/e1-{index}",),
                gate_0_1_passed=True,
                train_floor_satisfied=True,
            )
        )
    return tuple(candidates), tuple(lineage)


def test_snapshot_hash_and_lock_are_deterministic_and_immutable() -> None:
    candidates, lineage = _family()
    first = build_breeding_snapshot(
        candidates,
        lineage,
        selection_config_hash="f" * 64,
        selection_feature_names=("train_mean", "exclusive_task_coverage"),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = build_breeding_snapshot(
        tuple(reversed(candidates)),
        tuple(reversed(lineage)),
        selection_config_hash="f" * 64,
        selection_feature_names=("train_mean", "exclusive_task_coverage"),
        created_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert first.snapshot_id == second.snapshot_id

    lock = build_validation_selection_lock(
        first,
        selection_feature_names=("train_mean", "exclusive_task_coverage"),
        created_at=first.created_at + timedelta(seconds=1),
    )
    assert audit_breeding_isolation(first, lock)["valid"]
    assert verify_selection_lock(lock, candidates)["valid"]

    changed_identity = candidates[0].identity.model_copy(update={"content_hash": "e" * 64})
    changed = candidates[0].model_copy(update={"identity": changed_identity})
    assert not verify_selection_lock(lock, (changed, candidates[1]))["valid"]


def test_held_out_selection_feature_is_rejected() -> None:
    candidates, lineage = _family()
    with pytest.raises(ValueError, match="held-out"):
        build_breeding_snapshot(
            candidates,
            lineage,
            selection_config_hash="f" * 64,
            selection_feature_names=("validation_score",),
        )
