from __future__ import annotations

import json
from pathlib import Path

import pytest

from gepase.optimizer.evolution.models import (
    EvolutionCandidateIdentity,
    ExclusiveContribution,
    MergeCompatibilityReason,
    MergeEligibility,
    MergeParentCandidate,
    MergeParentSetSnapshot,
)
from gepase.optimizer.merge.parent_contract import validate_merge_parent_set

FIXTURES = Path(__file__).parents[1] / "fixtures" / "evolution_parent_sets"


def _identity(
    candidate_id: str,
    *,
    package_id: str = "package-demo",
    source_ref: str = "skills/demo",
    snapshot: str = "1",
    root_id: str = "root",
    parents: tuple[str, ...] = (),
    generation: int = 0,
    branch_id: str | None = None,
    branch_root: str | None = None,
    content: str = "0",
) -> EvolutionCandidateIdentity:
    return EvolutionCandidateIdentity(
        candidate_id=candidate_id,
        package_id=package_id,
        source_package_ref=source_ref,
        source_snapshot_hash=snapshot * 64,
        lineage_root_candidate_id=root_id,
        parent_ids=parents,
        branch_id=branch_id,
        branch_root_candidate_id=branch_root,
        generation=generation,
        operator="seed" if generation == 0 else "reflective_mutation",
        content_hash=content * 64,
        failure_cluster_ids=() if generation == 0 else (f"cluster-{branch_id}",),
    )


def _parent(
    identity: EvolutionCandidateIdentity,
    chain: tuple[str, ...],
    *,
    contribution: ExclusiveContribution | None = None,
) -> MergeParentCandidate:
    return MergeParentCandidate(
        identity=identity,
        patch_id=f"patch-{identity.candidate_id}",
        ancestor_chain=chain,
        contribution=contribution
        or ExclusiveContribution(task_keys=(f"task-{identity.candidate_id}",)),
        train_evidence_refs=(f"train-{identity.candidate_id}",),
        gate_0_1_passed=True,
        train_floor_satisfied=True,
    )


def _snapshot(case_id: str) -> MergeParentSetSnapshot:
    root = _identity("root")
    left = _identity(
        "candidate-a",
        parents=("root",),
        generation=1,
        branch_id="branch-a",
        branch_root="candidate-a",
        content="a",
    )
    right = _identity(
        "candidate-b",
        parents=("root",),
        generation=1,
        branch_id="branch-b",
        branch_root="candidate-b",
        content="b",
    )
    lineage: tuple[EvolutionCandidateIdentity, ...] = (root, left, right)
    parents = (
        _parent(left, ("root", "candidate-a")),
        _parent(right, ("root", "candidate-b")),
    )

    if case_id in {"cross_package", "different_snapshot", "different_root"}:
        package_id = "package-other" if case_id == "cross_package" else "package-demo"
        source_ref = "skills/other" if case_id == "cross_package" else "skills/demo"
        snapshot = "2" if case_id == "different_snapshot" else "1"
        second_root_id = "root-b"
        second_root = _identity(
            second_root_id,
            package_id=package_id,
            source_ref=source_ref,
            snapshot=snapshot,
            root_id=second_root_id,
            content="8",
        )
        right = _identity(
            "candidate-b",
            package_id=package_id,
            source_ref=source_ref,
            snapshot=snapshot,
            root_id=second_root_id,
            parents=(second_root_id,),
            generation=1,
            branch_id="branch-b",
            branch_root="candidate-b",
            content="b",
        )
        lineage = (root, left, second_root, right)
        parents = (
            _parent(left, ("root", "candidate-a")),
            _parent(right, (second_root_id, "candidate-b")),
        )
    elif case_id == "ancestor_descendant":
        descendant = _identity(
            "candidate-a2",
            parents=("candidate-a",),
            generation=2,
            branch_id="branch-a",
            branch_root="candidate-a",
            content="c",
        )
        lineage = (root, left, descendant)
        parents = (
            _parent(left, ("root", "candidate-a")),
            _parent(descendant, ("root", "candidate-a", "candidate-a2")),
        )
    elif case_id == "same_branch":
        right = right.model_copy(
            update={"branch_id": "branch-a"},
        )
        lineage = (root, left, right)
        parents = (
            _parent(left, ("root", "candidate-a")),
            _parent(right, ("root", "candidate-b")),
        )
    elif case_id == "duplicate_content":
        right = right.model_copy(update={"content_hash": left.content_hash})
        lineage = (root, left, right)
        parents = (
            _parent(left, ("root", "candidate-a")),
            _parent(right, ("root", "candidate-b")),
        )
    elif case_id == "empty_contribution":
        parents = (
            parents[0],
            _parent(
                right,
                ("root", "candidate-b"),
                contribution=ExclusiveContribution(),
            ),
        )
    elif case_id == "duplicate_contribution":
        repeated = ExclusiveContribution(task_keys=("same-task",))
        parents = (
            _parent(left, ("root", "candidate-a"), contribution=repeated),
            _parent(right, ("root", "candidate-b"), contribution=repeated),
        )
    elif case_id == "duplicate_candidate":
        parents = (parents[0], parents[0])

    return MergeParentSetSnapshot(
        parent_set_id=f"parent-set-{case_id}",
        parents=parents,
        lineage=lineage,
        selection_config_hash="f" * 64,
        train_selection_evidence_refs=("train-selection-lock",),
    )


@pytest.mark.parametrize(
    "fixture",
    sorted(FIXTURES.glob("*.json")),
    ids=lambda path: path.stem,
)
def test_parent_contract_fixtures_have_zero_false_accept_or_reject(
    fixture: Path,
) -> None:
    case = json.loads(fixture.read_text(encoding="utf-8"))
    report = validate_merge_parent_set(_snapshot(case["case_id"]))
    assert report.merge_input_compatible is case["expected_compatible"]
    assert MergeCompatibilityReason(case["expected_reason"]) in report.reason_codes


def test_valid_parent_set_recomputes_lca_and_first_divergent_children() -> None:
    report = validate_merge_parent_set(_snapshot("valid_same_root_divergent_branch"))
    assert report.merge_input_compatible
    assert report.lca_candidate_id == "root"
    assert report.first_divergent_child_ids == ("candidate-a", "candidate-b")
    assert report.ancestor_relation is False


def test_unknown_legacy_parent_is_never_silently_promoted() -> None:
    snapshot = _snapshot("valid_same_root_divergent_branch")
    legacy_parent = snapshot.parents[0].model_copy(
        update={"merge_eligibility": MergeEligibility.UNKNOWN_LEGACY}
    )
    report = validate_merge_parent_set(
        snapshot.model_copy(update={"parents": (legacy_parent, snapshot.parents[1])})
    )
    assert not report.merge_input_compatible
    assert MergeCompatibilityReason.LEGACY_IDENTITY_UNKNOWN in report.reason_codes
