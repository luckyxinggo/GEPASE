"""Budgeted union of failure slices for mutation context construction."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from pydantic import Field, model_validator

from gepase.package.ir import FailureSlice, PackageGraph
from gepase.schemas.common import FrozenModel


@dataclass
class _Accumulator:
    weighted_score: float = 0.0
    failures: set[str] = field(default_factory=set)
    distances: list[int] = field(default_factory=list)
    causes: set[str] = field(default_factory=set)
    refs: set[str] = field(default_factory=set)


class FailureSliceInput(FrozenModel):
    failure_id: str
    root_cause: str
    weight: float = Field(default=1.0, gt=0)
    evidence_ref: str
    failure_slice: FailureSlice


class UnionFailureNode(FrozenModel):
    node_id: str
    path: str
    score: float = Field(ge=0)
    coverage: float = Field(ge=0, le=1)
    min_distance: int = Field(ge=0)
    root_causes: tuple[str, ...]
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    token_estimate: int = Field(ge=1)
    failure_seed: bool = False
    high_blast_radius: bool = False


class UnionFailureSlice(FrozenModel):
    schema_version: str = "1.0.0"
    package_id: str
    nodes: tuple[UnionFailureNode, ...] = Field(min_length=1)
    failure_seed_ids: tuple[str, ...] = Field(min_length=1)
    root_cause_clusters: dict[str, tuple[str, ...]]
    omitted_nodes: int = Field(ge=0)
    token_estimate: int = Field(ge=1)
    max_nodes: int = Field(ge=1)
    max_tokens: int = Field(ge=1)

    @model_validator(mode="after")
    def seeds_preserved(self) -> UnionFailureSlice:
        selected = {item.node_id for item in self.nodes}
        if not selected & set(self.failure_seed_ids):
            raise ValueError("failure union pruning removed every failure seed")
        if len(self.nodes) > self.max_nodes or self.token_estimate > self.max_tokens:
            raise ValueError("failure union exceeded declared budget")
        return self


def build_failure_union(
    graph: PackageGraph,
    inputs: tuple[FailureSliceInput, ...],
    *,
    max_nodes: int,
    max_tokens: int,
) -> UnionFailureSlice:
    if not inputs:
        raise ValueError("failure union requires at least one slice")
    if max_nodes < 1 or max_tokens < 1:
        raise ValueError("failure union budgets must be positive")
    by_id = {node.node_id: node for node in graph.nodes}
    fan_out: defaultdict[str, int] = defaultdict(int)
    for edge in graph.edges:
        fan_out[edge.source] += 1
    fan_threshold = max(4, int(max(fan_out.values(), default=0) * 0.65))
    rows: dict[str, _Accumulator] = {}
    clusters: defaultdict[str, set[str]] = defaultdict(set)
    seeds = tuple(sorted({seed for item in inputs for seed in item.failure_slice.seed_node_ids}))
    for item in inputs:
        for node in item.failure_slice.nodes:
            if node.node_id not in by_id:
                continue
            clusters[item.root_cause].add(node.node_id)
            row = rows.setdefault(node.node_id, _Accumulator())
            row.weighted_score += node.score * item.weight
            row.failures.add(item.failure_id)
            row.distances.append(node.distance)
            row.causes.add(item.root_cause)
            row.refs.add(item.evidence_ref)
    prepared: list[UnionFailureNode] = []
    for node_id, row in rows.items():
        node = by_id[node_id]
        node_tokens = max(1, (len(node.label) + len(str(node.metadata))) // 4)
        prepared.append(
            UnionFailureNode(
                node_id=node_id,
                path=node.path,
                score=round(row.weighted_score, 8),
                coverage=len(row.failures) / len(inputs),
                min_distance=min(row.distances),
                root_causes=tuple(sorted(row.causes)),
                evidence_refs=tuple(sorted(row.refs)),
                token_estimate=node_tokens,
                failure_seed=node_id in seeds,
                high_blast_radius=fan_out[node_id] >= fan_threshold,
            )
        )
    prepared.sort(
        key=lambda item: (
            not item.failure_seed,
            -item.score,
            item.min_distance,
            item.node_id,
        )
    )
    selected: list[UnionFailureNode] = []
    used_tokens = 0
    for item in prepared:
        if len(selected) >= max_nodes or used_tokens + item.token_estimate > max_tokens:
            continue
        selected.append(item)
        used_tokens += item.token_estimate
    if not any(item.failure_seed for item in selected):
        seed = next((item for item in prepared if item.failure_seed), None)
        if seed is None or seed.token_estimate > max_tokens:
            raise ValueError("failure seed cannot fit the union budget")
        selected = [seed]
        used_tokens = seed.token_estimate
    return UnionFailureSlice(
        package_id=graph.package_id,
        nodes=tuple(selected),
        failure_seed_ids=seeds,
        root_cause_clusters={key: tuple(sorted(value)) for key, value in sorted(clusters.items())},
        omitted_nodes=len(prepared) - len(selected),
        token_estimate=used_tokens,
        max_nodes=max_nodes,
        max_tokens=max_tokens,
    )
