from __future__ import annotations

import json
import shutil
from pathlib import Path

from gepase.package.analyzer import PackageAnalyzer
from gepase.package.diff import graph_diff
from gepase.package.faults import evaluate_fault_corpus, evaluate_localization
from gepase.package.ir import EdgeKind, NodeKind
from gepase.package.slicing import reverse_slice


def test_graph_diff_preserves_stable_nodes_and_bounds_blast_radius(tmp_path: Path) -> None:
    source = Path("benchmarks/skills/policy-evidence-evaluator")
    before_root = tmp_path / "before"
    after_root = tmp_path / "after"
    shutil.copytree(source, before_root)
    shutil.copytree(source, after_root)
    skill = after_root / "SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8").replace(
            "Compute rates as accepted / total",
            "Compute every displayed rate as accepted / total",
        ),
        encoding="utf-8",
    )
    analyzer = PackageAnalyzer()
    before = analyzer.analyze(before_root).graph
    after = analyzer.analyze(after_root).graph
    difference = graph_diff(before, after)
    assert difference.modified_nodes
    assert difference.blast_radius > len(difference.modified_nodes)
    all_node_ids = {node.node_id for node in (*before.nodes, *after.nodes)}
    assert difference.blast_radius < len(all_node_ids)
    unchanged_before = {
        node.node_id: node.content_hash
        for node in before.nodes
        if node.path not in {"SKILL.md", "."}
    }
    unchanged_after = {
        node.node_id: node.content_hash
        for node in after.nodes
        if node.path not in {"SKILL.md", "."}
    }
    assert unchanged_before == unchanged_after


def test_dynamic_overlay_keeps_planned_and_observed_evidence_separate(
    package_graph_evidence_run: Path,
) -> None:
    analyzer = PackageAnalyzer()
    graph = analyzer.analyze(
        Path("benchmarks/skills/structured-report-builder"),
        evidence_run=package_graph_evidence_run,
    ).graph
    dynamic = [edge for edge in graph.edges if edge.layer != "static"]
    assert any(edge.kind is EdgeKind.PLANNED_READ for edge in dynamic)
    assert any(edge.kind is EdgeKind.OBSERVED_EXECUTE for edge in dynamic)
    assert all(
        (edge.layer == "planned" and edge.evidence_tier == "E1")
        or (edge.layer == "observed" and edge.evidence_tier in {"E2", "E3"})
        for edge in dynamic
    )
    records = {
        path.name
        for path in (package_graph_evidence_run / "records").glob("*.json")
    }
    assert all(
        Path(str(edge.metadata["evidence_path"])).name in records
        for edge in dynamic
    )


def test_reverse_slice_is_ranked_and_bounded(package_graph_evidence_run: Path) -> None:
    graph = PackageAnalyzer().analyze(
        Path("benchmarks/skills/structured-report-builder"),
        evidence_run=package_graph_evidence_run,
    ).graph
    seed = next(
        node.node_id
        for node in graph.nodes
        if node.kind is NodeKind.FILE and node.path == "SKILL.md"
    )
    result = reverse_slice(graph, (seed,), max_nodes=7, max_tokens=300)
    assert result.nodes[0].node_id == seed
    assert len(result.nodes) <= 7
    assert result.token_estimate <= 300
    assert [node.rank for node in result.nodes] == list(range(1, len(result.nodes) + 1))


def test_fault_corpus_detection_and_localization_meet_s3_thresholds() -> None:
    corpus = Path("benchmarks/fault_localization.jsonl")
    diagnosis = evaluate_fault_corpus(Path.cwd(), corpus)
    localization = evaluate_localization(Path.cwd(), corpus)
    recall = diagnosis["recall"]
    top5_recall = localization["top5_recall"]
    assert isinstance(recall, (int, float))
    assert isinstance(top5_recall, (int, float))
    assert diagnosis["cases"] == 30 and recall >= 0.95
    assert top5_recall >= 0.8
    assert not localization["misses"]


def test_report_is_self_contained_and_embeds_graph_data(tmp_path: Path) -> None:
    analyzer = PackageAnalyzer()
    result = analyzer.analyze(Path("benchmarks/skills/structured-report-builder"))
    summary = analyzer.write(result, tmp_path / "analysis")
    report = (tmp_path / "analysis/graph-report.html").read_text(encoding="utf-8")
    assert summary["artifact_verification"]["valid"] is True  # type: ignore[index]
    assert "<svg" in report and 'id="graph-data"' in report
    assert "https://" not in report and "http://" not in report
    graph_payload = json.loads((tmp_path / "analysis/graph.json").read_text(encoding="utf-8"))
    assert graph_payload["package_id"] == "structured-report-builder"
