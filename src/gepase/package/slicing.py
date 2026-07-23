"""Bounded reverse dependency slicing from failure evidence to package nodes."""

from __future__ import annotations

from collections import defaultdict, deque

from gepase.package.ir import (
    EdgeKind,
    FailureSlice,
    FailureSliceNode,
    NodeKind,
    PackageGraph,
)

_REVERSE_KINDS = {
    EdgeKind.CONTAINS,
    EdgeKind.REFERENCES,
    EdgeKind.IMPORTS,
    EdgeKind.CALLS,
    EdgeKind.EXECUTES,
    EdgeKind.TESTS,
    EdgeKind.READS,
    EdgeKind.PLANNED_READ,
    EdgeKind.PLANNED_EXECUTE,
    EdgeKind.OBSERVED_READ,
    EdgeKind.OBSERVED_EXECUTE,
    EdgeKind.FAILED_AT,
}


def reverse_slice(
    graph: PackageGraph,
    seed_node_ids: tuple[str, ...],
    *,
    max_nodes: int = 20,
    max_tokens: int = 2_000,
) -> FailureSlice:
    by_id = {node.node_id: node for node in graph.nodes}
    unknown = set(seed_node_ids) - set(by_id)
    if unknown:
        raise ValueError(f"unknown failure slice seeds: {sorted(unknown)}")
    predecessors: defaultdict[str, list[tuple[str, EdgeKind, float]]] = defaultdict(list)
    for edge in graph.edges:
        if edge.kind in _REVERSE_KINDS:
            predecessors[edge.target].append((edge.source, edge.kind, edge.confidence))
    distance: dict[str, int] = {node_id: 0 for node_id in seed_node_ids}
    reason: dict[str, str] = {node_id: "failure seed" for node_id in seed_node_ids}
    queue: deque[str] = deque(seed_node_ids)
    while queue:
        target = queue.popleft()
        for source, kind, _confidence_value in predecessors[target]:
            if source in distance:
                continue
            distance[source] = distance[target] + 1
            reason[source] = f"reverse {kind.value} dependency of {target}"
            queue.append(source)
    candidates = []
    for node_id, hop in distance.items():
        node = by_id[node_id]
        type_bonus = (
            0.2
            if node.kind in {NodeKind.FILE, NodeKind.INSTRUCTION, NodeKind.FUNCTION}
            else 0
        )
        evidence_bonus = 0.15 if node.kind is NodeKind.EVIDENCE else 0
        score = 1 / (1 + hop) + type_bonus + evidence_bonus
        candidates.append((score, hop, node_id))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    selected: list[FailureSliceNode] = []
    token_estimate = 0
    for score, hop, node_id in candidates:
        node = by_id[node_id]
        node_tokens = max(1, (len(node.label) + len(str(node.metadata))) // 4)
        if len(selected) >= max_nodes or token_estimate + node_tokens > max_tokens:
            continue
        token_estimate += node_tokens
        selected.append(
            FailureSliceNode(
                node_id=node_id,
                rank=len(selected) + 1,
                distance=hop,
                score=round(score, 6),
                reason=reason[node_id],
            )
        )
    return FailureSlice(
        package_id=graph.package_id,
        seed_node_ids=seed_node_ids,
        nodes=tuple(selected),
        omitted_nodes=max(0, len(candidates) - len(selected)),
        token_estimate=token_estimate,
    )
