"""Explainable graph-guided mutation target ranking."""

from __future__ import annotations

from collections import Counter, defaultdict, deque

from pydantic import Field

from gepase.optimizer.selectors import (
    ComponentSelector,
    FeatureContribution,
    RankedSelection,
    SelectionContext,
    SelectionResult,
    SelectorKind,
    _evidence,
    _result,
)
from gepase.package.ir import EdgeKind
from gepase.schemas.common import FrozenModel


class GraphSelectorWeights(FrozenModel):
    failure_coverage: float = Field(default=3.0, ge=0)
    inverse_distance: float = Field(default=2.0, ge=0)
    dynamic_access: float = Field(default=1.4, ge=0)
    diagnostic_severity: float = Field(default=1.2, ge=0)
    historical_yield: float = Field(default=0.8, ge=0)
    exploration_bonus: float = Field(default=0.6, ge=0)
    fan_out_risk: float = Field(default=-1.0, le=0)
    historical_regression: float = Field(default=-1.5, le=0)


def _distances(context: SelectionContext) -> dict[str, int]:
    seeds = {seed for item in context.failure_slices for seed in item.seed_node_ids}
    adjacency: defaultdict[str, set[str]] = defaultdict(set)
    traversable = {
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
    for edge in context.graph.edges:
        if edge.kind in traversable:
            adjacency[edge.source].add(edge.target)
            adjacency[edge.target].add(edge.source)
    distance = {seed: 0 for seed in seeds}
    queue: deque[str] = deque(sorted(seeds))
    while queue:
        current = queue.popleft()
        for neighbor in sorted(adjacency[current]):
            if neighbor not in distance:
                distance[neighbor] = distance[current] + 1
                queue.append(neighbor)
    return distance


class GraphGuidedComponentSelector(ComponentSelector):
    kind = SelectorKind.GRAPH_GUIDED

    def __init__(self, weights: GraphSelectorWeights | None = None) -> None:
        self.weights = weights or GraphSelectorWeights()

    def select(self, context: SelectionContext, *, limit: int) -> SelectionResult:
        if limit < 1:
            raise ValueError("selection limit must be positive")
        by_id = {node.node_id: node for node in context.graph.nodes}
        distances = _distances(context)
        fan_out: Counter[str] = Counter()
        dynamic: Counter[str] = Counter()
        for edge in context.graph.edges:
            fan_out[edge.source] += 1
            if edge.layer in {"planned", "observed"}:
                dynamic[edge.source] += edge.count
                dynamic[edge.target] += edge.count
        max_fan = max(fan_out.values(), default=1)
        max_dynamic = max(dynamic.values(), default=1)
        slice_paths: list[set[str]] = []
        for failure_slice in context.failure_slices:
            slice_paths.append(
                {by_id[item.node_id].path for item in failure_slice.nodes if item.node_id in by_id}
            )
        evidence = _evidence(context)
        scored: list[tuple[float, str, str, RankedSelection]] = []
        for target in context.targets:
            coverage = (
                sum(target.path in paths for paths in slice_paths) / len(slice_paths)
                if slice_paths
                else 0.0
            )
            distance = distances.get(target.node_id)
            if distance is None:
                path_distances = [
                    value
                    for node_id, value in distances.items()
                    if by_id.get(node_id) is not None and by_id[node_id].path == target.path
                ]
                distance = min(path_distances) if path_distances else 100
            inverse_distance = 1.0 / (1.0 + distance) if distance < 100 else 0.0
            access = dynamic[target.node_id] / max_dynamic
            severity = max(
                context.diagnostic_severity.get(target.node_id, 0.0),
                max(
                    (
                        context.diagnostic_severity.get(node_id, 0.0)
                        for node_id, node in by_id.items()
                        if node.path == target.path
                    ),
                    default=0.0,
                ),
            )
            attempts = context.total_attempts.get(target.node_id, 0)
            accepted = context.accepted_attempts.get(target.node_id, 0)
            historical_yield = accepted / attempts if attempts else 0.5
            exploration = 1.0 / (1.0 + context.exploration_count.get(target.node_id, 0))
            risk = fan_out[target.node_id] / max_fan
            regression = max(0.0, context.regression_loss.get(target.node_id, 0.0))
            values = {
                "failure_coverage": coverage,
                "inverse_distance": inverse_distance,
                "dynamic_access": access,
                "diagnostic_severity": severity,
                "historical_yield": historical_yield,
                "exploration_bonus": exploration,
                "fan_out_risk": risk,
                "historical_regression": regression,
            }
            contributions = tuple(
                FeatureContribution(
                    feature=name,
                    raw_value=round(value, 8),
                    weight=float(getattr(self.weights, name)),
                    contribution=round(value * float(getattr(self.weights, name)), 8),
                )
                for name, value in values.items()
            )
            total = sum(item.contribution for item in contributions)
            row = RankedSelection(
                rank=1,
                node_id=target.node_id,
                path=target.path,
                locator=target.locator,
                score=round(total, 8),
                contributions=contributions,
                evidence_refs=evidence,
                reason_code="graph_failure_impact_priority",
                high_blast_radius=risk >= 0.65,
            )
            scored.append((total, target.path, target.node_id, row))
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        chosen = tuple(
            item[3].model_copy(update={"rank": index})
            for index, item in enumerate(scored[: min(limit, len(scored))], 1)
        )
        return _result(self.kind, context, limit, chosen)
