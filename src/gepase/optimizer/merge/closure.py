"""LCA-relative mutation subgraphs and dependency-closed contributions."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable

from gepase.mutation.schema import (
    AddFile,
    InsertReference,
    PackagePatch,
    PatchOperation,
)
from gepase.optimizer.evolution.models import MergeParentCandidate
from gepase.optimizer.merge.models import ParentContribution
from gepase.package.ir import EdgeKind, PackageGraph

DEPENDENCY_EDGE_KINDS = {
    EdgeKind.CONTAINS,
    EdgeKind.REFERENCES,
    EdgeKind.IMPORTS,
    EdgeKind.CALLS,
    EdgeKind.EXECUTES,
    EdgeKind.TESTS,
    EdgeKind.READS,
    EdgeKind.REQUIRES_HOST,
    EdgeKind.USES_TOOL,
    EdgeKind.CALLS_EXTERNAL_SERVICE,
    EdgeKind.REQUIRES_SECRET,
}


def dependency_closure(
    graph: PackageGraph,
    seed_node_ids: set[str],
) -> tuple[str, ...]:
    """Close a mutation over package containment and required interfaces.

    Containment is traversed in both directions so an edited function carries
    its file/module context and an edited instruction carries its containing
    section/file. Typed structural dependency edges are traversed outward; incoming
    TESTS/REFERENCES/IMPORTS edges are retained because they encode consumers
    that must remain compatible.
    """

    known = {node.node_id for node in graph.nodes}
    missing = seed_node_ids - known
    if missing:
        raise ValueError(f"mutation contribution references unknown nodes: {sorted(missing)}")
    outgoing: defaultdict[str, list[tuple[str, EdgeKind]]] = defaultdict(list)
    incoming: defaultdict[str, list[tuple[str, EdgeKind]]] = defaultdict(list)
    for edge in graph.edges:
        # Agent semantic hypotheses are deliberately excluded from merge,
        # dependency and safety authorization, regardless of confidence.
        if edge.layer in {"static", "observed"} and edge.kind in DEPENDENCY_EDGE_KINDS:
            outgoing[edge.source].append((edge.target, edge.kind))
            incoming[edge.target].append((edge.source, edge.kind))
    seen = set(seed_node_ids)
    queue = deque((node_id, True) for node_id in sorted(seed_node_ids))
    while queue:
        current, expand_descendants = queue.popleft()
        neighbours: list[tuple[str, EdgeKind, bool]] = []
        if expand_descendants:
            neighbours.extend((node_id, kind, True) for node_id, kind in outgoing[current])
        neighbours.extend(
            (node_id, kind, kind is not EdgeKind.CONTAINS)
            for node_id, kind in incoming[current]
            if kind
            in {
                EdgeKind.CONTAINS,
                EdgeKind.TESTS,
                EdgeKind.REFERENCES,
                EdgeKind.IMPORTS,
            }
        )
        for node_id, _, allow_descendants in sorted(
            neighbours,
            key=lambda item: (item[1].value, item[0], item[2]),
        ):
            if node_id not in seen:
                seen.add(node_id)
                queue.append((node_id, allow_descendants))
    return tuple(sorted(seen))


def _operation_key(operation: PatchOperation) -> tuple[str, str]:
    target = getattr(operation, "target_node_id", None)
    return (operation.path, str(target or operation.op.value))


def parent_contribution(
    parent: MergeParentCandidate,
    *,
    lca_candidate_id: str,
    graph: PackageGraph,
    patch_for_candidate: Callable[[str], PackagePatch],
) -> ParentContribution:
    try:
        lca_index = parent.ancestor_chain.index(lca_candidate_id)
    except ValueError as error:
        raise ValueError("LCA is absent from parent ancestry proof") from error
    branch_candidates = parent.ancestor_chain[lca_index + 1 :]
    if not branch_candidates:
        raise ValueError("merge parent has no mutation relative to LCA")
    patch_rows = tuple(
        (candidate_id, patch_for_candidate(candidate_id)) for candidate_id in branch_candidates
    )
    latest: dict[tuple[str, str], PatchOperation] = {}
    patch_ids: list[str] = []
    for _, patch in patch_rows:
        patch_ids.append(patch.patch_id)
        for operation in patch.operations:
            latest[_operation_key(operation)] = operation
    operations = tuple(
        sorted(
            latest.values(),
            key=lambda item: (
                item.path,
                str(getattr(item, "target_node_id", "")),
                item.op.value,
                item.operation_id,
            ),
        )
    )
    mutation_ids = {
        str(operation.target_node_id)
        for operation in operations
        if getattr(operation, "target_node_id", None) is not None
    }
    mutation_ids.update(
        operation.referenced_from_node_id
        for operation in operations
        if isinstance(operation, InsertReference)
    )
    if not mutation_ids and any(isinstance(operation, AddFile) for operation in operations):
        mutation_ids.update(parent.contribution.component_ids)
    if not mutation_ids:
        raise ValueError("relative mutation has no graph-bound causal node")
    closed = dependency_closure(graph, mutation_ids)
    evidence = {
        *parent.train_evidence_refs,
        *(ref for patch in (row[1] for row in patch_rows) for ref in patch.evidence_refs),
        *(ref for operation in operations for ref in operation.evidence_refs),
    }
    return ParentContribution(
        parent_candidate_id=parent.identity.candidate_id,
        patch_ids=tuple(patch_ids),
        operations=operations,
        mutation_node_ids=tuple(sorted(mutation_ids)),
        dependency_node_ids=tuple(sorted(set(closed) - mutation_ids)),
        closure_node_ids=closed,
        evidence_refs=tuple(sorted(evidence)),
    )
