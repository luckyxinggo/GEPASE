"""End-to-end package analysis service used by CLI and stage gates."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gepase.package.dynamic_graph import overlay_evidence
from gepase.package.graph import compile_graph
from gepase.package.ir import PackageGraph, PackageIR, PackageSnapshot
from gepase.package.loader import load_package
from gepase.reporting.graph_report import render_graph_report
from gepase.store.artifacts import ArtifactStore


@dataclass(frozen=True)
class AnalysisResult:
    snapshot: PackageSnapshot
    package_ir: PackageIR
    graph: PackageGraph
    dynamic_audit: dict[str, object] | None = None

    def summary(self) -> dict[str, object]:
        unknown = [node for node in self.graph.nodes if node.kind.value == "unknown"]
        return {
            "package_id": self.snapshot.package_id,
            "snapshot_hash": self.snapshot.snapshot_hash,
            "scanned_files": len(self.snapshot.files),
            "manifest_files": len(self.snapshot.files),
            "nodes": len(self.graph.nodes),
            "edges": len(self.graph.edges),
            "static_edges": sum(edge.layer == "static" for edge in self.graph.edges),
            "planned_edges": sum(edge.layer == "planned" for edge in self.graph.edges),
            "observed_edges": sum(edge.layer == "observed" for edge in self.graph.edges),
            "diagnostics": len(self.graph.diagnostics),
            "parse_crash": 0,
            "unknown_nodes": len(unknown),
            "unknown_nodes_with_reason": sum(bool(node.metadata.get("reason")) for node in unknown),
            "dynamic_audit": self.dynamic_audit,
        }


class PackageAnalyzer:
    def analyze(
        self,
        package_root: Path,
        *,
        evidence_run: Path | None = None,
    ) -> AnalysisResult:
        snapshot = load_package(package_root)
        package_ir, graph = compile_graph(package_root.resolve(), snapshot)
        audit = None
        if evidence_run is not None:
            graph, audit = overlay_evidence(graph, evidence_run.resolve())
        return AnalysisResult(snapshot, package_ir, graph, audit)

    def write(self, result: AnalysisResult, output_dir: Path) -> dict[str, object]:
        store = ArtifactStore(output_dir)
        store.write_json("snapshot.json", result.snapshot.model_dump(mode="json"))
        store.write_json("package-ir.json", result.package_ir.model_dump(mode="json"))
        store.write_json("graph.json", result.graph.model_dump(mode="json"))
        store.write_json(
            "diagnostics.json",
            {
                "schema_version": result.graph.schema_version,
                "package_id": result.graph.package_id,
                "diagnostics": [item.model_dump(mode="json") for item in result.graph.diagnostics],
            },
        )
        if result.dynamic_audit is not None:
            store.write_json("dynamic-audit.json", result.dynamic_audit)
        store.write_text(
            "graph-report.html",
            render_graph_report(result.graph),
            "text/html",
        )
        return {**result.summary(), "artifact_verification": store.verify().as_dict()}
