"""Compile parser facts into a closed, typed heterogeneous PackageGraph."""

from __future__ import annotations

from pathlib import Path

from gepase.package.binary_manifest import parse_binary
from gepase.package.capability_manifest import capability_rows
from gepase.package.diagnostics import diagnose
from gepase.package.ir import (
    EdgeKind,
    FileKind,
    GraphEdge,
    IRNode,
    NodeKind,
    PackageGraph,
    PackageIR,
    PackageSnapshot,
    make_edge,
    make_node,
)
from gepase.package.markdown_ir import parse_markdown
from gepase.package.parsing import ParsedFile, RelationFact
from gepase.package.python_ir import parse_python
from gepase.package.requirements_ir import parse_requirements
from gepase.package.shell_ir import parse_shell


def compile_graph(package_root: Path, snapshot: PackageSnapshot) -> tuple[PackageIR, PackageGraph]:
    package_id = snapshot.package_id
    package_node = make_node(
        package_id,
        NodeKind.PACKAGE,
        ".",
        "package",
        package_id,
        snapshot.snapshot_hash,
        mutable=False,
        metadata={"root_name": snapshot.root_name},
    )
    nodes: list[IRNode] = [package_node]
    edges: list[GraphEdge] = []
    file_nodes: dict[str, IRNode] = {}
    parsed_files: list[ParsedFile] = []
    for item in snapshot.files:
        file_node = make_node(
            package_id,
            NodeKind.FILE,
            item.path,
            "file",
            item.path,
            item.sha256,
            mutable=item.mutable,
            metadata={
                "file_kind": item.kind.value,
                "size_bytes": item.size_bytes,
                "binary": item.binary,
                "classification_reason": item.reason,
                "source_sha256": item.sha256,
            },
        )
        file_nodes[item.path] = file_node
        nodes.append(file_node)
        edges.append(make_edge(package_node.node_id, file_node.node_id, EdgeKind.CONTAINS))
        parsed = _parse_file(package_root, snapshot, item.path, item.kind, item.binary, file_node)
        nodes.extend(parsed.nodes)
        parsed_files.append(parsed)

    by_path_locator = {(node.path, node.locator): node for node in nodes}
    by_symbol: dict[str, IRNode] = {}
    for node in nodes:
        if node.kind in {NodeKind.FUNCTION, NodeKind.CLASS}:
            by_symbol.setdefault(node.label, node)
    for parsed in parsed_files:
        for relation in parsed.relations:
            edge, extra_node = _resolve_relation(
                package_id,
                relation,
                {node.node_id: node for node in nodes},
                file_nodes,
                by_path_locator,
                by_symbol,
            )
            if extra_node is not None and extra_node.node_id not in {
                node.node_id for node in nodes
            }:
                nodes.append(extra_node)
            edges.append(edge)

    for relation, value in capability_rows(snapshot.capabilities):
        kind = EdgeKind(relation)
        target = _external_node(package_id, value, f"capability:{relation}")
        if target.node_id not in {node.node_id for node in nodes}:
            nodes.append(target)
        edges.append(make_edge(package_node.node_id, target.node_id, kind, identity=value))

    dedup_nodes = {node.node_id: node for node in nodes}
    dedup_edges = {edge.edge_id: edge for edge in edges}
    static_nodes = tuple(sorted(dedup_nodes.values(), key=lambda item: item.node_id))
    static_edges = tuple(sorted(dedup_edges.values(), key=lambda item: item.edge_id))
    diagnostics = diagnose(package_id, static_nodes, static_edges)
    ir = PackageIR(
        package_id=package_id,
        snapshot_hash=snapshot.snapshot_hash,
        nodes=static_nodes,
        edges=static_edges,
    )
    graph = PackageGraph(
        package_id=package_id,
        snapshot_hash=snapshot.snapshot_hash,
        nodes=static_nodes,
        edges=static_edges,
        diagnostics=diagnostics,
    )
    return ir, graph


def _parse_file(
    package_root: Path,
    snapshot: PackageSnapshot,
    path: str,
    kind: FileKind,
    binary: bool,
    file_node: IRNode,
) -> ParsedFile:
    if binary:
        return parse_binary(snapshot.package_id, package_root, path, file_node)
    suffix = Path(path).suffix.lower()
    if suffix in {".md", ".markdown"}:
        return parse_markdown(snapshot.package_id, package_root, path, file_node)
    if suffix == ".py":
        return parse_python(snapshot.package_id, package_root, path, file_node)
    if Path(path).name in {"requirements.txt", "requirements.lock"}:
        return parse_requirements(snapshot.package_id, package_root, path, file_node)
    if suffix in {".sh", ".bash", ".zsh"}:
        return parse_shell(snapshot.package_id, package_root, path, file_node)
    return ParsedFile((), ())


def _resolve_relation(
    package_id: str,
    fact: RelationFact,
    nodes_by_id: dict[str, IRNode],
    file_nodes: dict[str, IRNode],
    by_path_locator: dict[tuple[str, str], IRNode],
    by_symbol: dict[str, IRNode],
) -> tuple[GraphEdge, IRNode | None]:
    source = nodes_by_id[fact.source]
    target: IRNode | None = None
    extra: IRNode | None = None
    if fact.target_path:
        target = file_nodes.get(fact.target_path)
        if target is None:
            extra = _unknown_node(
                package_id,
                fact.target_path,
                "missing package-relative path",
            )
            target = extra
    elif fact.target_locator:
        if fact.target_locator.startswith("symbol/"):
            symbol = fact.target_locator.removeprefix("symbol/").rsplit(".", 1)[-1]
            target = by_symbol.get(symbol)
            if target is None:
                extra = _external_node(package_id, symbol, "unresolved symbol")
                target = extra
        else:
            target = by_path_locator.get((source.path, fact.target_locator))
            if target is None:
                extra = _unknown_node(
                    package_id,
                    f"{source.path}#{fact.target_locator}",
                    fact.reason or "semantic locator not found",
                )
                target = extra
    elif fact.external_name:
        extra = (
            _unknown_node(package_id, fact.external_name, fact.reason)
            if fact.reason and "unsafe" in fact.reason
            else _external_node(package_id, fact.external_name, fact.reason or "external")
        )
        target = extra
    else:
        extra = _unknown_node(package_id, "unspecified", fact.reason or "target omitted")
        target = extra
    return (
        make_edge(
            fact.source,
            target.node_id,
            fact.kind,
            identity={
                "target_path": fact.target_path,
                "target_locator": fact.target_locator,
                "external": fact.external_name,
            },
            metadata={**fact.metadata, "resolution_reason": fact.reason},
        ),
        extra,
    )


def _external_node(package_id: str, name: str, reason: str) -> IRNode:
    return make_node(
        package_id,
        NodeKind.EXTERNAL,
        "<external>",
        f"external/{name}",
        name,
        name,
        mutable=False,
        metadata={"reason": reason},
    )


def _unknown_node(package_id: str, name: str, reason: str | None) -> IRNode:
    return make_node(
        package_id,
        NodeKind.UNKNOWN,
        "<unknown>",
        f"unknown/{name}",
        name,
        name,
        mutable=False,
        metadata={"reason": reason or "unresolved target"},
    )


def validate_graphs(paths: list[Path]) -> dict[str, object]:
    import json

    schema_errors = 0
    dangling = 0
    duplicates = 0
    checked = 0
    errors: list[dict[str, str]] = []
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            node_ids = [node["node_id"] for node in raw.get("nodes", [])]
            known = set(node_ids)
            duplicate_count = len(node_ids) - len(known)
            dangling_count = sum(
                edge.get("source") not in known or edge.get("target") not in known
                for edge in raw.get("edges", [])
            )
            PackageGraph.model_validate(raw)
            checked += 1
            duplicates += duplicate_count
            dangling += dangling_count
        except (OSError, ValueError, KeyError) as error:
            schema_errors += 1
            errors.append({"path": path.as_posix(), "error_type": type(error).__name__})
    return {
        "valid": schema_errors == dangling == duplicates == 0 and checked == len(paths),
        "checked": checked,
        "schema_errors": schema_errors,
        "dangling_node_ref": dangling,
        "duplicate_node_id": duplicates,
        "errors": errors,
    }
