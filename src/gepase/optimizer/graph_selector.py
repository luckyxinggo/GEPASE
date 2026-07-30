"""Explainable graph-guided mutation target ranking."""

from __future__ import annotations

from collections import Counter, defaultdict, deque

from pydantic import Field

from gepase.optimizer.selectors import (
    AttributionScope,
    ComponentSelector,
    FeatureContribution,
    FeatureGroup,
    RankedSelection,
    SelectionContext,
    SelectionResult,
    SelectionScoreBreakdown,
    SelectionTarget,
    SelectorKind,
    ValidationIntensity,
    ValidationLevel,
    _evidence,
    _result,
)
from gepase.package.ir import EdgeKind, NodeKind, PackageGraph
from gepase.schemas.common import FrozenModel

_PATH_FALLBACK_DECAY = 0.25
_PATH_FALLBACK_DISTANCE_PENALTY = 2


class GraphSelectorWeights(FrozenModel):
    failure_coverage: float = Field(default=3.0, ge=0)
    inverse_distance: float = Field(default=2.0, ge=0)
    dynamic_access: float = Field(default=1.4, ge=0)
    diagnostic_severity: float = Field(default=1.2, ge=0)
    historical_yield: float = Field(default=0.8, ge=0)
    exploration_bonus: float = Field(default=0.6, ge=0)
    fan_out_risk: float = Field(default=1.0, ge=0)
    historical_regression: float = Field(default=1.5, ge=0)
    risk_ranking_fraction: float = Field(default=0.15, ge=0, le=0.5)
    max_ranking_risk_penalty: float = Field(default=0.35, ge=0, le=1)


class LegacyGraphSelectorWeights(FrozenModel):
    """Frozen v0.1 weights used only for honest offline replay."""

    failure_coverage: float = 3.0
    inverse_distance: float = 2.0
    dynamic_access: float = 1.4
    diagnostic_severity: float = 1.2
    historical_yield: float = 0.8
    exploration_bonus: float = 0.6
    fan_out_risk: float = -1.0
    historical_regression: float = -1.5


def eligible_mutation_targets(graph: PackageGraph) -> tuple[SelectionTarget, ...]:
    """Project one PackageGraph into the sole mutable selector target contract."""

    kinds = {
        NodeKind.FILE,
        NodeKind.FRONTMATTER,
        NodeKind.SECTION,
        NodeKind.INSTRUCTION,
        NodeKind.REFERENCE_CHUNK,
        NodeKind.FUNCTION,
    }
    return tuple(
        SelectionTarget(
            node_id=node.node_id,
            path=node.path,
            locator=node.locator,
            node_kind=node.kind.value,
            content_hash=node.content_hash,
            token_estimate=max(1, (len(node.label) + len(str(node.metadata))) // 4),
        )
        for node in graph.nodes
        if node.mutable
        and (node.span is not None or node.kind is NodeKind.FILE)
        and node.kind in kinds
    )


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
        trusted = edge.layer in {"static", "planned", "observed"} and edge.kind in traversable
        if trusted:
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
            if edge.layer != "semantic_hypothesis":
                fan_out[edge.source] += 1
            if edge.layer == "observed":
                dynamic[edge.source] += edge.count
                dynamic[edge.target] += edge.count
        max_fan = max(fan_out.values(), default=1)
        max_dynamic = max(dynamic.values(), default=1)
        slice_seeds = [
            tuple(seed for seed in failure_slice.seed_node_ids if seed in by_id)
            for failure_slice in context.failure_slices
        ]
        evidence = _evidence(context)
        scored: list[tuple[float, str, str, RankedSelection]] = []
        for target in context.targets:
            coverage_values: list[float] = []
            coverage_sources: set[str] = set()
            coverage_fallback = False
            for seeds in slice_seeds:
                if target.node_id in seeds:
                    coverage_values.append(1.0)
                    coverage_sources.add(target.node_id)
                    continue
                same_path = tuple(seed for seed in seeds if by_id[seed].path == target.path)
                if same_path:
                    coverage_values.append(_PATH_FALLBACK_DECAY)
                    coverage_sources.update(same_path)
                    coverage_fallback = True
                else:
                    coverage_values.append(0.0)
            coverage = sum(coverage_values) / len(coverage_values) if coverage_values else 0.0
            coverage_scope = (
                AttributionScope.EXACT_NODE
                if target.node_id in coverage_sources
                else AttributionScope.PATH_FALLBACK
                if coverage_fallback
                else AttributionScope.NONE
            )
            distance = distances.get(target.node_id)
            distance_sources: tuple[str, ...] = (target.node_id,) if distance is not None else ()
            distance_scope = (
                AttributionScope.EXACT_NODE if distance is not None else AttributionScope.NONE
            )
            if distance is None:
                path_distances = [
                    (node_id, value)
                    for node_id, value in distances.items()
                    if by_id.get(node_id) is not None and by_id[node_id].path == target.path
                ]
                if path_distances:
                    source_id, source_distance = min(
                        path_distances,
                        key=lambda item: (item[1], item[0]),
                    )
                    distance = source_distance + _PATH_FALLBACK_DISTANCE_PENALTY
                    distance_sources = (source_id,)
                    distance_scope = AttributionScope.PATH_FALLBACK
                else:
                    distance = 100
            inverse_distance = 1.0 / (1.0 + distance) if distance < 100 else 0.0
            exact_access = dynamic[target.node_id]
            access_sources: tuple[str, ...] = ()
            access_scope = AttributionScope.NONE
            access_decay = 1.0
            if exact_access:
                access = exact_access / max_dynamic
                access_sources = (target.node_id,)
                access_scope = AttributionScope.EXACT_NODE
            else:
                same_path_access = sorted(
                    (
                        (node_id, value)
                        for node_id, value in dynamic.items()
                        if value
                        and by_id.get(node_id) is not None
                        and by_id[node_id].path == target.path
                    ),
                    key=lambda item: (-item[1], item[0]),
                )
                if same_path_access:
                    access = same_path_access[0][1] / max_dynamic * _PATH_FALLBACK_DECAY
                    access_sources = (same_path_access[0][0],)
                    access_scope = AttributionScope.PATH_FALLBACK
                    access_decay = _PATH_FALLBACK_DECAY
                else:
                    access = 0.0
            exact_severity = context.diagnostic_severity.get(target.node_id, 0.0)
            severity_sources: tuple[str, ...] = ()
            severity_scope = AttributionScope.NONE
            severity_decay = 1.0
            if exact_severity:
                severity = exact_severity
                severity_sources = (target.node_id,)
                severity_scope = AttributionScope.EXACT_NODE
            else:
                same_path_severity = sorted(
                    (
                        (node_id, value)
                        for node_id, value in context.diagnostic_severity.items()
                        if value
                        and by_id.get(node_id) is not None
                        and by_id[node_id].path == target.path
                    ),
                    key=lambda item: (-item[1], item[0]),
                )
                if same_path_severity:
                    severity = same_path_severity[0][1] * _PATH_FALLBACK_DECAY
                    severity_sources = (same_path_severity[0][0],)
                    severity_scope = AttributionScope.PATH_FALLBACK
                    severity_decay = _PATH_FALLBACK_DECAY
                else:
                    severity = 0.0
            attempts = context.total_attempts.get(target.node_id, 0)
            accepted = context.accepted_attempts.get(target.node_id, 0)
            historical_yield = accepted / attempts if attempts else 0.5
            exploration = 1.0 / (1.0 + context.exploration_count.get(target.node_id, 0))
            risk = fan_out[target.node_id] / max_fan
            regression = max(0.0, context.regression_loss.get(target.node_id, 0.0))
            relevance_values = {
                "failure_coverage": coverage,
                "inverse_distance": inverse_distance,
                "dynamic_access": access,
                "diagnostic_severity": severity,
                "historical_yield": historical_yield,
            }
            exploration_values = {
                "exploration_bonus": exploration,
            }
            risk_values = {
                "fan_out_risk": risk,
                "historical_regression": regression,
            }
            attribution = {
                "failure_coverage": (
                    coverage_scope,
                    tuple(sorted(coverage_sources)),
                    _PATH_FALLBACK_DECAY if coverage_fallback else 1.0,
                ),
                "inverse_distance": (distance_scope, distance_sources, 1.0),
                "dynamic_access": (access_scope, access_sources, access_decay),
                "diagnostic_severity": (
                    severity_scope,
                    severity_sources,
                    severity_decay,
                ),
            }
            contributions = tuple(
                FeatureContribution(
                    feature=name,
                    raw_value=round(value, 8),
                    weight=float(getattr(self.weights, name)),
                    contribution=round(value * float(getattr(self.weights, name)), 8),
                    group=group,
                    attribution_scope=attribution.get(
                        name,
                        (AttributionScope.NONE, (), 1.0),
                    )[0],
                    source_node_ids=attribution.get(
                        name,
                        (AttributionScope.NONE, (), 1.0),
                    )[1],
                    fallback_decay=attribution.get(
                        name,
                        (AttributionScope.NONE, (), 1.0),
                    )[2],
                )
                for group, values in (
                    (FeatureGroup.RELEVANCE, relevance_values),
                    (FeatureGroup.EXPLORATION, exploration_values),
                    (FeatureGroup.RISK, risk_values),
                )
                for name, value in values.items()
            )
            relevance_score = sum(
                item.contribution for item in contributions if item.group is FeatureGroup.RELEVANCE
            )
            exploration_score = sum(
                item.contribution
                for item in contributions
                if item.group is FeatureGroup.EXPLORATION
            )
            risk_score = sum(
                item.contribution for item in contributions if item.group is FeatureGroup.RISK
            )
            risk_penalty = min(
                self.weights.max_ranking_risk_penalty,
                risk_score * self.weights.risk_ranking_fraction,
            )
            total = relevance_score + exploration_score - risk_penalty
            executable = target.path.endswith((".py", ".sh", ".bash", ".zsh"))
            if risk >= 0.65 or regression > 0:
                validation = ValidationIntensity(
                    level=ValidationLevel.FULL,
                    reasons=tuple(
                        reason
                        for condition, reason in (
                            (risk >= 0.65, "high_fan_out"),
                            (regression > 0, "historical_regression"),
                            (executable, "executable_component"),
                        )
                        if condition
                    ),
                    required_checks=(
                        "targeted_static",
                        "package_reparse",
                        "dependency_closure",
                        "security",
                        "full_split_validation",
                    ),
                )
            elif executable or risk >= 0.35:
                validation = ValidationIntensity(
                    level=ValidationLevel.ELEVATED,
                    reasons=("executable_component",) if executable else ("fan_out",),
                    required_checks=(
                        "targeted_static",
                        "package_reparse",
                        "dependency_closure",
                        "security",
                    ),
                )
            else:
                validation = ValidationIntensity()
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
                eligible=True,
                eligibility_reasons=("mutable_and_typed", "risk_is_not_eligibility"),
                score_breakdown=SelectionScoreBreakdown(
                    relevance=round(relevance_score, 8),
                    exploration=round(exploration_score, 8),
                    risk=round(risk_score, 8),
                    capped_ranking_risk_penalty=round(risk_penalty, 8),
                ),
                validation_intensity=validation,
            )
            scored.append((total, target.path, target.node_id, row))
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        all_ranked = tuple(
            item[3].model_copy(update={"rank": index}) for index, item in enumerate(scored, 1)
        )
        chosen = all_ranked[: min(limit, len(all_ranked))]
        alternatives = list(all_ranked[len(chosen) : len(chosen) + 5])
        executable_alternative = next(
            (
                item
                for item in all_ranked[len(chosen) :]
                if item.path.endswith((".py", ".sh", ".bash", ".zsh"))
            ),
            None,
        )
        if executable_alternative is not None and executable_alternative.node_id not in {
            item.node_id for item in alternatives
        }:
            alternatives.append(executable_alternative)
        return _result(
            self.kind,
            context,
            limit,
            chosen,
            alternatives=tuple(alternatives),
        )


class LegacyGraphGuidedComponentSelector(ComponentSelector):
    """Exact pre-GH-P0 scoring policy, retained as a replay control only."""

    kind = SelectorKind.GRAPH_GUIDED

    def __init__(self, weights: LegacyGraphSelectorWeights | None = None) -> None:
        self.weights = weights or LegacyGraphSelectorWeights()

    def select(self, context: SelectionContext, *, limit: int) -> SelectionResult:
        if limit < 1:
            raise ValueError("selection limit must be positive")
        by_id = {node.node_id: node for node in context.graph.nodes}
        distances = _distances(context)
        fan_out: Counter[str] = Counter()
        dynamic: Counter[str] = Counter()
        for edge in context.graph.edges:
            if edge.layer == "semantic_hypothesis":
                continue
            fan_out[edge.source] += 1
            if edge.layer in {"planned", "observed"}:
                dynamic[edge.source] += edge.count
                dynamic[edge.target] += edge.count
        max_fan = max(fan_out.values(), default=1)
        max_dynamic = max(dynamic.values(), default=1)
        slice_paths = [
            {by_id[item.node_id].path for item in failure_slice.nodes if item.node_id in by_id}
            for failure_slice in context.failure_slices
        ]
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
            values = {
                "failure_coverage": coverage,
                "inverse_distance": 1.0 / (1.0 + distance) if distance < 100 else 0.0,
                "dynamic_access": dynamic[target.node_id] / max_dynamic,
                "diagnostic_severity": max(
                    context.diagnostic_severity.get(target.node_id, 0.0),
                    max(
                        (
                            context.diagnostic_severity.get(node_id, 0.0)
                            for node_id, node in by_id.items()
                            if node.path == target.path
                        ),
                        default=0.0,
                    ),
                ),
                "historical_yield": (
                    context.accepted_attempts.get(target.node_id, 0)
                    / context.total_attempts[target.node_id]
                    if context.total_attempts.get(target.node_id, 0)
                    else 0.5
                ),
                "exploration_bonus": 1.0 / (1.0 + context.exploration_count.get(target.node_id, 0)),
                "fan_out_risk": fan_out[target.node_id] / max_fan,
                "historical_regression": max(0.0, context.regression_loss.get(target.node_id, 0.0)),
            }
            contributions = tuple(
                FeatureContribution(
                    feature=name,
                    raw_value=round(value, 8),
                    weight=float(getattr(self.weights, name)),
                    contribution=round(value * float(getattr(self.weights, name)), 8),
                    group=(
                        FeatureGroup.RISK
                        if "risk" in name or "regression" in name
                        else FeatureGroup.RELEVANCE
                    ),
                )
                for name, value in values.items()
            )
            total = sum(item.contribution for item in contributions)
            scored.append(
                (
                    total,
                    target.path,
                    target.node_id,
                    RankedSelection(
                        rank=1,
                        node_id=target.node_id,
                        path=target.path,
                        locator=target.locator,
                        score=round(total, 8),
                        contributions=contributions,
                        evidence_refs=evidence,
                        reason_code="legacy_graph_failure_impact_priority",
                        high_blast_radius=values["fan_out_risk"] >= 0.65,
                    ),
                )
            )
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        chosen = tuple(
            item[3].model_copy(update={"rank": rank})
            for rank, item in enumerate(scored[: min(limit, len(scored))], 1)
        )
        return _result(self.kind, context, limit, chosen)
