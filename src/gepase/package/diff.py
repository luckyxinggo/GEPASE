"""Stable-node graph diff, affected closure, and blast-radius computation."""

from __future__ import annotations

from collections import defaultdict, deque

from gepase.package.ir import EdgeKind, PackageGraph, PackageGraphDiff

_PROPAGATING = {
    EdgeKind.CONTAINS,
    EdgeKind.REFERENCES,
    EdgeKind.IMPORTS,
    EdgeKind.CALLS,
    EdgeKind.EXECUTES,
    EdgeKind.TESTS,
}


def graph_diff(before: PackageGraph, after: PackageGraph) -> PackageGraphDiff:
    before_nodes = {node.node_id: node for node in before.nodes}
    after_nodes = {node.node_id: node for node in after.nodes}
    added = set(after_nodes) - set(before_nodes)
    removed = set(before_nodes) - set(after_nodes)
    modified = {
        node_id
        for node_id in set(before_nodes) & set(after_nodes)
        if before_nodes[node_id].content_hash != after_nodes[node_id].content_hash
    }
    before_edges = {edge.edge_id for edge in before.edges}
    after_edges = {edge.edge_id for edge in after.edges}
    seeds = set(added) | set(removed) | modified
    adjacency: defaultdict[str, set[str]] = defaultdict(set)
    for edge in (*before.edges, *after.edges):
        if edge.kind in _PROPAGATING:
            adjacency[edge.source].add(edge.target)
            adjacency[edge.target].add(edge.source)
    affected = set(seeds)
    queue: deque[str] = deque(sorted(seeds))
    while queue:
        node_id = queue.popleft()
        for neighbor in adjacency[node_id]:
            if neighbor not in affected:
                affected.add(neighbor)
                queue.append(neighbor)
    return PackageGraphDiff(
        before_snapshot=before.snapshot_hash,
        after_snapshot=after.snapshot_hash,
        added_nodes=tuple(sorted(added)),
        removed_nodes=tuple(sorted(removed)),
        modified_nodes=tuple(sorted(modified)),
        added_edges=tuple(sorted(after_edges - before_edges)),
        removed_edges=tuple(sorted(before_edges - after_edges)),
        affected_closure=tuple(sorted(affected)),
        blast_radius=len(affected),
    )
