"""Explainable graph-guided mutation target ranking."""

from __future__ import annotations

from collections import Counter, defaultdict, deque

from pydantic import Field

from gepase.optimizer.selectors import (
    ComponentSelector,
    FeatureContribution,
    FeatureGroup,
    RankedSelection,
    SelectionContext,
    SelectionResult,
    SelectionScoreBreakdown,
    SelectorKind,
    ValidationIntensity,
    ValidationLevel,
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
    semantic_hypothesis_support: float = Field(default=0.35, ge=0, le=0.5)
    max_semantic_contribution: float = Field(default=0.35, ge=0, le=0.5)
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


def _distances(
    context: SelectionContext,
    *,
    include_semantic_hypotheses: bool = False,
) -> dict[str, int]:
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
    semantic_traversable = {
        EdgeKind.IMPLEMENTS,
        EdgeKind.EXPLAINS,
        EdgeKind.CONSTRAINS,
        EdgeKind.CONSUMES,
        EdgeKind.PRODUCES,
        EdgeKind.VALIDATES,
        EdgeKind.CONFLICTS_WITH,
    }
    for edge in context.graph.edges:
        trusted = edge.layer in {"static", "planned", "observed"} and edge.kind in traversable
        semantic = (
            include_semantic_hypotheses
            and edge.layer == "semantic_hypothesis"
            and edge.kind in semantic_traversable
            and "selector_top_k" in set(edge.metadata.get("allowed_consumers", []))
        )
        if trusted or semantic:
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
        # Keep structural distance trusted-only.  Semantic hypotheses have one
        # separately capped feature and must not amplify another relevance term.
        distances = _distances(context)
        fan_out: Counter[str] = Counter()
        dynamic: Counter[str] = Counter()
        semantic: defaultdict[str, float] = defaultdict(float)
        for edge in context.graph.edges:
            if edge.layer != "semantic_hypothesis":
                fan_out[edge.source] += 1
            if edge.layer == "observed":
                dynamic[edge.source] += edge.count
                dynamic[edge.target] += edge.count
            elif edge.layer == "semantic_hypothesis" and "selector_top_k" in set(
                edge.metadata.get("allowed_consumers", [])
            ):
                semantic[edge.source] += edge.confidence
                semantic[edge.target] += edge.confidence
        max_fan = max(fan_out.values(), default=1)
        max_dynamic = max(dynamic.values(), default=1)
        max_semantic = max(semantic.values(), default=1.0)
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
            path_access = sum(
                value
                for node_id, value in dynamic.items()
                if by_id.get(node_id) is not None and by_id[node_id].path == target.path
            )
            access = path_access / max_dynamic
            # Unlike observed file access, an Analyzer hypothesis is anchored
            # to exact nodes.  Do not fan its score across every mutable node
            # that happens to share the same file path.
            semantic_support = semantic[target.node_id] / max_semantic
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
            relevance_values = {
                "failure_coverage": coverage,
                "inverse_distance": inverse_distance,
                "dynamic_access": access,
                "diagnostic_severity": severity,
                "historical_yield": historical_yield,
                "semantic_hypothesis_support": semantic_support,
            }
            exploration_values = {
                "exploration_bonus": exploration,
            }
            risk_values = {
                "fan_out_risk": risk,
                "historical_regression": regression,
            }
            contributions = tuple(
                FeatureContribution(
                    feature=name,
                    raw_value=round(value, 8),
                    weight=float(getattr(self.weights, name)),
                    contribution=round(
                        min(
                            value * float(getattr(self.weights, name)),
                            self.weights.max_semantic_contribution,
                        )
                        if name == "semantic_hypothesis_support"
                        else value * float(getattr(self.weights, name)),
                        8,
                    ),
                    group=group,
                )
                for group, values in (
                    (FeatureGroup.RELEVANCE, relevance_values),
                    (FeatureGroup.EXPLORATION, exploration_values),
                    (FeatureGroup.RISK, risk_values),
                )
                for name, value in values.items()
            )
            relevance_score = sum(
                item.contribution
                for item in contributions
                if item.group is FeatureGroup.RELEVANCE
            )
            exploration_score = sum(
                item.contribution
                for item in contributions
                if item.group is FeatureGroup.EXPLORATION
            )
            risk_score = sum(
                item.contribution
                for item in contributions
                if item.group is FeatureGroup.RISK
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
            item[3].model_copy(update={"rank": index})
            for index, item in enumerate(scored, 1)
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
        if (
            executable_alternative is not None
            and executable_alternative.node_id not in {item.node_id for item in alternatives}
        ):
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
                "exploration_bonus": 1.0
                / (1.0 + context.exploration_count.get(target.node_id, 0)),
                "fan_out_risk": fan_out[target.node_id] / max_fan,
                "historical_regression": max(
                    0.0, context.regression_loss.get(target.node_id, 0.0)
                ),
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
