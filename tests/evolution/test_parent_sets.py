from __future__ import annotations

from gepase.optimizer.evolution.models import (
    EvolutionCandidateIdentity,
    ExclusiveContribution,
    MergeParentCandidate,
)
from gepase.optimizer.evolution.parent_sets import (
    enumerate_parent_sets,
    parent_set_contract_audit,
)
from gepase.optimizer.evolution.selection_lock import build_breeding_snapshot


def _package(
    package: str,
    snapshot: str,
    hash_offset: int,
) -> tuple[list[MergeParentCandidate], list[EvolutionCandidateIdentity]]:
    root_id = f"{package}-root"
    root = EvolutionCandidateIdentity(
        candidate_id=root_id,
        package_id=package,
        source_package_ref=f"skills/{package}",
        source_snapshot_hash=snapshot,
        lineage_root_candidate_id=root_id,
        generation=0,
        operator="seed",
        content_hash=f"{hash_offset:064x}",
    )
    parents = []
    lineage = [root]
    for index in range(2):
        candidate_id = f"{package}-candidate-{index}"
        identity = EvolutionCandidateIdentity(
            candidate_id=candidate_id,
            package_id=package,
            source_package_ref=f"skills/{package}",
            source_snapshot_hash=snapshot,
            lineage_root_candidate_id=root_id,
            parent_ids=(root_id,),
            branch_id=f"{package}-branch-{index}",
            branch_root_candidate_id=candidate_id,
            generation=1,
            operator="reflective_mutation",
            content_hash=f"{hash_offset + index + 1:064x}",
            failure_cluster_ids=(f"{package}-cluster-{index}",),
        )
        lineage.append(identity)
        parents.append(
            MergeParentCandidate(
                identity=identity,
                patch_id=f"{package}-patch-{index}",
                ancestor_chain=(root_id, candidate_id),
                contribution=ExclusiveContribution(
                    task_keys=(f"{package}-task-{index}",),
                    closure_ids=(f"{package}-closure-{index}",),
                ),
                train_evidence_refs=(f"train/{package}/e1-{index}",),
                gate_0_1_passed=True,
                train_floor_satisfied=True,
            )
        )
    return parents, lineage


def test_parent_sets_are_same_package_same_root_and_cross_package_is_never_merge() -> None:
    parents_a, lineage_a = _package("package-a", "a" * 64, 10)
    parents_b, lineage_b = _package("package-b", "b" * 64, 20)
    snapshot = build_breeding_snapshot(
        tuple((*parents_a, *parents_b)),
        tuple((*lineage_a, *lineage_b)),
        selection_config_hash="f" * 64,
        selection_feature_names=("train_mean", "exclusive_contribution"),
    )
    report = enumerate_parent_sets(snapshot)
    audit = parent_set_contract_audit(report)

    assert report.merge_compatible_parent_set_count == 2
    assert report.merge_compatible_package_count == 2
    assert report.cross_package_pairs_observed == 4
    assert report.cross_package_pairs_counted_as_merge == 0
    assert all(
        len({parent.identity.package_id for parent in row.parent_set.parents}) == 1
        for row in report.ranked_parent_sets
    )
    assert all(
        row.parent_set.compatibility_report
        and row.parent_set.compatibility_report.merge_input_compatible
        for row in report.ranked_parent_sets
    )
    assert audit["valid"]
    assert audit["all_compatible_sets_pass_contract"]
