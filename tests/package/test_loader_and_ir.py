from __future__ import annotations

import json
import shutil
from pathlib import Path

import jsonschema
import pytest

from gepase.package.analyzer import PackageAnalyzer
from gepase.package.ir import EdgeKind, NodeKind, PackageGraph
from gepase.package.loader import load_package

PUBLIC_SKILLS = Path("benchmarks/skills")


def test_all_public_packages_are_fully_snapshotted_and_round_trip() -> None:
    analyzer = PackageAnalyzer()
    for package in sorted(PUBLIC_SKILLS.iterdir()):
        result = analyzer.analyze(package)
        actual = sum(path.is_file() for path in package.rglob("*"))
        assert len(result.snapshot.files) == actual
        assert len(result.package_ir.nodes) == len(result.graph.nodes)
        assert not result.graph.diagnostics
        restored = PackageGraph.model_validate_json(result.graph.model_dump_json())
        assert restored == result.graph
        assert all(
            node.kind is not NodeKind.UNKNOWN or node.metadata.get("reason")
            for node in result.graph.nodes
        )


def test_loader_rejects_symlink_escape(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "SKILL.md").write_text("# Safe\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (package / "escaped.txt").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink escapes"):
        load_package(package)


def test_runtime_python_cache_does_not_change_package_identity(tmp_path: Path) -> None:
    package = tmp_path / "package"
    core = package / "core"
    core.mkdir(parents=True)
    (package / "SKILL.md").write_text("# Stable\n", encoding="utf-8")
    (core / "tool.py").write_text("VALUE = 1\n", encoding="utf-8")
    before = load_package(package)

    cache = core / "__pycache__"
    cache.mkdir()
    (cache / "tool.cpython-313.pyc").write_bytes(b"runtime cache")
    (core / "loose.pyc").write_bytes(b"runtime cache")

    after = load_package(package)
    assert after.snapshot_hash == before.snapshot_hash
    assert after.files == before.files


def test_markdown_semantic_node_ids_ignore_line_offsets(tmp_path: Path) -> None:
    source = PUBLIC_SKILLS / "structured-report-builder"
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    shutil.copytree(source, first_root)
    shutil.copytree(source, second_root)
    skill = second_root / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    skill.write_text(text.replace("## Workflow", "\n\n## Workflow"), encoding="utf-8")
    analyzer = PackageAnalyzer()
    first = analyzer.analyze(first_root).graph
    second = analyzer.analyze(second_root).graph
    first_sections = {
        (node.path, node.locator): node.node_id
        for node in first.nodes
        if node.kind is NodeKind.SECTION
    }
    second_sections = {
        (node.path, node.locator): node.node_id
        for node in second.nodes
        if node.kind is NodeKind.SECTION
    }
    assert first_sections == second_sections


def test_python_ast_and_markdown_edges_are_semantic() -> None:
    graph = PackageAnalyzer().analyze(PUBLIC_SKILLS / "structured-report-builder").graph
    kinds = {node.kind for node in graph.nodes}
    edge_kinds = {edge.kind for edge in graph.edges}
    assert {NodeKind.SECTION, NodeKind.INSTRUCTION, NodeKind.FUNCTION, NodeKind.CALL} <= kinds
    assert NodeKind.ENTRYPOINT in kinds
    assert {EdgeKind.REFERENCES, EdgeKind.EXECUTES, EdgeKind.CALLS} <= edge_kinds
    script = next(
        node
        for node in graph.nodes
        if node.kind is NodeKind.FILE and node.path == "scripts/render_report.py"
    )
    assert any(
        edge.target == script.node_id and edge.kind is EdgeKind.EXECUTES
        for edge in graph.edges
    )


def test_graph_json_conforms_to_public_schema() -> None:
    graph = PackageAnalyzer().analyze(PUBLIC_SKILLS / "tabular-context-builder").graph
    schema = json.loads(Path("schemas/package_graph.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(graph.model_dump(mode="json"), schema)
