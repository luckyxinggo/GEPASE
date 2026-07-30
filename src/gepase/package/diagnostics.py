"""Deterministic structural diagnostics over a typed package graph."""

from __future__ import annotations

import sys
from collections import Counter, defaultdict

from gepase.package.ir import (
    Diagnostic,
    EdgeKind,
    GraphEdge,
    IRNode,
    NodeKind,
    stable_id,
)

_DEPENDENCY_EDGES = {
    EdgeKind.REFERENCES,
    EdgeKind.IMPORTS,
    EdgeKind.CALLS,
    EdgeKind.EXECUTES,
    EdgeKind.TESTS,
}


def diagnose(
    package_id: str,
    nodes: tuple[IRNode, ...],
    edges: tuple[GraphEdge, ...],
) -> tuple[Diagnostic, ...]:
    by_id = {node.node_id: node for node in nodes}
    declared_imports = {
        str(import_name).casefold()
        for node in nodes
        if node.kind is NodeKind.DEPENDENCY
        for import_name in node.metadata.get("import_names", [])
    }
    diagnostics: list[Diagnostic] = []
    incoming: defaultdict[str, list[GraphEdge]] = defaultdict(list)
    outgoing: defaultdict[str, list[GraphEdge]] = defaultdict(list)
    for edge in edges:
        incoming[edge.target].append(edge)
        outgoing[edge.source].append(edge)
        target = by_id[edge.target]
        if target.kind is NodeKind.UNKNOWN and edge.kind in {
            EdgeKind.REFERENCES,
            EdgeKind.IMPORTS,
            EdgeKind.EXECUTES,
            EdgeKind.TESTS,
        }:
            kind = (
                "unsafe_path"
                if "unsafe" in str(target.metadata.get("reason", ""))
                else "broken_reference"
            )
            diagnostics.append(
                _diagnostic(
                    package_id,
                    kind,
                    "error",
                    f"{edge.kind.value} target cannot be resolved: {target.label}",
                    (edge.source, edge.target),
                    by_id[edge.source].path,
                    {"edge_id": edge.edge_id, "target": target.label},
                )
            )

    referenced_file_ids = {
        edge.target
        for edge in edges
        if edge.kind not in {EdgeKind.CONTAINS} and by_id[edge.target].kind is NodeKind.FILE
    }
    for node in nodes:
        if node.kind is not NodeKind.FILE:
            continue
        file_kind = str(node.metadata.get("file_kind", ""))
        if file_kind in {"skill", "license", "metadata", "agent_config"}:
            continue
        if node.node_id not in referenced_file_ids:
            diagnostics.append(
                _diagnostic(
                    package_id,
                    "orphan_node",
                    "warning",
                    f"package file has no non-containment incoming dependency: {node.path}",
                    (node.node_id,),
                    node.path,
                )
            )

    for node in nodes:
        if node.kind is NodeKind.FRONTMATTER and node.metadata.get("parse_error"):
            diagnostics.append(
                _diagnostic(
                    package_id,
                    "invalid_frontmatter",
                    "error",
                    "frontmatter YAML could not be parsed",
                    (node.node_id,),
                    node.path,
                    {"error_type": str(node.metadata["parse_error"])},
                )
            )
        if node.kind is NodeKind.ERROR:
            error_type = str(node.metadata.get("error_type", "parse_error"))
            diagnostics.append(
                _diagnostic(
                    package_id,
                    error_type,
                    "error",
                    str(node.metadata.get("message", node.label)),
                    (node.node_id,),
                    node.path,
                )
            )
        if node.kind is NodeKind.ENTRYPOINT:
            noncontainment_incoming = [
                edge for edge in incoming[node.node_id] if edge.kind is not EdgeKind.CONTAINS
            ]
            file_parent = next(
                (
                    edge.source
                    for edge in incoming[node.node_id]
                    if by_id[edge.source].kind is NodeKind.FILE
                ),
                None,
            )
            if file_parent is None:
                module_parent = next(
                    (
                        edge.source
                        for edge in incoming[node.node_id]
                        if by_id[edge.source].kind is NodeKind.PYTHON_MODULE
                    ),
                    None,
                )
                if module_parent is not None:
                    file_parent = next(
                        (
                            edge.source
                            for edge in incoming[module_parent]
                            if by_id[edge.source].kind is NodeKind.FILE
                        ),
                        None,
                    )
            file_referenced = bool(
                file_parent
                and any(edge.kind is not EdgeKind.CONTAINS for edge in incoming[file_parent])
            )
            if not noncontainment_incoming and not file_referenced:
                diagnostics.append(
                    _diagnostic(
                        package_id,
                        "unreachable_entry",
                        "warning",
                        f"entrypoint is not referenced by package instructions: {node.path}",
                        (node.node_id,),
                        node.path,
                    )
                )
        if node.kind is NodeKind.IMPORT:
            for edge in outgoing[node.node_id]:
                target = by_id[edge.target]
                if edge.kind is EdgeKind.IMPORTS and target.kind is NodeKind.EXTERNAL:
                    top = target.label.lstrip(".").split(".", 1)[0]
                    if (
                        top
                        and top not in sys.stdlib_module_names
                        and top.casefold() not in declared_imports
                    ):
                        diagnostics.append(
                            _diagnostic(
                                package_id,
                                "undeclared_dependency",
                                "warning",
                                f"non-stdlib import requires an explicit runtime dependency: {top}",
                                (node.node_id, target.node_id),
                                node.path,
                                {"dependency": top},
                            )
                        )

    functions_by_path: defaultdict[str, list[IRNode]] = defaultdict(list)
    entrypoints_by_path: defaultdict[str, list[IRNode]] = defaultdict(list)
    for node in nodes:
        if node.kind is NodeKind.FUNCTION:
            functions_by_path[node.path].append(node)
        elif node.kind is NodeKind.ENTRYPOINT:
            entrypoints_by_path[node.path].append(node)
    for path, functions in sorted(functions_by_path.items()):
        main = next((node for node in functions if node.label == "main"), None)
        if main is not None and not entrypoints_by_path[path]:
            diagnostics.append(
                _diagnostic(
                    package_id,
                    "missing_entrypoint",
                    "error",
                    f"script defines main() but has no __main__ guard: {path}",
                    (main.node_id,),
                    path,
                )
            )

    instructions = [node for node in nodes if node.kind is NodeKind.INSTRUCTION]
    counts = Counter(node.content_hash for node in instructions)
    for digest, count in sorted(counts.items()):
        if count <= 1:
            continue
        related = tuple(node.node_id for node in instructions if node.content_hash == digest)
        diagnostics.append(
            _diagnostic(
                package_id,
                "duplicate_instruction",
                "warning",
                f"identical instruction appears {count} times",
                related,
                by_id[related[0]].path,
            )
        )

    for cycle in _cycles(nodes, edges):
        diagnostics.append(
            _diagnostic(
                package_id,
                "dependency_cycle",
                "warning",
                "dependency cycle: " + " -> ".join(by_id[node_id].label for node_id in cycle),
                cycle,
                by_id[cycle[0]].path,
            )
        )
    return tuple(sorted(diagnostics, key=lambda item: item.diagnostic_id))


def _diagnostic(
    package_id: str,
    kind: str,
    severity: str,
    message: str,
    related: tuple[str, ...],
    path: str | None,
    metadata: dict[str, object] | None = None,
) -> Diagnostic:
    return Diagnostic(
        diagnostic_id=stable_id("diagnostic", package_id, kind, related, message),
        kind=kind,
        severity=severity,  # type: ignore[arg-type]
        message=message,
        related_node_ids=related,
        path=path,
        metadata=metadata or {},
    )


def _cycles(nodes: tuple[IRNode, ...], edges: tuple[GraphEdge, ...]) -> tuple[tuple[str, ...], ...]:
    adjacency: defaultdict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.kind in _DEPENDENCY_EDGES and edge.source != edge.target:
            adjacency[edge.source].add(edge.target)
    visited: set[str] = set()
    active: list[str] = []
    active_set: set[str] = set()
    found: set[tuple[str, ...]] = set()

    def walk(node_id: str) -> None:
        if node_id in active_set:
            start = active.index(node_id)
            cycle = tuple(active[start:])
            canonical = min(tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle)))
            found.add(canonical)
            return
        if node_id in visited:
            return
        active.append(node_id)
        active_set.add(node_id)
        for target in sorted(adjacency[node_id]):
            walk(target)
        active.pop()
        active_set.remove(node_id)
        visited.add(node_id)

    for node in nodes:
        walk(node.node_id)
    return tuple(sorted(found))
