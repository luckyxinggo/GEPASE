"""Bounded same-parent TargetSet contracts for atomic PackagePatch work."""

from __future__ import annotations

from collections import defaultdict, deque

from pydantic import Field, model_validator

from gepase.optimizer.selectors import RankedSelection
from gepase.package.ir import EdgeKind, IRNode, PackageGraph
from gepase.schemas.common import FrozenModel

_CAUSAL_EDGE_KINDS = {
    EdgeKind.CONTAINS,
    EdgeKind.REFERENCES,
    EdgeKind.IMPORTS,
    EdgeKind.CALLS,
    EdgeKind.EXECUTES,
    EdgeKind.READS,
    EdgeKind.OBSERVED_READ,
    EdgeKind.OBSERVED_EXECUTE,
}


class TargetSet(FrozenModel):
    schema_version: str = "1.0.0"
    parent_candidate_id: str
    primary_target_id: str
    companion_target_ids: tuple[str, ...] = Field(default=(), max_length=1)
    causal_path_node_ids: tuple[str, ...] = Field(min_length=1)
    causal_path_edge_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    scope_reason: str = Field(min_length=1, max_length=800)

    @model_validator(mode="after")
    def bounded_unique_targets(self) -> TargetSet:
        targets = (self.primary_target_id, *self.companion_target_ids)
        if len(targets) > 2 or len(targets) != len(set(targets)):
            raise ValueError("TargetSet must contain one or two unique targets")
        if not set(targets) <= set(self.causal_path_node_ids):
            raise ValueError("TargetSet targets must appear on its causal path")
        if len(targets) == 2 and not self.causal_path_edge_ids:
            raise ValueError("two-target TargetSet requires a typed causal graph path")
        return self

    @property
    def target_node_ids(self) -> tuple[str, ...]:
        return (self.primary_target_id, *self.companion_target_ids)


def _causal_path(
    graph: PackageGraph,
    start: str,
    end: str,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    adjacency: defaultdict[str, list[tuple[str, str]]] = defaultdict(list)
    package_nodes = {node.node_id for node in graph.nodes if node.path == "."}
    for edge in graph.edges:
        # Semantic hypotheses may rank a node for exploration, but can never
        # authorize a second target or enlarge atomic patch scope.
        if edge.kind not in _CAUSAL_EDGE_KINDS or edge.layer not in {"static", "observed"}:
            continue
        if edge.source in package_nodes or edge.target in package_nodes:
            continue
        adjacency[edge.source].append((edge.target, edge.edge_id))
        adjacency[edge.target].append((edge.source, edge.edge_id))
    queue: deque[str] = deque([start])
    previous: dict[str, tuple[str, str] | None] = {start: None}
    while queue:
        current = queue.popleft()
        if current == end:
            break
        for neighbor, edge_id in sorted(adjacency[current]):
            if neighbor not in previous:
                previous[neighbor] = (current, edge_id)
                queue.append(neighbor)
    if end not in previous:
        return None
    nodes = [end]
    edges: list[str] = []
    cursor = end
    while previous[cursor] is not None:
        parent, edge_id = previous[cursor]  # type: ignore[misc]
        nodes.append(parent)
        edges.append(edge_id)
        cursor = parent
    return tuple(reversed(nodes)), tuple(reversed(edges))


def _same_mutation_locus(left: IRNode, right: IRNode) -> bool:
    """Reject overlapping selectors that would edit the same physical locus."""

    if left.path != right.path:
        return False
    if left.span is None or right.span is None:
        return True
    return not (
        left.span.end_line < right.span.start_line or right.span.end_line < left.span.start_line
    )


def choose_bounded_target_set(
    graph: PackageGraph,
    ranked: tuple[RankedSelection, ...],
    *,
    parent_candidate_id: str,
    evidence_refs: tuple[str, ...],
    scope_reason: str,
    max_targets: int,
) -> tuple[tuple[RankedSelection, ...], TargetSet | None]:
    """Keep the primary and at most one graph-connected companion."""

    if not ranked:
        raise ValueError("ranked selection is empty")
    if max_targets not in {1, 2}:
        raise ValueError("GH-P0 TargetSet limit must be one or two")
    primary = ranked[0]
    if max_targets == 1:
        return (primary,), None
    nodes = {node.node_id: node for node in graph.nodes}
    primary_node = nodes[primary.node_id]
    for companion in ranked[1:]:
        if _same_mutation_locus(primary_node, nodes[companion.node_id]):
            continue
        path = _causal_path(graph, primary.node_id, companion.node_id)
        if path is None:
            continue
        node_ids, edge_ids = path
        return (
            (primary, companion),
            TargetSet(
                parent_candidate_id=parent_candidate_id,
                primary_target_id=primary.node_id,
                companion_target_ids=(companion.node_id,),
                causal_path_node_ids=node_ids,
                causal_path_edge_ids=edge_ids,
                evidence_refs=evidence_refs,
                scope_reason=scope_reason,
            ),
        )
    return (primary,), None
