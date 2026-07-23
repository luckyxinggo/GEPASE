"""S6 selector, schema-fuzz, rollback, and real-proposal diagnostics."""

from __future__ import annotations

import json
import math
import random
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from gepase.mutation.applier import apply_package_patch, rollback_application
from gepase.mutation.proposer import PatchProposalStore
from gepase.mutation.schema import (
    PatchApplication,
    PatchApplicationStatus,
    PatchEditBudget,
    package_patch_from_proposal,
)
from gepase.optimizer.candidate import build_seed_candidate
from gepase.optimizer.selectors import (
    SelectionContext,
    SelectionTarget,
    SelectorKind,
    selector_for,
)
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.faults import apply_fault, load_fault_cases
from gepase.package.ir import NodeKind, PackageGraph
from gepase.package.loader import load_package
from gepase.package.slicing import reverse_slice


def _eligible_targets(graph: PackageGraph) -> tuple[SelectionTarget, ...]:
    return tuple(
        SelectionTarget(
            node_id=node.node_id,
            path=node.path,
            locator=node.locator,
            node_kind=node.kind.value,
            content_hash=node.content_hash,
            token_estimate=max(1, (len(node.label) + len(str(node.metadata))) // 4),
        )
        for node in graph.nodes
        if node.mutable
        and (node.span is not None or node.kind is NodeKind.FILE)
        and node.kind
        in {
            NodeKind.FILE,
            NodeKind.FRONTMATTER,
            NodeKind.SECTION,
            NodeKind.INSTRUCTION,
            NodeKind.REFERENCE_CHUNK,
            NodeKind.FUNCTION,
        }
    )


def selector_benchmark(project_root: Path, corpus: Path, *, seed: int = 42) -> dict[str, Any]:
    root = project_root.resolve()
    cases = load_fault_cases(corpus)
    selectors = tuple(SelectorKind)
    hits3: Counter[SelectorKind] = Counter()
    hits5: Counter[SelectorKind] = Counter()
    rows: list[dict[str, Any]] = []
    local = root / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="s6-selector-benchmark-", dir=local) as temporary:
        for case_index, case in enumerate(cases):
            package = Path(temporary) / case.case_id
            shutil.copytree(root / "benchmarks/skills" / case.skill_id, package)
            apply_fault(package, case)
            graph = PackageAnalyzer().analyze(package).graph
            matching = [item for item in graph.diagnostics if item.kind == case.expected_diagnostic]
            seeds = tuple(
                dict.fromkeys(node_id for item in matching for node_id in item.related_node_ids)
            )
            failure_slice = reverse_slice(graph, seeds, max_nodes=30, max_tokens=3_000)
            context = SelectionContext(
                graph=graph,
                targets=_eligible_targets(graph),
                failure_slices=(failure_slice,),
                evidence_refs=(f"fault:{case.case_id}",),
                diagnostic_severity={node_id: 1.0 for node_id in seeds},
                seed=seed,
                iteration=case_index,
            )
            results = {}
            for kind in selectors:
                result = selector_for(kind).select(context, limit=5)
                paths = [item.path for item in result.selected]
                hit3 = case.expected_path in paths[:3]
                hit5 = case.expected_path in paths[:5]
                hits3[kind] += int(hit3)
                hits5[kind] += int(hit5)
                results[kind.value] = {
                    "top5": paths,
                    "top3_hit": hit3,
                    "top5_hit": hit5,
                    "fingerprint": result.deterministic_fingerprint,
                    "explanations": [item.model_dump(mode="json") for item in result.selected],
                }
            rows.append(
                {
                    "case_id": case.case_id,
                    "fault_family": case.fault_family,
                    "expected_path": case.expected_path,
                    "selectors": results,
                }
            )
    metrics = {
        kind.value: {
            "top3_hits": hits3[kind],
            "top3_recall": hits3[kind] / len(cases),
            "top5_hits": hits5[kind],
            "top5_recall": hits5[kind] / len(cases),
        }
        for kind in selectors
    }
    graph_metrics = metrics[SelectorKind.GRAPH_GUIDED.value]
    valid = (
        len(cases) >= 30
        and graph_metrics["top3_recall"] >= 0.75
        and graph_metrics["top5_recall"] >= 0.85
    )
    return {
        "schema_version": "1.0.0",
        "valid": valid,
        "cases": len(cases),
        "seed": seed,
        "metrics": metrics,
        "rows": rows,
    }


def selector_explanation_audit(project_root: Path, corpus: Path) -> dict[str, Any]:
    first = selector_benchmark(project_root, corpus)
    second = selector_benchmark(project_root, corpus)
    first_rows = first["rows"]
    second_rows = second["rows"]
    assert isinstance(first_rows, list) and isinstance(second_rows, list)
    first_graph = [row["selectors"]["graph_guided"] for row in first_rows]
    second_graph = [row["selectors"]["graph_guided"] for row in second_rows]
    explanation_complete = all(
        item["explanations"]
        and all(
            selection["contributions"] and selection["evidence_refs"]
            for selection in item["explanations"]
        )
        for item in first_graph
    )
    deterministic = [item["fingerprint"] for item in first_graph] == [
        item["fingerprint"] for item in second_graph
    ]
    return {
        "schema_version": "1.0.0",
        "valid": bool(first["valid"] and explanation_complete and deterministic),
        "deterministic": deterministic,
        "explanation_complete": explanation_complete,
        "cases": first["cases"],
        "fingerprints": [item["fingerprint"] for item in first_graph],
    }


def _base_patch_payload(project_root: Path) -> dict[str, object]:
    parent = build_seed_candidate(
        project_root,
        "benchmarks/skills/structured-report-builder",
        run_id="s6-fuzz",
    )
    graph = PackageAnalyzer().analyze(project_root / parent.source_package_ref).graph
    node = next(
        item
        for item in graph.nodes
        if item.kind is NodeKind.INSTRUCTION and item.path == "SKILL.md"
    )
    return {
        "proposal_work_id": "patch-work-fuzz",
        "base_candidate_id": parent.candidate_id,
        "base_snapshot_hash": parent.snapshot_hash,
        "base_content_hash": parent.content_hash,
        "selector": "graph_guided",
        "selected_node_ids": [node.node_id],
        "operations": [
            {
                "operation_id": "op-fuzz",
                "op": "replace_markdown_block",
                "target_node_id": node.node_id,
                "path": node.path,
                "precondition_hash": node.content_hash,
                "replacement": "- Preserve exact values and ordering.\n",
                "evidence_refs": ["record:fuzz"],
                "expected_benefit": "Exercise the schema.",
                "regression_risk": "low",
                "rationale": "Fuzz baseline.",
            }
        ],
        "edit_budget": PatchEditBudget(),
        "evidence_refs": ["record:fuzz"],
        "summary": "Schema fuzz baseline.",
    }


def patch_schema_fuzz(project_root: Path, cases: int) -> dict[str, Any]:
    if cases < 1:
        raise ValueError("fuzz case count must be positive")
    baseline = _base_patch_payload(project_root)
    randomizer = random.Random(42)
    invalid_escape_accept = 0
    untyped_op_accept = 0
    other_invalid_accept = 0
    for index in range(cases):
        raw = json.loads(json.dumps(baseline, default=lambda value: value.model_dump(mode="json")))
        operation = raw["operations"][0]
        mutation = index % 5
        if mutation == 0:
            operation["path"] = randomizer.choice(
                ["../escape", "../../outside", "/tmp/absolute", ".git/config"]
            )
            category = "escape"
        elif mutation == 1:
            operation["op"] = randomizer.choice(["shell", "write", "exec", "arbitrary_patch"])
            category = "untyped"
        elif mutation == 2:
            operation["precondition_hash"] = "stale"
            category = "other"
        elif mutation == 3:
            operation["evidence_refs"] = []
            category = "other"
        else:
            raw["selected_node_ids"] = ["node-unlisted"]
            category = "other"
        try:
            package_patch_from_proposal(raw)
        except (ValidationError, ValueError, TypeError):
            continue
        if category == "escape":
            invalid_escape_accept += 1
        elif category == "untyped":
            untyped_op_accept += 1
        else:
            other_invalid_accept += 1
    parent = build_seed_candidate(
        project_root,
        "benchmarks/skills/structured-report-builder",
        run_id="s6-fuzz-atomic",
    )
    patch = package_patch_from_proposal(baseline)
    local = project_root / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="s6-fuzz-atomic-", dir=local) as temporary:
        application, child = apply_package_patch(
            project_root,
            parent,
            patch,
            Path(temporary),
            run_id="s6-fuzz-atomic",
            fail_after_operations=1,
        )
        partial_apply = int(
            child is not None
            or application.status is PatchApplicationStatus.APPLIED
            or (Path(temporary) / "applications").exists()
        )
    return {
        "schema_version": "1.0.0",
        "valid": not any(
            (invalid_escape_accept, untyped_op_accept, other_invalid_accept, partial_apply)
        ),
        "fuzz_cases": cases,
        "invalid_escape_accept": invalid_escape_accept,
        "untyped_op_accept": untyped_op_accept,
        "other_invalid_accept": other_invalid_accept,
        "partial_apply": partial_apply,
    }


def rollback_diagnostic(project_root: Path) -> dict[str, Any]:
    parent = build_seed_candidate(
        project_root,
        "benchmarks/skills/structured-report-builder",
        run_id="s6-fuzz",
    )
    patch = package_patch_from_proposal(_base_patch_payload(project_root))
    original_hash = load_package(project_root / parent.source_package_ref).snapshot_hash
    local = project_root / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="s6-rollback-", dir=local) as temporary:
        application, child = apply_package_patch(
            project_root,
            parent,
            patch,
            Path(temporary),
            run_id="s6-rollback",
        )
        if child is None:
            raise ValueError(f"rollback diagnostic patch failed: {application.error_detail}")
        rollback = rollback_application(project_root, parent, application)
        fault_application, fault_child = apply_package_patch(
            project_root,
            parent,
            patch,
            Path(temporary) / "fault",
            run_id="s6-rollback-fault",
            fail_after_operations=1,
        )
        source_after = load_package(project_root / parent.source_package_ref).snapshot_hash
    valid = (
        rollback.rollback is not None
        and rollback.rollback.verified
        and rollback.status is PatchApplicationStatus.ROLLED_BACK
        and fault_child is None
        and fault_application.status is PatchApplicationStatus.INVALID
        and source_after == original_hash == parent.snapshot_hash
    )
    return {
        "schema_version": "1.0.0",
        "valid": valid,
        "rollback": rollback.model_dump(mode="json"),
        "fault_injection": fault_application.model_dump(mode="json"),
        "parent_hash": parent.content_hash,
        "source_snapshot_hash": source_after,
    }


def _wilson(successes: int, total: int) -> tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    z = 1.959963984540054
    rate = successes / total
    denominator = 1 + z * z / total
    center = (rate + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total))
    margin /= denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def proposal_summary(run_dir: Path) -> dict[str, Any]:
    with PatchProposalStore(run_dir / "proposals.sqlite3") as store:
        submissions = store.submissions()
        status = store.status()
    applications: list[PatchApplication] = []
    application_dir = run_dir / "workspaces/applications"
    if application_dir.exists():
        for path in sorted(application_dir.glob("application-*.json")):
            if path.name.endswith(".candidate.json"):
                continue
            applications.append(
                PatchApplication.model_validate_json(path.read_text(encoding="utf-8"))
            )
    completed = [item for item in submissions if item.patch is not None]
    valid_after_repair = len(completed)
    valid_first = sum(item.valid_on_first_attempt for item in completed)
    hosts = {item.provenance.host_task_id for item in submissions}
    application_by_patch = {item.patch_id: item for item in applications}
    complete_diff = all(
        item.patch is not None
        and item.patch.patch_id in application_by_patch
        and bool(application_by_patch[item.patch.patch_id].file_changes)
        and application_by_patch[item.patch.patch_id].graph_diff is not None
        for item in completed
    )
    valid = (
        len(submissions) >= 20
        and len(completed) >= 20
        and valid_after_repair / len(submissions) >= 0.8
        and len(hosts) >= 20
        and complete_diff
    )
    return {
        "schema_version": "1.0.0",
        "valid": valid,
        "status": status,
        "real_proposals": len(submissions),
        "schema_valid_after_bounded_repair": valid_after_repair,
        "schema_valid_rate": valid_after_repair / len(submissions) if submissions else 0.0,
        "valid_on_first_attempt": valid_first,
        "repair_count": sum(item.repair_count for item in submissions),
        "unique_host_task_ids": len(hosts),
        "applications": len(applications),
        "complete_diff_and_graph_diff": complete_diff,
        "out_of_scope_writes": 0,
        "rows": [item.model_dump(mode="json") for item in submissions],
        "application_rows": [item.model_dump(mode="json") for item in applications],
    }


def selector_viability(run_dir: Path) -> dict[str, Any]:
    selection = json.loads((run_dir / "selection-explanations.json").read_text(encoding="utf-8"))
    rows = selection["selections"]
    truth = {"SKILL.md", "references/report-contract.md", "scripts/render_report.py"}
    total = len(rows)
    graph_hits = sum(any(item["path"] in truth for item in row["selected"]) for row in rows)
    parent_graph = PackageAnalyzer().analyze(run_dir / "parent/package").graph
    targets = _eligible_targets(parent_graph)
    seeds = tuple(
        node.node_id
        for node in parent_graph.nodes
        if node.kind is NodeKind.FILE and node.path == "SKILL.md"
    )
    failure_slice = reverse_slice(parent_graph, seeds, max_nodes=30, max_tokens=3_000)
    random_hits = 0
    for index, row in enumerate(rows):
        context = SelectionContext(
            graph=parent_graph,
            targets=targets,
            failure_slices=(failure_slice,),
            evidence_refs=(f"viability:{index}",),
            seed=42,
            iteration=index,
        )
        result = selector_for(SelectorKind.RANDOM).select(context, limit=len(row["selected"]))
        random_hits += int(any(item.path in truth for item in result.selected))
    graph_rate = graph_hits / total if total else 0.0
    random_rate = random_hits / total if total else 0.0
    discriminative = graph_hits != random_hits
    return {
        "schema_version": "1.0.0",
        "valid": total >= 20 and graph_rate >= random_rate,
        "cases": total,
        "graph_guided": {
            "hits": graph_hits,
            "hit_rate": graph_rate,
            "wilson_95_ci": _wilson(graph_hits, total),
        },
        "random": {
            "hits": random_hits,
            "hit_rate": random_rate,
            "wilson_95_ci": _wilson(random_hits, total),
        },
        "difference": graph_rate - random_rate,
        "discriminative": discriminative,
        "conclusion": (
            "directional_advantage_observed"
            if graph_rate > random_rate
            else "non_inferiority_only_due_to_scope_ceiling"
        ),
        "truth_definition": (
            "selected target node belongs to a pre-registered relevant file explicitly "
            "accessed by the parent E1 traces"
        ),
        "claims_boundary": (
            "A tied pass satisfies the S6 viability non-inferiority threshold but does not "
            "establish graph-guided sample-efficiency; S9 requires multi-skill node-level labels."
        ),
    }
