"""Fidelity-aware planned/observed evidence overlay for PackageGraph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from gepase.evals.evidence import EvaluationRecord, TraceCompleteness, TraceStep
from gepase.evals.schema import EvidenceTier
from gepase.evals.work_items import ExecutionBundle, PackageAccessKind
from gepase.package.ir import (
    EdgeKind,
    IRNode,
    NodeKind,
    PackageGraph,
    make_edge,
    make_node,
)
from gepase.schemas.common import FrozenModel
from gepase.store.artifacts import (
    resolve_scoped_artifact_index,
    resolve_verified_artifact,
    sha256_bytes,
    verify_candidate_bundle_artifact,
)


class PackageAccessMapping(FrozenModel):
    work_id: str
    task_id: str
    variant: str
    context_id: str
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_artifact: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sequence: int = Field(ge=0)
    access_kind: Literal["available", "read", "executed"]
    path: str
    supplied_node_id: str | None
    mapped_node_id: str | None
    mapping_strength: Literal["typed", "weak", "rejected"]
    reason: str
    bytes_loaded: int = Field(ge=0)
    tokens_loaded: int = Field(ge=0)


class PackageAccessOverlayAudit(FrozenModel):
    schema_version: str = "1.0.0"
    package_id: str
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_run: str
    source_run_seal_valid: bool
    work_item_source_refs: tuple[str, ...] = ()
    allowed_task_ids: tuple[str, ...]
    accepted_work_ids: tuple[str, ...]
    filtered_work_ids: tuple[str, ...]
    mappings: tuple[PackageAccessMapping, ...]
    observed_edges: int = Field(ge=0)
    mapped_events: int = Field(ge=0)
    rejected_events: int = Field(ge=0)
    typed_mapping_rate: float = Field(ge=0, le=1)
    planned_edges_added: int = Field(default=0, ge=0)
    weak_fallback_events: int = Field(default=0, ge=0)
    model_correction_ref: str | None = None
    model_correction_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    model_corrected_submission_ids: tuple[str, ...] = ()


class SelectorGraphBinding(FrozenModel):
    """Immutable provenance for the exact graph consumed by one selector."""

    schema_version: str = "1.0.0"
    mode: Literal["static_observed"]
    parent_candidate_id: str
    parent_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_ref: str
    snapshot_ref: str
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_ir_ref: str
    package_ir_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    static_graph_ref: str
    static_graph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selector_graph_ref: str
    selector_graph_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_ref: str
    coverage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    overlay_audit_ref: str
    overlay_audit_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    layer_counts: dict[str, int]
    evidence_run_ref: str
    evidence_variant: Literal["original", "candidate"]
    evidence_task_ids: tuple[str, ...]
    evidence_scope_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    graph_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cache_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_work_ids: tuple[str, ...]
    filtered_work_ids: tuple[str, ...]
    mapped_access_events: int = Field(ge=0)
    rejected_access_events: int = Field(ge=0)
    observed_edges: int = Field(ge=0)
    semantic_hypothesis_edges: Literal[0] = 0

    @model_validator(mode="after")
    def safe_and_layered(self) -> SelectorGraphBinding:
        for reference in (
            self.package_ref,
            self.snapshot_ref,
            self.package_ir_ref,
            self.static_graph_ref,
            self.selector_graph_ref,
            self.coverage_ref,
            self.overlay_audit_ref,
            self.evidence_run_ref,
        ):
            path = Path(reference)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("selector graph refs must be repository-relative")
        if not self.evidence_task_ids or not self.accepted_work_ids:
            raise ValueError("observed selector graph requires accepted parent-train evidence")
        if any(value < 0 for value in self.layer_counts.values()):
            raise ValueError("selector graph layer counts cannot be negative")
        if self.layer_counts.get("observed", 0) != self.observed_edges:
            raise ValueError("selector graph observed layer count disagrees with overlay audit")
        if self.layer_counts.get("planned", 0) != 0:
            raise ValueError("selector graph must not consume planned evidence")
        if self.layer_counts.get("semantic_hypothesis", 0) != 0:
            raise ValueError("GH-E0 selector graph must not consume semantic hypotheses")
        return self


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
        json.loads(path.read_text(encoding="utf-8")).get("record_id") for path in record_paths
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
        layer = "planned" if record.evidence_tier is EvidenceTier.E1_SIMULATED else "observed"
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
            mapped_execute += int(kind in {EdgeKind.PLANNED_EXECUTE, EdgeKind.OBSERVED_EXECUTE})
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


def overlay_package_access(
    graph: PackageGraph,
    run_dir: Path,
    *,
    allowed_task_ids: set[str],
    expected_graph_ref: str,
    expected_variant: Literal["original", "candidate"] = "original",
    expected_provider_id: str | None = None,
    expected_host: str | None = None,
    expected_model: str | None = None,
    expected_seed: int | None = None,
    expected_timeout_seconds: int | None = None,
    expected_candidate_id: str | None = None,
    expected_content_hash: str | None = None,
    expected_reference_key_hash: str | None = None,
    model_correction_ref: str | None = None,
) -> tuple[PackageGraph, PackageAccessOverlayAudit]:
    """Bind sealed typed PackageAccessEvent rows to one snapshot-scoped graph.

    This intentionally does not infer access from prose.  Work outside the caller's
    pre-registered task allowlist (for GH-P0: parent train only) is filtered before
    any edge is created.
    """

    run = run_dir.resolve()
    _index_path, indexed = resolve_scoped_artifact_index(run)
    metadata_path = run / "run-metadata.json"
    if indexed.get("run-metadata.json") != sha256_bytes(metadata_path.read_bytes()):
        raise ValueError("source run metadata is not sealed or hash-matched")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    model_corrections, correction_hash = _load_model_corrections(
        run,
        model_correction_ref,
    )
    expected_metadata = {
        "host": expected_host,
        "model": expected_model,
        "seed": expected_seed,
        "timeout_seconds": expected_timeout_seconds,
    }
    mismatched_metadata = [
        key
        for key, expected in expected_metadata.items()
        if expected is not None and metadata.get(key) != expected
    ]
    if mismatched_metadata:
        raise ValueError(f"source run provider/runtime mismatch: {sorted(mismatched_metadata)}")
    if metadata.get("split") not in {None, "train"}:
        raise ValueError("observed selector evidence must come from the train split")
    if expected_candidate_id is not None:
        if metadata.get("candidate_id") != expected_candidate_id:
            raise ValueError("candidate evidence is bound to another parent")
        if metadata.get("candidate_content_hash") != expected_content_hash:
            raise ValueError("candidate evidence content hash mismatch")
    elif metadata.get("mode") != "frozen-functional":
        raise ValueError("seed observed evidence must come from a functional reference run")
    if expected_reference_key_hash is not None and metadata.get("reference_key_hash") not in {
        None,
        expected_reference_key_hash,
    }:
        raise ValueError("candidate evidence reference/runtime key mismatch")
    # Historical R3 exports one legacy manifest at the run root.  The strict
    # fresh reference lifecycle exports the same canonical executor view under
    # ``exports/`` and separately seals every typed work item.  Both are Core
    # formats; either must be content-addressed before it can bind access.
    work_manifest_path = next(
        (
            path
            for path in (
                run / "executor-work-items.json",
                run / "exports/executor-batch.json",
            )
            if path.is_file()
        ),
        None,
    )
    work_item_source_refs: list[str] = []
    work_rows: list[dict[str, Any]] = []
    if work_manifest_path is not None:
        manifest_ref = work_manifest_path.relative_to(run).as_posix()
        if indexed.get(manifest_ref) != sha256_bytes(work_manifest_path.read_bytes()):
            raise ValueError("executor-work-items manifest is not sealed or hash-matched")
        manifest = json.loads(work_manifest_path.read_text(encoding="utf-8"))
        raw_rows = manifest.get("work_items") if isinstance(manifest, dict) else None
        if not isinstance(raw_rows, list) or not all(isinstance(row, dict) for row in raw_rows):
            raise ValueError("executor-work-items manifest has invalid work rows")
        work_rows = list(raw_rows)
        work_item_source_refs.append(manifest_ref)
    by_work = {str(row["work_id"]): row for row in work_rows}
    typed_work_paths = sorted((run / "executor-work-items").glob("*.json"))
    if work_manifest_path is None and not typed_work_paths:
        raise ValueError("sealed executor work item export is absent")
    for work_path in typed_work_paths:
        relative = work_path.relative_to(run).as_posix()
        if indexed.get(relative) != sha256_bytes(work_path.read_bytes()):
            raise ValueError(f"executor work item is not sealed: {relative}")
        row = json.loads(work_path.read_text(encoding="utf-8"))
        if not isinstance(row, dict):
            raise ValueError(f"executor work item is not an object: {relative}")
        work_id = str(row["work_id"])
        if work_id in by_work and by_work[work_id] != row:
            raise ValueError(f"executor work manifest disagrees with typed item: {work_id}")
        by_work[work_id] = row
        work_item_source_refs.append(relative)
    nodes = {node.node_id: node for node in graph.nodes}
    file_nodes = {node.path: node for node in graph.nodes if node.kind is NodeKind.FILE}
    edges = {edge.edge_id: edge for edge in graph.edges}
    mappings: list[PackageAccessMapping] = []
    accepted: list[str] = []
    filtered: list[str] = []
    mapped = 0
    rejected = 0
    observed = 0
    graph_reference = Path(expected_graph_ref)
    if graph_reference.is_absolute() or ".." in graph_reference.parts:
        raise ValueError("expected graph ref must be repository-relative")
    referenced_graph = next(
        (
            parent / graph_reference
            for parent in (run, *run.parents)
            if (parent / graph_reference).is_file()
        ),
        None,
    )
    if referenced_graph is None:
        raise ValueError("expected sealed graph ref cannot be resolved")
    try:
        if expected_candidate_id is not None:
            try:
                verify_candidate_bundle_artifact(
                    referenced_graph,
                    expected_candidate_id=expected_candidate_id,
                    expected_package_id=graph.package_id,
                    expected_content_hash=expected_content_hash or graph.snapshot_hash,
                )
            except ValueError:
                # Terminal historical runs predate Candidate sub-stores; their
                # complete evolution-level ArtifactStore remains authoritative.
                resolve_verified_artifact(referenced_graph)
        else:
            resolve_verified_artifact(referenced_graph)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(
            "referenced PackageGraph is not sealed or hash-matched"
        ) from error
    graph_payload = json.loads(referenced_graph.read_text(encoding="utf-8"))
    if graph_payload.get("snapshot_hash") != graph.snapshot_hash:
        raise ValueError("snapshot hash mismatch for referenced PackageGraph")
    canonical_submission_ids: set[str] = set()
    for record_path in sorted((run / "records").glob("*.json")):
        record_ref = record_path.relative_to(run).as_posix()
        if indexed.get(record_ref) != sha256_bytes(record_path.read_bytes()):
            raise ValueError(f"evaluation record is not sealed: {record_ref}")
        record = EvaluationRecord.model_validate_json(record_path.read_text(encoding="utf-8"))
        submission_id = record.provenance.submission_id
        if record.evidence_tier is EvidenceTier.E2_DELEGATED and submission_id is not None:
            canonical_submission_ids.add(submission_id)
    corrected_submission_ids: list[str] = []
    for path in sorted((run / "execution-submissions").glob("*.json")):
        relative = path.relative_to(run).as_posix()
        digest = sha256_bytes(path.read_bytes())
        if indexed.get(relative) != digest:
            raise ValueError(f"execution submission is not sealed or hash-matched: {relative}")
        submission = ExecutionBundle.model_validate_json(path.read_text(encoding="utf-8"))
        if canonical_submission_ids and submission.submission_id not in canonical_submission_ids:
            continue
        work = by_work.get(submission.work_id)
        if work is None:
            raise ValueError(f"execution work item is missing: {submission.work_id}")
        if str(work.get("task_id")) not in allowed_task_ids or work.get("skill_ref") is None:
            filtered.append(submission.work_id)
            continue
        if expected_provider_id is not None and submission.provider_id != expected_provider_id:
            raise ValueError(f"provider mismatch for {submission.work_id}")
        if expected_host is not None and submission.host != expected_host:
            raise ValueError(f"host mismatch for {submission.work_id}")
        effective_model = model_corrections.get(submission.submission_id, submission.model)
        if expected_model is not None and effective_model != expected_model:
            raise ValueError(f"model mismatch for {submission.work_id}")
        if effective_model != submission.model:
            corrected_submission_ids.append(submission.submission_id)
        if work.get("package_graph_ref") != expected_graph_ref:
            raise ValueError(f"snapshot graph ref mismatch for {submission.work_id}")
        node_map = {str(key): str(value) for key, value in work["package_node_map"].items()}
        sequences = [event.sequence for event in submission.package_access]
        if sequences != sorted(set(sequences)):
            raise ValueError(
                f"package access sequence is not unique and ordered: {submission.work_id}"
            )
        access_summary_path = run / "package-access" / f"{submission.work_id}.json"
        access_summary_ref = access_summary_path.relative_to(run).as_posix()
        if indexed.get(access_summary_ref) != sha256_bytes(access_summary_path.read_bytes()):
            raise ValueError(f"package access summary is not sealed: {submission.work_id}")
        access_summary = json.loads(access_summary_path.read_text(encoding="utf-8"))
        if (
            access_summary.get("valid") is not True
            or access_summary.get("variant") != expected_variant
        ):
            raise ValueError(f"package access summary is invalid: {submission.work_id}")
        evidence = make_node(
            graph.package_id,
            NodeKind.EVIDENCE,
            relative,
            f"package-access/{submission.work_id}",
            f"E2:{work['task_id']}:{expected_variant}",
            path.read_bytes(),
            mutable=False,
            metadata={
                "work_id": submission.work_id,
                "task_id": work["task_id"],
                "variant": expected_variant,
                "context_id": submission.context_id,
                "snapshot_hash": graph.snapshot_hash,
                "provider": submission.provider_id,
                "host": submission.host,
                "model": submission.model,
                "effective_model": effective_model,
                "trace_completeness": "typed_package_access",
                "source_artifact": relative,
                "source_sha256": digest,
            },
        )
        nodes[evidence.node_id] = evidence
        accepted.append(submission.work_id)
        for event in submission.package_access:
            expected_node_id = node_map.get(event.path)
            target = file_nodes.get(event.path)
            valid = (
                expected_node_id is not None
                and event.node_id == expected_node_id
                and target is not None
                and target.node_id == expected_node_id
            )
            mapping = PackageAccessMapping(
                work_id=submission.work_id,
                task_id=str(work["task_id"]),
                variant=expected_variant,
                context_id=submission.context_id or submission.work_id,
                snapshot_hash=graph.snapshot_hash,
                source_artifact=relative,
                source_sha256=digest,
                sequence=event.sequence,
                access_kind=event.kind.value,
                path=event.path,
                supplied_node_id=event.node_id,
                mapped_node_id=target.node_id if valid and target is not None else None,
                mapping_strength="typed" if valid else "rejected",
                reason=(
                    "node_id/path/snapshot binding verified" if valid else "typed binding mismatch"
                ),
                bytes_loaded=event.bytes_loaded,
                tokens_loaded=event.tokens_loaded,
            )
            mappings.append(mapping)
            if not valid or target is None:
                rejected += 1
                continue
            mapped += 1
            if event.kind is PackageAccessKind.AVAILABLE:
                continue
            kind = (
                EdgeKind.OBSERVED_READ
                if event.kind is PackageAccessKind.READ
                else EdgeKind.OBSERVED_EXECUTE
            )
            edge = make_edge(
                evidence.node_id,
                target.node_id,
                kind,
                layer="observed",
                identity=(submission.work_id, event.sequence, kind.value),
                evidence_tier="E2",
                evaluation_id=submission.work_id,
                task_id=str(work["task_id"]),
                provider=submission.provider_id,
                confidence=1.0,
                trace_completeness="typed_package_access",
                metadata={
                    "variant": expected_variant,
                    "context_id": submission.context_id,
                    "snapshot_hash": graph.snapshot_hash,
                    "host": submission.host,
                    "model": submission.model,
                    "effective_model": effective_model,
                    "trace_sequence": event.sequence,
                    "path": event.path,
                    "bytes_loaded": event.bytes_loaded,
                    "tokens_loaded": event.tokens_loaded,
                    "evidence_path": relative,
                    "source_sha256": digest,
                    "mapping_strength": "typed",
                },
            )
            edges[edge.edge_id] = edge
            observed += 1
    overlaid = PackageGraph(
        package_id=graph.package_id,
        snapshot_hash=graph.snapshot_hash,
        nodes=tuple(sorted(nodes.values(), key=lambda item: item.node_id)),
        edges=tuple(sorted(edges.values(), key=lambda item: item.edge_id)),
        diagnostics=graph.diagnostics,
    )
    total = mapped + rejected
    source_run = run.name
    for ancestor in run.parents:
        if ancestor.name == "runs" and ancestor.parent.name == "artifacts":
            source_run = (Path("artifacts/runs") / run.relative_to(ancestor)).as_posix()
            break
    audit = PackageAccessOverlayAudit(
        package_id=graph.package_id,
        snapshot_hash=graph.snapshot_hash,
        source_run=source_run,
        source_run_seal_valid=True,
        work_item_source_refs=tuple(work_item_source_refs),
        allowed_task_ids=tuple(sorted(allowed_task_ids)),
        accepted_work_ids=tuple(sorted(accepted)),
        filtered_work_ids=tuple(sorted(filtered)),
        mappings=tuple(mappings),
        observed_edges=observed,
        mapped_events=mapped,
        rejected_events=rejected,
        typed_mapping_rate=(mapped / total if total else 0.0),
        model_correction_ref=model_correction_ref,
        model_correction_sha256=correction_hash,
        model_corrected_submission_ids=tuple(sorted(corrected_submission_ids)),
    )
    return overlaid, audit


def _load_model_corrections(
    run: Path,
    correction_ref: str | None,
) -> tuple[dict[str, str], str | None]:
    """Validate one append-only, user-confirmed Host metadata correction.

    The correction changes only the effective model used for provenance checks;
    original submissions, records, ledger rows, and hashes remain untouched.
    """

    if correction_ref is None:
        return {}, None
    relative = Path(correction_ref)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("model correction ref must be repository-relative")
    audit_path = next(
        (parent / relative for parent in (run, *run.parents) if (parent / relative).is_file()),
        None,
    )
    if audit_path is None:
        raise ValueError("model correction audit cannot be resolved")
    audit_bytes = audit_path.read_bytes()
    audit = json.loads(audit_bytes)
    if audit.get("status") != "passed" or audit.get("run_id") != run.name:
        raise ValueError("model correction audit belongs to another or failed run")
    checks = audit.get("machine_checks")
    if not isinstance(checks, dict) or not checks or not all(checks.values()):
        raise ValueError("model correction audit machine checks are not all passing")
    decision_ref = audit.get("decision_ref")
    if not isinstance(decision_ref, str):
        raise ValueError("model correction audit lacks its decision ref")
    decision_relative = Path(decision_ref)
    if decision_relative.is_absolute() or ".." in decision_relative.parts:
        raise ValueError("model correction decision ref must be repository-relative")
    decision_path = next(
        (
            parent / decision_relative
            for parent in (run, *run.parents)
            if (parent / decision_relative).is_file()
        ),
        None,
    )
    if decision_path is None:
        raise ValueError("model correction decision cannot be resolved")
    decision_bytes = decision_path.read_bytes()
    if sha256_bytes(decision_bytes) != audit.get("decision_sha256"):
        raise ValueError("model correction decision hash mismatch")
    decision = json.loads(decision_bytes)
    binding = decision.get("run_binding")
    runtime_path = run / "runtime-session.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    if (
        not isinstance(binding, dict)
        or binding.get("run_id") != run.name
        or binding.get("config_hash") != runtime.get("config_hash")
        or decision.get("reviewer") != "user"
        or decision.get("correction_reason") != "host_cli_metadata_label_error"
    ):
        raise ValueError("model correction decision binding is invalid")
    checkpoint_id = binding.get("checkpoint_id")
    if not isinstance(checkpoint_id, str):
        raise ValueError("model correction decision lacks checkpoint binding")
    checkpoint_path = run / "budget-checkpoints" / f"{checkpoint_id}.json"
    if (
        not checkpoint_path.is_file()
        or sha256_bytes(checkpoint_path.read_bytes()) != binding.get("checkpoint_sha256")
    ):
        raise ValueError("model correction checkpoint hash mismatch")
    recorded_model = decision.get("recorded_model")
    effective_model = decision.get("effective_actual_model")
    if not isinstance(recorded_model, str) or not isinstance(effective_model, str):
        raise ValueError("model correction labels are invalid")
    originals = {
        str(item["submission_id"]): item
        for item in audit.get("original_submission_hashes", [])
        if isinstance(item, dict) and "submission_id" in item
    }
    corrections: dict[str, str] = {}
    for row in decision.get("affected_submissions", []):
        if not isinstance(row, dict):
            raise ValueError("model correction affected submission is invalid")
        submission_id = str(row.get("submission_id"))
        original = originals.get(submission_id)
        if original is None:
            raise ValueError("model correction lacks original submission hash")
        source_ref = Path(str(original.get("submission_ref")))
        if source_ref.is_absolute() or ".." in source_ref.parts:
            raise ValueError("corrected submission ref must be repository-relative")
        source_path = next(
            (
                parent / source_ref
                for parent in (run, *run.parents)
                if (parent / source_ref).is_file()
            ),
            None,
        )
        if source_path is None or sha256_bytes(source_path.read_bytes()) != original.get(
            "submission_sha256"
        ):
            raise ValueError("corrected original submission hash mismatch")
        submission = ExecutionBundle.model_validate_json(source_path.read_text(encoding="utf-8"))
        if (
            submission.submission_id != submission_id
            or submission.work_id != row.get("work_id")
            or submission.host_task_id != row.get("host_task_id")
            or submission.context_id != row.get("context_id")
            or submission.host != decision.get("host")
            or submission.model != recorded_model
        ):
            raise ValueError("model correction submission binding mismatch")
        corrections[submission_id] = effective_model
    if len(corrections) != audit.get("affected_submission_count"):
        raise ValueError("model correction affected submission count mismatch")
    return corrections, sha256_bytes(audit_bytes)


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
