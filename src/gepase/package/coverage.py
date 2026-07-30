"""Explicit PackageGraph coverage and parser-status audit."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from pydantic import Field

from gepase.package.ir import NodeKind, PackageGraph, PackageSnapshot, ParseStatus
from gepase.schemas.common import FrozenModel


class FileCoverage(FrozenModel):
    path: str
    file_kind: str
    parse_status: ParseStatus
    parse_status_explicit: bool
    parser: str
    parse_detail: str | None = None
    file_node_id: str
    internal_node_count: int = Field(ge=0)
    incoming_edge_count: int = Field(ge=0)
    outgoing_edge_count: int = Field(ge=0)
    isolated_mutable: bool
    mutation_capabilities: tuple[str, ...]


class GraphCoverageAudit(FrozenModel):
    schema_version: str = "1.0.0"
    package_id: str
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_file_count: int = Field(ge=0)
    file_node_count: int = Field(ge=0)
    file_node_coverage: float = Field(ge=0, le=1)
    files: tuple[FileCoverage, ...]
    parse_status_counts: dict[str, int]
    node_kind_counts: dict[str, int]
    edge_kind_counts: dict[str, int]
    layer_counts: dict[str, int]
    isolated_mutable_components: tuple[str, ...]
    unresolved_symbols: tuple[str, ...]
    ambiguous_symbols: tuple[str, ...]
    error_paths: tuple[str, ...]


def _capabilities(path: str, mutable: bool, binary: bool) -> tuple[str, ...]:
    if not mutable or binary:
        return ("inspect_only",)
    suffix = Path(path).suffix.lower()
    if suffix in {".md", ".markdown"}:
        return ("replace_markdown_block", "replace_text_file")
    if suffix == ".py":
        return ("replace_python_function", "replace_text_file")
    if suffix in {".sh", ".bash", ".zsh", ".yaml", ".yml", ".json", ".toml"}:
        return ("replace_text_file",)
    return ("inspect_only",)


def audit_graph_coverage(
    snapshot: PackageSnapshot,
    graph: PackageGraph,
) -> GraphCoverageAudit:
    file_nodes = {
        node.path: node for node in graph.nodes if node.kind is NodeKind.FILE
    }
    incoming: Counter[str] = Counter(edge.target for edge in graph.edges)
    outgoing: Counter[str] = Counter(edge.source for edge in graph.edges)
    internal: defaultdict[str, int] = defaultdict(int)
    for node in graph.nodes:
        if node.kind not in {NodeKind.PACKAGE, NodeKind.FILE} and node.path in file_nodes:
            internal[node.path] += 1
    rows: list[FileCoverage] = []
    isolated: list[str] = []
    errors: list[str] = []
    for item in snapshot.files:
        node = file_nodes.get(item.path)
        if node is None:
            continue
        raw_status = str(node.metadata.get("parse_status", ParseStatus.SHALLOW.value))
        status = ParseStatus(raw_status)
        # Package->file containment is not evidence of component connectivity.
        non_manifest_incident = sum(
            edge.source == node.node_id or edge.target == node.node_id
            for edge in graph.edges
            if not (
                edge.kind.value == "contains"
                and edge.source != node.node_id
                and edge.target == node.node_id
            )
        )
        is_isolated = item.mutable and non_manifest_incident == 0
        if is_isolated:
            isolated.append(item.path)
        if status is ParseStatus.ERROR:
            errors.append(item.path)
        rows.append(
            FileCoverage(
                path=item.path,
                file_kind=item.kind.value,
                parse_status=status,
                parse_status_explicit="parse_status" in node.metadata,
                parser=str(node.metadata.get("parser", "unknown")),
                parse_detail=(
                    str(node.metadata["parse_detail"])
                    if node.metadata.get("parse_detail") is not None
                    else None
                ),
                file_node_id=node.node_id,
                internal_node_count=internal[item.path],
                incoming_edge_count=incoming[node.node_id],
                outgoing_edge_count=outgoing[node.node_id],
                isolated_mutable=is_isolated,
                mutation_capabilities=_capabilities(item.path, item.mutable, item.binary),
            )
        )
    unresolved = sorted(
        node.label
        for node in graph.nodes
        if node.kind is NodeKind.UNKNOWN
        and "ambiguous" not in str(node.metadata.get("reason", "")).casefold()
    )
    ambiguous = sorted(
        node.label
        for node in graph.nodes
        if node.kind is NodeKind.UNKNOWN
        and "ambiguous" in str(node.metadata.get("reason", "")).casefold()
    )
    status_counts = Counter(row.parse_status.value for row in rows)
    return GraphCoverageAudit(
        package_id=snapshot.package_id,
        snapshot_hash=snapshot.snapshot_hash,
        snapshot_file_count=len(snapshot.files),
        file_node_count=len(file_nodes),
        file_node_coverage=(len(file_nodes) / len(snapshot.files) if snapshot.files else 1.0),
        files=tuple(rows),
        parse_status_counts=dict(sorted(status_counts.items())),
        node_kind_counts=dict(
            sorted(Counter(node.kind.value for node in graph.nodes).items())
        ),
        edge_kind_counts=dict(
            sorted(Counter(edge.kind.value for edge in graph.edges).items())
        ),
        layer_counts=dict(sorted(Counter(edge.layer for edge in graph.edges).items())),
        isolated_mutable_components=tuple(isolated),
        unresolved_symbols=tuple(unresolved),
        ambiguous_symbols=tuple(ambiguous),
        error_paths=tuple(errors),
    )
