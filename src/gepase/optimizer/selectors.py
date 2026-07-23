"""Comparable component selectors for bounded package mutation."""

from __future__ import annotations

import hashlib
import random
from abc import ABC, abstractmethod
from enum import StrEnum

from pydantic import Field, model_validator

from gepase.package.ir import FailureSlice, PackageGraph
from gepase.schemas.common import FrozenModel


class SelectorKind(StrEnum):
    RANDOM = "random"
    ROUND_ROBIN = "round_robin"
    TRACE_ONLY = "trace_only"
    GRAPH_GUIDED = "graph_guided"


class SelectionTarget(FrozenModel):
    node_id: str
    path: str
    locator: str
    node_kind: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    token_estimate: int = Field(ge=1)
    mutable: bool = True


class SelectionContext(FrozenModel):
    graph: PackageGraph
    targets: tuple[SelectionTarget, ...]
    failure_slices: tuple[FailureSlice, ...]
    evidence_refs: tuple[str, ...] = ()
    diagnostic_severity: dict[str, float] = Field(default_factory=dict)
    accepted_attempts: dict[str, int] = Field(default_factory=dict)
    total_attempts: dict[str, int] = Field(default_factory=dict)
    regression_loss: dict[str, float] = Field(default_factory=dict)
    exploration_count: dict[str, int] = Field(default_factory=dict)
    seed: int = 42
    iteration: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def context_invariants(self) -> SelectionContext:
        graph_ids = {node.node_id for node in self.graph.nodes}
        unknown = {target.node_id for target in self.targets} - graph_ids
        if unknown:
            raise ValueError(f"selection targets absent from graph: {sorted(unknown)}")
        if not self.targets:
            raise ValueError("selection context requires at least one target")
        if any(not item.mutable for item in self.targets):
            raise ValueError("selection context must contain mutable targets only")
        return self


class FeatureContribution(FrozenModel):
    feature: str
    raw_value: float
    weight: float
    contribution: float


class RankedSelection(FrozenModel):
    rank: int = Field(ge=1)
    node_id: str
    path: str
    locator: str
    score: float
    contributions: tuple[FeatureContribution, ...]
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    reason_code: str
    high_blast_radius: bool = False


class SelectionResult(FrozenModel):
    schema_version: str = "1.0.0"
    selector: SelectorKind
    seed: int
    iteration: int
    requested_limit: int = Field(ge=1)
    selected: tuple[RankedSelection, ...] = Field(min_length=1)
    eligible_nodes: int = Field(ge=1)
    deterministic_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


def _fingerprint(kind: SelectorKind, context: SelectionContext, ids: list[str]) -> str:
    payload = "|".join((kind.value, str(context.seed), str(context.iteration), *ids))
    return hashlib.sha256(payload.encode()).hexdigest()


def _evidence(context: SelectionContext) -> tuple[str, ...]:
    if context.evidence_refs:
        return tuple(sorted(set(context.evidence_refs)))
    seeds = sorted({seed for item in context.failure_slices for seed in item.seed_node_ids})
    return tuple(f"graph-node:{seed}" for seed in seeds) or ("graph:package",)


class ComponentSelector(ABC):
    kind: SelectorKind

    @abstractmethod
    def select(self, context: SelectionContext, *, limit: int) -> SelectionResult: ...


class RandomComponentSelector(ComponentSelector):
    kind = SelectorKind.RANDOM

    def select(self, context: SelectionContext, *, limit: int) -> SelectionResult:
        if limit < 1:
            raise ValueError("selection limit must be positive")
        targets = sorted(context.targets, key=lambda item: item.node_id)
        randomizer = random.Random(context.seed + context.iteration)
        randomizer.shuffle(targets)
        chosen = targets[: min(limit, len(targets))]
        evidence = _evidence(context)
        rows = tuple(
            RankedSelection(
                rank=index,
                node_id=item.node_id,
                path=item.path,
                locator=item.locator,
                score=1.0,
                contributions=(
                    FeatureContribution(
                        feature="seeded_random", raw_value=1.0, weight=1.0, contribution=1.0
                    ),
                ),
                evidence_refs=evidence,
                reason_code="seeded_random_control",
            )
            for index, item in enumerate(chosen, 1)
        )
        return _result(self.kind, context, limit, rows)


class RoundRobinComponentSelector(ComponentSelector):
    kind = SelectorKind.ROUND_ROBIN

    def select(self, context: SelectionContext, *, limit: int) -> SelectionResult:
        if limit < 1:
            raise ValueError("selection limit must be positive")
        targets = sorted(context.targets, key=lambda item: (item.path, item.locator, item.node_id))
        count = min(limit, len(targets))
        start = context.iteration % len(targets)
        chosen = [targets[(start + offset) % len(targets)] for offset in range(count)]
        evidence = _evidence(context)
        rows = tuple(
            RankedSelection(
                rank=index,
                node_id=item.node_id,
                path=item.path,
                locator=item.locator,
                score=1.0 - ((index - 1) / max(1, len(targets))),
                contributions=(
                    FeatureContribution(
                        feature="round_robin_position",
                        raw_value=float(index),
                        weight=1.0,
                        contribution=1.0 / index,
                    ),
                ),
                evidence_refs=evidence,
                reason_code="deterministic_round_robin_control",
            )
            for index, item in enumerate(chosen, 1)
        )
        return _result(self.kind, context, limit, rows)


class TraceOnlyComponentSelector(ComponentSelector):
    kind = SelectorKind.TRACE_ONLY

    def select(self, context: SelectionContext, *, limit: int) -> SelectionResult:
        if limit < 1:
            raise ValueError("selection limit must be positive")
        access: dict[str, float] = {target.node_id: 0.0 for target in context.targets}
        by_id = {node.node_id: node for node in context.graph.nodes}
        for edge in context.graph.edges:
            if edge.layer not in {"planned", "observed"}:
                continue
            multiplier = 2.0 if edge.layer == "observed" else 1.0
            for node_id in (edge.source, edge.target):
                node = by_id.get(node_id)
                if node is None:
                    continue
                for target in context.targets:
                    if target.node_id == node_id or target.path == node.path:
                        access[target.node_id] += multiplier * edge.count * edge.confidence
        ranked = sorted(
            context.targets,
            key=lambda item: (-access[item.node_id], item.path, item.locator, item.node_id),
        )
        evidence = _evidence(context)
        chosen = ranked[: min(limit, len(ranked))]
        rows = tuple(
            RankedSelection(
                rank=index,
                node_id=item.node_id,
                path=item.path,
                locator=item.locator,
                score=access[item.node_id],
                contributions=(
                    FeatureContribution(
                        feature="dynamic_access_frequency",
                        raw_value=access[item.node_id],
                        weight=1.0,
                        contribution=access[item.node_id],
                    ),
                ),
                evidence_refs=evidence,
                reason_code=("trace_access" if access[item.node_id] else "trace_control_fallback"),
            )
            for index, item in enumerate(chosen, 1)
        )
        return _result(self.kind, context, limit, rows)


def _result(
    kind: SelectorKind,
    context: SelectionContext,
    limit: int,
    rows: tuple[RankedSelection, ...],
) -> SelectionResult:
    ids = [item.node_id for item in rows]
    return SelectionResult(
        selector=kind,
        seed=context.seed,
        iteration=context.iteration,
        requested_limit=limit,
        selected=rows,
        eligible_nodes=len(context.targets),
        deterministic_fingerprint=_fingerprint(kind, context, ids),
    )


def selector_for(kind: SelectorKind) -> ComponentSelector:
    if kind is SelectorKind.RANDOM:
        return RandomComponentSelector()
    if kind is SelectorKind.ROUND_ROBIN:
        return RoundRobinComponentSelector()
    if kind is SelectorKind.TRACE_ONLY:
        return TraceOnlyComponentSelector()
    from gepase.optimizer.graph_selector import GraphGuidedComponentSelector

    return GraphGuidedComponentSelector()
