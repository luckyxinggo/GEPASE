from __future__ import annotations

import pytest
from pydantic import ValidationError

from gepase.optimizer.evolution.lineage import CandidateAncestryIndex
from gepase.optimizer.evolution.models import (
    BreedingSnapshot,
    EvolutionCandidateIdentity,
    ExclusiveContribution,
    FailureCluster,
    MergeParentCandidate,
    MutationBranch,
)


def _identity(
    candidate_id: str,
    *,
    parent_ids: tuple[str, ...] = (),
    generation: int = 0,
    branch_id: str | None = None,
    branch_root: str | None = None,
    content: str = "a",
) -> EvolutionCandidateIdentity:
    return EvolutionCandidateIdentity(
        candidate_id=candidate_id,
        package_id="package-demo",
        source_package_ref="skills/demo",
        source_snapshot_hash="1" * 64,
        lineage_root_candidate_id="root",
        parent_ids=parent_ids,
        branch_id=branch_id,
        branch_root_candidate_id=branch_root,
        generation=generation,
        operator="seed" if generation == 0 else "reflective_mutation",
        content_hash=content * 64,
        failure_cluster_ids=() if generation == 0 else ("cluster-a",),
    )


def _parent(identity: EvolutionCandidateIdentity, chain: tuple[str, ...]) -> MergeParentCandidate:
    return MergeParentCandidate(
        identity=identity,
        patch_id=f"patch-{identity.candidate_id}",
        ancestor_chain=chain,
        contribution=ExclusiveContribution(task_keys=(f"task-{identity.candidate_id}",)),
        train_evidence_refs=(f"train-{identity.candidate_id}",),
        gate_0_1_passed=True,
        train_floor_satisfied=True,
    )


def test_failure_cluster_and_branch_are_strongly_typed() -> None:
    cluster = FailureCluster(
        cluster_id="cluster-a",
        package_id="package-demo",
        source_snapshot_hash="1" * 64,
        evidence_refs=("evidence-a", "evidence-b"),
        representative_task_ids=("task-a",),
        oracle_refs=("oracle-a",),
        target_metric="execution_correctness",
        causal_node_ids=("node-a",),
        allowed_operations=("replace_markdown_block",),
        support_count=2,
        severity=0.8,
        confidence=0.9,
        expected_benefit="repair the repeated failure",
        blast_radius=1,
    )
    branch = MutationBranch(
        branch_id="branch-a",
        package_id=cluster.package_id,
        source_snapshot_hash=cluster.source_snapshot_hash,
        lineage_root_candidate_id="root",
        branch_root_candidate_id="candidate-a",
        failure_cluster_id=cluster.cluster_id,
        candidate_ids=("candidate-a", "candidate-a2"),
    )
    assert branch.failure_cluster_id == cluster.cluster_id
    with pytest.raises(ValidationError, match="support_count"):
        cluster.model_copy(update={"support_count": 1}, deep=True).__class__(
            **{**cluster.model_dump(), "support_count": 1}
        )


def test_lineage_queries_use_explicit_edges() -> None:
    root = _identity("root", content="0")
    branch_a = _identity(
        "candidate-a",
        parent_ids=("root",),
        generation=1,
        branch_id="branch-a",
        branch_root="candidate-a",
        content="a",
    )
    branch_a2 = _identity(
        "candidate-a2",
        parent_ids=("candidate-a",),
        generation=2,
        branch_id="branch-a",
        branch_root="candidate-a",
        content="c",
    )
    branch_b = _identity(
        "candidate-b",
        parent_ids=("root",),
        generation=1,
        branch_id="branch-b",
        branch_root="candidate-b",
        content="b",
    )
    index = CandidateAncestryIndex((root, branch_a, branch_a2, branch_b))
    assert index.is_ancestor("candidate-a", "candidate-a2")
    assert not index.is_ancestor("candidate-a2", "candidate-a")
    assert index.lowest_common_ancestor(("candidate-a2", "candidate-b")) == "root"
    assert index.first_divergent_child("root", "candidate-a2") == "candidate-a"
    assert index.root_to_candidate_chain("candidate-a2") == (
        "root",
        "candidate-a",
        "candidate-a2",
    )


def test_breeding_snapshot_physically_rejects_held_out_evidence() -> None:
    root = _identity("root", content="0")
    candidate = _identity(
        "candidate-a",
        parent_ids=("root",),
        generation=1,
        branch_id="branch-a",
        branch_root="candidate-a",
    )
    with pytest.raises(ValidationError, match="held-out"):
        BreedingSnapshot(
            snapshot_id="snapshot-a",
            candidates=(_parent(candidate, ("root", "candidate-a")),),
            lineage=(root, candidate),
            selection_config_hash="f" * 64,
            train_evidence_refs=("validation-record-a",),
        )


def test_lineage_index_rejects_missing_parent_instead_of_inferring_it() -> None:
    candidate = _identity(
        "candidate-a",
        parent_ids=("missing-root",),
        generation=1,
        branch_id="branch-a",
        branch_root="candidate-a",
    )
    with pytest.raises(ValueError, match=r"missing explicit lineage root|parent"):
        CandidateAncestryIndex((candidate,))
