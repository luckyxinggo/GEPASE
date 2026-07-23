"""Deterministic conflict-free union rebased onto the shared LCA."""

from __future__ import annotations

from collections import defaultdict

from gepase.mutation.schema import (
    ABSENT_PRECONDITION,
    AddFile,
    InsertReference,
    PackagePatch,
    PatchEditBudget,
    PatchOperation,
    package_patch_from_proposal,
)
from gepase.optimizer.candidate import PackageCandidate
from gepase.optimizer.merge.conflicts import detect_conflicts
from gepase.optimizer.merge.models import (
    ContributionSource,
    MergeContributionMap,
    ParentContribution,
)
from gepase.package.ir import PackageGraph


def _signature(operation: PatchOperation) -> tuple[str, str, str, str]:
    return (
        operation.op.value,
        operation.path,
        str(getattr(operation, "target_node_id", "")),
        str(getattr(operation, "replacement", getattr(operation, "content", ""))),
    )


def deterministic_merge_patch(
    lca: PackageCandidate,
    graph: PackageGraph,
    contributions: tuple[ParentContribution, ...],
    *,
    parent_set_id: str,
) -> tuple[PackagePatch, MergeContributionMap]:
    conflicts = detect_conflicts(contributions)
    if conflicts:
        raise ValueError(f"deterministic merge has {len(conflicts)} unresolved conflicts")
    nodes = {node.node_id: node for node in graph.nodes}
    grouped: defaultdict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    representative: dict[tuple[str, str, str, str], PatchOperation] = {}
    for contribution in contributions:
        for operation in contribution.operations:
            signature = _signature(operation)
            representative.setdefault(signature, operation)
            grouped[signature].append(contribution.parent_candidate_id)
    operations: list[PatchOperation] = []
    sources: list[ContributionSource] = []
    for index, signature in enumerate(sorted(representative), start=1):
        operation = representative[signature]
        target_id = getattr(operation, "target_node_id", None)
        precondition = operation.precondition_hash
        if target_id is not None:
            node = nodes.get(str(target_id))
            if node is None:
                raise ValueError(f"merge operation target is absent from LCA: {target_id}")
            precondition = node.content_hash
        elif not isinstance(operation, (AddFile, InsertReference)):
            raise ValueError("non-additive merge operation lacks a target node")
        else:
            precondition = ABSENT_PRECONDITION
        merged = operation.model_copy(
            update={
                "operation_id": f"op-merge-{index:03d}",
                "precondition_hash": precondition,
            }
        )
        operations.append(merged)
        sources.append(
            ContributionSource(
                operation_id=merged.operation_id,
                path=merged.path,
                target_node_id=str(target_id) if target_id is not None else None,
                source_parent_candidate_ids=tuple(sorted(set(grouped[signature]))),
            )
        )
    selected_nodes = {
        str(item.target_node_id)
        for item in operations
        if getattr(item, "target_node_id", None) is not None
    }
    selected_nodes.update(
        item.referenced_from_node_id for item in operations if isinstance(item, InsertReference)
    )
    if not selected_nodes:
        selected_nodes.update(
            node_id for contribution in contributions for node_id in contribution.mutation_node_ids
        )
    paths = {item.path for item in operations}
    additions = sum(isinstance(item, (AddFile, InsertReference)) for item in operations)
    from gepase.mutation.schema import DeleteFile

    deletions = sum(isinstance(item, DeleteFile) for item in operations)
    replacement_chars = sum(
        len(str(getattr(item, "replacement", getattr(item, "content", "")))) for item in operations
    )
    evidence = tuple(
        sorted(
            {
                f"parent-set:{parent_set_id}",
                *(ref for contribution in contributions for ref in contribution.evidence_refs),
            }
        )
    )
    patch = package_patch_from_proposal(
        {
            "proposal_work_id": f"merge-union-{parent_set_id}",
            "base_candidate_id": lca.candidate_id,
            "base_snapshot_hash": lca.snapshot_hash,
            "base_content_hash": lca.content_hash,
            "selector": "package_aware_pareto_dependency_closed_union",
            "selected_node_ids": sorted(selected_nodes),
            "operations": [item.model_dump(mode="json") for item in operations],
            "edit_budget": PatchEditBudget(
                max_operations=max(1, len(operations)),
                max_changed_files=max(1, len(paths)),
                max_added_files=additions,
                max_deleted_files=deletions,
                max_total_replacement_chars=max(1, replacement_chars),
                allow_script_edits=True,
                allow_file_topology_edits=True,
            ).model_dump(mode="json"),
            "evidence_refs": evidence,
            "summary": (
                f"Dependency-closed deterministic union of {len(contributions)} "
                f"independent branches from {parent_set_id}."
            ),
        }
    )
    return patch, MergeContributionMap(
        lca_candidate_id=lca.candidate_id,
        sources=tuple(sources),
    )
