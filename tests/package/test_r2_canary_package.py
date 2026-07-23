from __future__ import annotations

import json
from pathlib import Path

from gepase.evals.eval_plan import SourceProvenance
from gepase.evals.eval_plan_checks import verify_upstream_tree_manifest
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.ir import EdgeKind, FileKind, NodeKind

CANARY = Path("benchmarks/canaries/slack-gif-creator")


def test_pinned_slack_gif_creator_is_a_complete_package_snapshot() -> None:
    provenance = SourceProvenance.model_validate_json(
        (CANARY / "source-provenance.json").read_text(encoding="utf-8")
    )
    result = PackageAnalyzer().analyze(CANARY / "package")
    assert result.snapshot.package_id == "slack-gif-creator"
    assert result.snapshot.snapshot_hash == provenance.package_snapshot_hash
    assert provenance.source_commit == "fa0fa64bdc967915dc8399e803be67759e1e62b8"
    assert provenance.license_spdx == "Apache-2.0"
    assert verify_upstream_tree_manifest(Path.cwd(), provenance) == (True, ())
    assert {item.path for item in result.snapshot.files} == {
        "LICENSE.txt",
        "SKILL.md",
        "requirements.txt",
        "core/easing.py",
        "core/frame_composer.py",
        "core/gif_builder.py",
        "core/validators.py",
    }
    assert all(
        item.kind is FileKind.SCRIPT
        for item in result.snapshot.files
        if item.path.startswith("core/")
    )
    assert not result.graph.diagnostics


def test_canary_graph_connects_instructions_core_modules_and_dependencies() -> None:
    graph = PackageAnalyzer().analyze(CANARY / "package").graph
    dependency_nodes = [node for node in graph.nodes if node.kind is NodeKind.DEPENDENCY]
    assert {str(node.metadata["distribution"]) for node in dependency_nodes} == {
        "imageio",
        "imageio-ffmpeg",
        "numpy",
        "pillow",
    }
    core_file_ids = {
        node.node_id
        for node in graph.nodes
        if node.kind is NodeKind.FILE and node.path.startswith("core/")
    }
    assert core_file_ids
    assert all(
        any(edge.target == node_id and edge.kind is EdgeKind.REFERENCES for edge in graph.edges)
        for node_id in core_file_ids
    )
    source = json.loads((CANARY / "source-provenance.json").read_text(encoding="utf-8"))
    assert source["upstream_tree_hash"] == "c61d2f7bb6334b68a6936ad3f41ebfc7cb76fe2a"
