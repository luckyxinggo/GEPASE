"""Deep Python AST extraction for package scripts and tests."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

from gepase.package.ir import EdgeKind, IRNode, NodeKind, SourceSpan, make_node
from gepase.package.parsing import ParsedFile, RelationFact


class _PythonVisitor(ast.NodeVisitor):
    def __init__(
        self,
        package_id: str,
        relative_path: str,
        source: str,
        module: IRNode,
    ) -> None:
        self.package_id = package_id
        self.relative_path = relative_path
        self.source = source
        self.module = module
        self.nodes: list[IRNode] = []
        self.relations: list[RelationFact] = []
        self.scope: list[IRNode] = [module]
        self.occurrence: defaultdict[tuple[str, str], int] = defaultdict(int)

    def _node(self, kind: NodeKind, locator: str, label: str, value: ast.AST) -> IRNode:
        segment = ast.get_source_segment(self.source, value) or label
        node = make_node(
            self.package_id,
            kind,
            self.relative_path,
            locator,
            label,
            segment,
            span=SourceSpan(
                start_line=max(1, getattr(value, "lineno", 1)),
                end_line=max(1, getattr(value, "end_lineno", getattr(value, "lineno", 1))),
            ),
        )
        self.nodes.append(node)
        self.relations.append(
            RelationFact(self.scope[-1].node_id, EdgeKind.CONTAINS, target_locator=locator)
        )
        return node

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            locator = self._locator("import", alias.name)
            imported = self._node(NodeKind.IMPORT, locator, alias.name, node)
            self.relations.append(
                RelationFact(imported.node_id, EdgeKind.IMPORTS, external_name=alias.name)
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module_name = "." * node.level + (node.module or "")
        locator = self._locator("import", module_name)
        imported = self._node(NodeKind.IMPORT, locator, module_name, node)
        self.relations.append(
            RelationFact(imported.node_id, EdgeKind.IMPORTS, external_name=module_name)
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        locator = self._locator("function", node.name)
        function = self._node(NodeKind.FUNCTION, locator, node.name, node)
        self.scope.append(function)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        locator = self._locator("class", node.name)
        class_node = self._node(NodeKind.CLASS, locator, node.name, node)
        self.scope.append(class_node)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        locator = self._locator("call", name)
        call = self._node(NodeKind.CALL, locator, name, node)
        self.relations.append(
            RelationFact(
                call.node_id,
                EdgeKind.CALLS,
                target_locator=f"symbol/{name}",
                external_name=name,
            )
        )
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        if _is_main_guard(node.test):
            locator = "entrypoint/__main__"
            entry = self._node(NodeKind.ENTRYPOINT, locator, "__main__", node)
            self.relations.append(
                RelationFact(entry.node_id, EdgeKind.EXECUTES, target_locator="symbol/main")
            )
        self.generic_visit(node)

    def scope_names(self) -> tuple[str, ...]:
        names = ["module"]
        for node in self.scope[1:]:
            names.append(node.label)
        return tuple(names)

    def _locator(self, kind: str, name: str) -> str:
        scope = ".".join(self.scope_names())
        self.occurrence[(f"{kind}/{scope}", name)] += 1
        return f"{kind}/{scope}/{name}#{self.occurrence[(f'{kind}/{scope}', name)]}"


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_call_name(node.value)}.{node.attr}"
    return type(node).__name__


def _is_main_guard(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "__name__"
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value == "__main__"
    )


def parse_python(
    package_id: str,
    package_root: Path,
    relative_path: str,
    file_node: IRNode,
) -> ParsedFile:
    source = (package_root / relative_path).read_text(encoding="utf-8")
    module = make_node(
        package_id,
        NodeKind.PYTHON_MODULE,
        relative_path,
        "module",
        relative_path,
        source,
    )
    relations = [RelationFact(file_node.node_id, EdgeKind.CONTAINS, target_locator="module")]
    try:
        tree = ast.parse(source, filename=relative_path)
    except SyntaxError as error:
        locator = f"error/syntax/{error.lineno or 1}"
        error_node = make_node(
            package_id,
            NodeKind.ERROR,
            relative_path,
            locator,
            "SyntaxError",
            error.msg,
            span=SourceSpan(
                start_line=max(1, error.lineno or 1),
                end_line=max(1, error.lineno or 1),
            ),
            metadata={"error_type": "syntax_error", "message": error.msg},
        )
        relations.append(RelationFact(module.node_id, EdgeKind.CONTAINS, target_locator=locator))
        return ParsedFile((module, error_node), tuple(relations))
    visitor = _PythonVisitor(package_id, relative_path, source, module)
    visitor.visit(tree)
    return ParsedFile(
        (module, *visitor.nodes),
        tuple((*relations, *visitor.relations)),
    )
