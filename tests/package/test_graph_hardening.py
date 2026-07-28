from __future__ import annotations

import json
from pathlib import Path

import pytest

from gepase.optimizer.graph_selector import GraphGuidedComponentSelector
from gepase.optimizer.selectors import SelectionContext, SelectionTarget
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.coverage import audit_graph_coverage
from gepase.package.dynamic_graph import overlay_package_access
from gepase.package.ir import FailureSlice, FailureSliceNode, NodeKind

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "benchmarks/canaries/slack-gif-creator/package"
R3 = ROOT / "artifacts/runs/r3-slack-gif-creator-paired"
GRAPH_REF = "artifacts/runs/r2-slack-gif-creator-evalplan/package/graph.json"


def _train_tasks() -> set[str]:
    summary = json.loads((R3 / "functional-run-summary.json").read_text(encoding="utf-8"))
    return {
        str(row["task_id"])
        for row in summary["pair_summaries"]
        if row["split"] == "train"
    }


def test_every_file_has_explicit_parse_status_and_mutation_capability() -> None:
    result = PackageAnalyzer().analyze(PACKAGE)
    audit = audit_graph_coverage(result.snapshot, result.graph)
    assert audit.file_node_count == audit.snapshot_file_count == 7
    assert audit.file_node_coverage == 1.0
    assert all(
        row.parse_status.value in {"deep", "shallow", "opaque", "error"}
        for row in audit.files
    )
    assert all(row.mutation_capabilities for row in audit.files)


def test_sealed_train_package_access_binds_to_observed_graph_and_selector() -> None:
    result = PackageAnalyzer().analyze(PACKAGE)
    overlaid, audit = overlay_package_access(
        result.graph,
        R3,
        allowed_task_ids=_train_tasks(),
        expected_graph_ref=GRAPH_REF,
    )
    assert audit.source_run_seal_valid
    assert len(audit.accepted_work_ids) == 5
    assert audit.observed_edges > 0
    assert audit.mapped_events > 0 and audit.rejected_events == 0
    assert audit.typed_mapping_rate == 1.0
    assert not any(edge.layer == "planned" for edge in overlaid.edges)

    script = next(
        node
        for node in overlaid.nodes
        if node.kind is NodeKind.FILE and node.path == "core/easing.py"
    )
    context = SelectionContext(
        graph=overlaid,
        targets=(
            SelectionTarget(
                node_id=script.node_id,
                path=script.path,
                locator=script.locator,
                node_kind=script.kind.value,
                content_hash=script.content_hash,
                token_estimate=10,
            ),
        ),
        failure_slices=(
            FailureSlice(
                package_id=overlaid.package_id,
                seed_node_ids=(script.node_id,),
                nodes=(
                    FailureSliceNode(
                        node_id=script.node_id,
                        rank=1,
                        distance=0,
                        score=1.0,
                        reason="fixture failure seed",
                    ),
                ),
                omitted_nodes=0,
                token_estimate=1,
            ),
        ),
        evidence_refs=("sealed:r3:train",),
    )
    selected = GraphGuidedComponentSelector().select(context, limit=1).selected[0]
    dynamic = next(item for item in selected.contributions if item.feature == "dynamic_access")
    assert dynamic.raw_value > 0
    assert selected.eligible
    assert selected.validation_intensity.level.value in {"elevated", "full"}

    all_targets = tuple(
        SelectionTarget(
            node_id=node.node_id,
            path=node.path,
            locator=node.locator,
            node_kind=node.kind.value,
            content_hash=node.content_hash,
            token_estimate=10,
        )
        for node in overlaid.nodes
        if node.mutable
        and (node.span is not None or node.kind is NodeKind.FILE)
        and node.kind
        in {
            NodeKind.FILE,
            NodeKind.FRONTMATTER,
            NodeKind.SECTION,
            NodeKind.INSTRUCTION,
            NodeKind.FUNCTION,
        }
    )
    full = GraphGuidedComponentSelector().select(
        context.model_copy(update={"targets": all_targets}), limit=len(all_targets)
    )
    high_fan_out = [item for item in full.selected if item.high_blast_radius]
    assert high_fan_out
    assert all(item.eligible for item in high_fan_out)
    assert all(item.validation_intensity.level.value == "full" for item in high_fan_out)


def test_overlay_rejects_cross_snapshot_binding() -> None:
    graph = PackageAnalyzer().analyze(PACKAGE).graph.model_copy(
        update={"snapshot_hash": "0" * 64}
    )
    with pytest.raises(ValueError, match="snapshot hash mismatch"):
        overlay_package_access(
            graph,
            R3,
            allowed_task_ids=_train_tasks(),
            expected_graph_ref=GRAPH_REF,
        )


def test_config_keys_local_refs_and_python_imports_are_deterministic(tmp_path: Path) -> None:
    package = tmp_path / "config-package"
    (package / "scripts").mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: config-package\ndescription: fixture\n---\n\n# Fixture\n",
        encoding="utf-8",
    )
    (package / "config.json").write_text(
        '{"entrypoint":"scripts/run.py","nested":{"enabled":true}}\n',
        encoding="utf-8",
    )
    (package / "scripts/helper.py").write_text(
        "def clean(value: str) -> str:\n    return value.strip()\n",
        encoding="utf-8",
    )
    (package / "scripts/run.py").write_text(
        "from scripts.helper import clean\n\n"
        "def run(value: str) -> str:\n"
        "    return clean(value)\n",
        encoding="utf-8",
    )
    first = PackageAnalyzer().analyze(package)
    second = PackageAnalyzer().analyze(package)
    assert first.graph == second.graph
    config_file = next(
        node
        for node in first.graph.nodes
        if node.kind is NodeKind.FILE and node.path == "config.json"
    )
    assert config_file.metadata["parse_status"] == "deep"
    assert any(node.kind is NodeKind.CONFIG_KEY for node in first.graph.nodes)
    assert any(
        edge.kind.value == "references"
        and next(node for node in first.graph.nodes if node.node_id == edge.target).path
        == "scripts/run.py"
        for edge in first.graph.edges
    )
    assert any(
        edge.kind.value == "imports"
        and next(node for node in first.graph.nodes if node.node_id == edge.target).path
        == "scripts/helper.py"
        for edge in first.graph.edges
    )
