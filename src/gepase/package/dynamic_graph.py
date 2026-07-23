"""Fidelity-aware planned/observed evidence overlay for PackageGraph."""

from __future__ import annotations

import json
from pathlib import Path

from gepase.evals.evidence import EvaluationRecord, TraceCompleteness, TraceStep
from gepase.evals.schema import EvidenceTier
from gepase.package.ir import (
    EdgeKind,
    IRNode,
    NodeKind,
    PackageGraph,
    make_edge,
    make_node,
)


def overlay_evidence(graph: PackageGraph, run_dir: Path) -> tuple[PackageGraph, dict[str, object]]:
    record_paths = sorted((run_dir / "records").glob("*.json"))
    records: list[tuple[EvaluationRecord, Path]] = []
    invalid_records = 0
    for path in record_paths:
        try:
            record = EvaluationRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            invalid_records += 1
            continue
        if record.skill_id == graph.package_id:
            records.append((record, path))
    all_record_ids = {
        json.loads(path.read_text(encoding="utf-8")).get("record_id")
        for path in record_paths
    }
    nodes = {node.node_id: node for node in graph.nodes}
    edges = {edge.edge_id: edge for edge in graph.edges}
    evidence_ref_errors = 0
    mapped_read = 0
    mapped_execute = 0
    planned_edges = 0
    observed_edges = 0
    for record, path in records:
        evidence = make_node(
            graph.package_id,
            NodeKind.EVIDENCE,
            path.relative_to(run_dir).as_posix(),
            f"evaluation/{record.record_id}",
            f"{record.evidence_tier.value}:{record.task_id}:{record.variant}",
            record.model_dump_json(),
            mutable=False,
            metadata={
                "record_id": record.record_id,
                "work_id": record.work_id,
                "task_id": record.task_id,
                "variant": record.variant,
                "tier": record.evidence_tier.value,
                "provider": record.provenance.provider_id,
                "host": record.provenance.host,
                "model": record.provenance.model,
                "record_path": path.relative_to(run_dir).as_posix(),
                "source_record_refs": list(record.source_record_refs),
            },
        )
        nodes[evidence.node_id] = evidence
        for source_ref in record.source_record_refs:
            if source_ref not in all_record_ids:
                evidence_ref_errors += 1
        trace = (
            record.planned_trace
            if record.evidence_tier is EvidenceTier.E1_SIMULATED
            else record.observed_trace
        )
        layer = (
            "planned"
            if record.evidence_tier is EvidenceTier.E1_SIMULATED
            else "observed"
        )
        for step in trace:
            kind = _step_kind(step, layer)
            target = _map_package_node(graph, step)
            if target is None and kind not in {
                EdgeKind.PLANNED_PRODUCE,
                EdgeKind.OBSERVED_PRODUCE,
            }:
                continue
            if kind in {EdgeKind.PLANNED_PRODUCE, EdgeKind.OBSERVED_PRODUCE}:
                artifact = make_node(
                    graph.package_id,
                    NodeKind.ARTIFACT,
                    "<artifact>",
                    f"artifact/{record.record_id}/{step.sequence}",
                    step.target or step.action,
                    f"{record.record_id}:{step.sequence}:{step.target}",
                    mutable=False,
                    metadata={
                        "artifact_root": record.artifact_root,
                        "declared_artifacts": [item.model_dump() for item in record.artifacts],
                    },
                )
                nodes[artifact.node_id] = artifact
                target = artifact
            assert target is not None
            edge = make_edge(
                evidence.node_id,
                target.node_id,
                kind,
                layer=layer,  # type: ignore[arg-type]
                identity=(record.record_id, step.sequence, kind.value),
                evidence_tier=record.evidence_tier.value,
                evaluation_id=record.record_id,
                task_id=record.task_id,
                provider=record.provenance.provider_id,
                confidence=_confidence(record.trace_completeness, layer),
                trace_completeness=record.trace_completeness.value,
                metadata={
                    "trace_sequence": step.sequence,
                    "action": step.action,
                    "target": step.target,
                    "tool": step.tool,
                    "outcome": step.outcome,
                    "evidence_path": path.relative_to(run_dir).as_posix(),
                    "source_record_refs": list(record.source_record_refs),
                },
            )
            edges[edge.edge_id] = edge
            planned_edges += int(layer == "planned")
            observed_edges += int(layer == "observed")
            mapped_read += int(kind in {EdgeKind.PLANNED_READ, EdgeKind.OBSERVED_READ})
            mapped_execute += int(
                kind in {EdgeKind.PLANNED_EXECUTE, EdgeKind.OBSERVED_EXECUTE}
            )
    overlaid = PackageGraph(
        package_id=graph.package_id,
        snapshot_hash=graph.snapshot_hash,
        nodes=tuple(sorted(nodes.values(), key=lambda item: item.node_id)),
        edges=tuple(sorted(edges.values(), key=lambda item: item.edge_id)),
        diagnostics=graph.diagnostics,
    )
    audit = {
        "package_id": graph.package_id,
        "records": len(records),
        "invalid_records": invalid_records,
        "planned_edges": planned_edges,
        "observed_edges": observed_edges,
        "mapped_read_edges": mapped_read,
        "mapped_execute_edges": mapped_execute,
        "evidence_ref_errors": evidence_ref_errors,
        "planned_observed_partition_valid": all(
            (edge.layer == "planned" and edge.evidence_tier == "E1")
            or (edge.layer == "observed" and edge.evidence_tier in {"E2", "E3"})
            or edge.layer == "static"
            for edge in overlaid.edges
        ),
    }
    return overlaid, audit


def _step_kind(step: TraceStep, layer: str) -> EdgeKind:
    text = " ".join(
        value.casefold() for value in (step.action, step.target, step.tool, step.outcome) if value
    )
    failed = any(marker in text for marker in ("failed", "error", "did not complete", "rejected"))
    if failed and layer == "observed":
        return EdgeKind.FAILED_AT
    if any(marker in text for marker in ("execute", "python", "bash", "run", "command")):
        return EdgeKind.PLANNED_EXECUTE if layer == "planned" else EdgeKind.OBSERVED_EXECUTE
    if any(marker in text for marker in ("write", "produce", "generate", "render", "output")):
        return EdgeKind.PLANNED_PRODUCE if layer == "planned" else EdgeKind.OBSERVED_PRODUCE
    return EdgeKind.PLANNED_READ if layer == "planned" else EdgeKind.OBSERVED_READ


def _map_package_node(graph: PackageGraph, step: TraceStep) -> IRNode | None:
    text = " ".join(value for value in (step.target, step.tool) if value)
    files = [node for node in graph.nodes if node.kind is NodeKind.FILE]
    ranked = sorted(files, key=lambda node: len(node.path), reverse=True)
    for node in ranked:
        if node.path in text or f"/{node.path}" in text:
            return node
    marker = f"benchmarks/skills/{graph.package_id}"
    if marker in text:
        return next((node for node in files if node.path == "SKILL.md"), None)
    for name in ("SKILL.md", "references/", "scripts/"):
        if name in text:
            return next((node for node in ranked if node.path in text), None)
    return None


def _confidence(completeness: TraceCompleteness, layer: str) -> float:
    if layer == "planned":
        return 0.55
    return {
        TraceCompleteness.COMPLETE: 0.95,
        TraceCompleteness.PARTIAL: 0.65,
        TraceCompleteness.PLANNED_ONLY: 0.4,
        TraceCompleteness.NONE: 0.2,
    }[completeness]
