"""Run the GH-P0 sealed-evidence replay and all offline machine Gates."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stage_gate_support import (
    load_json_object,
    protected_tree_hashes,
    run_command,
    tree_hash,
)

from gepase.mutation.applier import apply_package_patch
from gepase.mutation.schema import PatchEditBudget, package_patch_from_proposal
from gepase.mutation.target_set import choose_bounded_target_set
from gepase.optimizer.candidate import build_seed_candidate
from gepase.optimizer.graph_selector import (
    GraphGuidedComponentSelector,
    LegacyGraphGuidedComponentSelector,
)
from gepase.optimizer.selectors import (
    FeatureContribution,
    RankedSelection,
    SelectionContext,
    SelectionTarget,
)
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.coverage import audit_graph_coverage
from gepase.package.dynamic_graph import overlay_package_access
from gepase.package.ir import FailureSlice, NodeKind, PackageGraph, PackageSnapshot
from gepase.package.loader import load_package
from gepase.store.artifacts import ArtifactStore, canonical_json_bytes, sha256_bytes

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "artifacts/stages/GH-P0"
R2 = ROOT / "artifacts/runs/r2-slack-gif-creator-evalplan"
R3 = ROOT / "artifacts/runs/r3-slack-gif-creator-paired"
R4 = ROOT / "artifacts/runs/r4-slack-gif-creator-evolution"
PACKAGE = ROOT / "benchmarks/canaries/slack-gif-creator/package"
GRAPH_REF = "artifacts/runs/r2-slack-gif-creator-evalplan/package/graph.json"


def _load(path: Path) -> dict[str, Any]:
    return load_json_object(path, root=ROOT)


def _protected_hashes() -> dict[str, object]:
    return protected_tree_hashes(ROOT, public_canary_source=PACKAGE)


def _run(command: tuple[str, ...], commands: list[str]) -> dict[str, object]:
    return run_command(
        command,
        root=ROOT,
        commands=commands,
        environment={"UV_CACHE_DIR": "/tmp/gepase-ghp0-uv-cache"},
    )


def _targets(graph: PackageGraph) -> tuple[SelectionTarget, ...]:
    kinds = {
        NodeKind.FILE,
        NodeKind.FRONTMATTER,
        NodeKind.SECTION,
        NodeKind.INSTRUCTION,
        NodeKind.REFERENCE_CHUNK,
        NodeKind.FUNCTION,
        NodeKind.CONFIG_KEY,
    }
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
        and (node.span is not None or node.kind in {NodeKind.FILE, NodeKind.CONFIG_KEY})
        and node.kind in kinds
    )


def _rank_payload(rows: tuple[RankedSelection, ...], limit: int = 25) -> list[dict[str, object]]:
    return [item.model_dump(mode="json") for item in rows[:limit]]


def _replay(
    old_graph: PackageGraph,
    new_graph: PackageGraph,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    old_rows: list[dict[str, object]] = []
    new_rows: list[dict[str, object]] = []
    comparisons: list[dict[str, object]] = []
    for path in sorted((R4 / "proposal-work-items").glob("*.json")):
        work = _load(path)
        failure_slice = FailureSlice.model_validate(
            work["actionable_side_information"]["graph_slice"]
        )
        evidence_refs = tuple(str(item) for item in work["evidence_refs"])
        severity = {node_id: 1.0 for node_id in failure_slice.seed_node_ids}
        old_context = SelectionContext(
            graph=old_graph,
            targets=_targets(old_graph),
            failure_slices=(failure_slice,),
            evidence_refs=evidence_refs,
            diagnostic_severity=severity,
        )
        new_context = SelectionContext(
            graph=new_graph,
            targets=_targets(new_graph),
            failure_slices=(failure_slice,),
            evidence_refs=evidence_refs,
            diagnostic_severity=severity,
        )
        old_result = LegacyGraphGuidedComponentSelector().select(
            old_context, limit=len(old_context.targets)
        )
        new_result = GraphGuidedComponentSelector().select(
            new_context, limit=len(new_context.targets)
        )
        old_by_id = {item.node_id: item for item in old_result.selected}
        new_by_id = {item.node_id: item for item in new_result.selected}
        common = set(old_by_id) & set(new_by_id)
        rank_changes = sum(old_by_id[node].rank != new_by_id[node].rank for node in common)
        new_dynamic = sum(
            any(
                contribution.feature == "dynamic_access" and contribution.raw_value > 0
                for contribution in item.contributions
            )
            for item in new_result.selected
        )
        old_executable_top10 = sum(
            item.path.endswith((".py", ".sh")) for item in old_result.selected[:10]
        )
        new_executable_top10 = sum(
            item.path.endswith((".py", ".sh")) for item in new_result.selected[:10]
        )
        executable_alternative = next(
            (
                item.model_dump(mode="json")
                for item in new_result.selected
                if item.path.endswith((".py", ".sh"))
            ),
            None,
        )
        high_fan_out = [item for item in new_result.selected if item.high_blast_radius]
        old_rows.append(
            {
                "work_id": work["work_id"],
                "task_id": work["task_id"],
                "policy": "v0.1 frozen legacy selector",
                "graph_layer_counts": dict(Counter(edge.layer for edge in old_graph.edges)),
                "ranking": _rank_payload(old_result.selected),
            }
        )
        new_rows.append(
            {
                "work_id": work["work_id"],
                "task_id": work["task_id"],
                "policy": "GH-P0 decoupled relevance/exploration/risk",
                "graph_layer_counts": dict(Counter(edge.layer for edge in new_graph.edges)),
                "ranking": _rank_payload(new_result.selected),
                "first_executable_alternative": executable_alternative,
            }
        )
        comparisons.append(
            {
                "work_id": work["work_id"],
                "same_failure_slice": True,
                "same_evidence_refs": True,
                "old_top1": old_result.selected[0].model_dump(mode="json"),
                "new_top1": new_result.selected[0].model_dump(mode="json"),
                "top1_changed": old_result.selected[0].node_id != new_result.selected[0].node_id,
                "common_target_rank_changes": rank_changes,
                "targets_with_nonzero_dynamic": new_dynamic,
                "old_executable_top10": old_executable_top10,
                "new_executable_top10": new_executable_top10,
                "executable_alternative_present": executable_alternative is not None,
                "high_fan_out_targets": len(high_fan_out),
                "high_fan_out_ineligible": sum(not item.eligible for item in high_fan_out),
                "high_fan_out_with_full_validation": sum(
                    item.validation_intensity.level.value == "full" for item in high_fan_out
                ),
            }
        )
    return (
        {"schema_version": "1.0.0", "rows": old_rows},
        {"schema_version": "1.0.0", "rows": new_rows},
        {"schema_version": "1.0.0", "comparisons": comparisons},
    )


def _fixture_rank(node_id: str, path: str, locator: str, rank: int) -> RankedSelection:
    return RankedSelection(
        rank=rank,
        node_id=node_id,
        path=path,
        locator=locator,
        score=float(3 - rank),
        contributions=(
            FeatureContribution(
                feature="fixture_causal_support",
                raw_value=1.0,
                weight=1.0,
                contribution=1.0,
            ),
        ),
        evidence_refs=("fixture:shared-failure",),
        reason_code="deterministic_fixture",
    )


def _target_set_fixture() -> dict[str, object]:
    package_ref = "tests/fixtures/graph_hardening/target_set_package"
    package = ROOT / package_ref
    graph = PackageAnalyzer().analyze(package).graph
    instruction = next(
        node
        for node in graph.nodes
        if node.kind is NodeKind.INSTRUCTION and "worker.py" in node.label
    )
    function = next(node for node in graph.nodes if node.kind is NodeKind.FUNCTION)
    ranked = (
        _fixture_rank(instruction.node_id, instruction.path, instruction.locator, 1),
        _fixture_rank(function.node_id, function.path, function.locator, 2),
    )
    single, absent = choose_bounded_target_set(
        graph,
        ranked,
        parent_candidate_id="fixture-parent",
        evidence_refs=("fixture:shared-failure",),
        scope_reason="Single target remains the default.",
        max_targets=1,
    )
    selected, target_set = choose_bounded_target_set(
        graph,
        ranked,
        parent_candidate_id="fixture-parent",
        evidence_refs=("fixture:shared-failure",),
        scope_reason="Instruction and implementation are connected by one static reference.",
        max_targets=2,
    )
    if target_set is None:
        raise ValueError("deterministic fixture did not form a TargetSet")
    parent = build_seed_candidate(ROOT, package_ref, run_id="gh-p0-fixture")
    budget = PatchEditBudget(
        max_operations=2,
        max_changed_files=2,
        max_added_files=0,
        max_deleted_files=0,
        allow_file_topology_edits=False,
    )
    patch = package_patch_from_proposal(
        {
            "proposal_work_id": "gh-p0-fixture-work",
            "base_candidate_id": parent.candidate_id,
            "base_snapshot_hash": parent.snapshot_hash,
            "base_content_hash": parent.content_hash,
            "selector": "graph_guided",
            "selected_node_ids": [item.node_id for item in selected],
            "operations": [
                {
                    "operation_id": "op-fixture-instruction",
                    "op": "replace_markdown_block",
                    "target_node_id": instruction.node_id,
                    "path": instruction.path,
                    "precondition_hash": instruction.content_hash,
                    "replacement": "Read and execute `scripts/worker.py` exactly once.",
                    "evidence_refs": ["fixture:shared-failure"],
                    "expected_benefit": "Bound the execution instruction.",
                    "regression_risk": "low",
                    "rationale": "The shared failure covers instruction and implementation.",
                },
                {
                    "operation_id": "op-fixture-script",
                    "op": "replace_python_function",
                    "target_node_id": function.node_id,
                    "path": function.path,
                    "precondition_hash": function.content_hash,
                    "replacement": (
                        "def render(value: str) -> str:\n"
                        "    return value.strip().lower()\n"
                    ),
                    "evidence_refs": ["fixture:shared-failure"],
                    "expected_benefit": "Make the implementation deterministic.",
                    "regression_risk": "medium",
                    "rationale": (
                        "The static graph connects this implementation to the instruction."
                    ),
                },
            ],
            "edit_budget": budget,
            "evidence_refs": ["fixture:shared-failure"],
            "summary": "Deterministic atomic two-target fixture.",
        }
    )
    source_before = load_package(package).snapshot_hash
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gh-p0-fixture-", dir=local) as temporary:
        application, child = apply_package_patch(
            ROOT,
            parent,
            patch,
            Path(temporary),
            run_id="gh-p0-fixture",
            fail_after_operations=1,
        )
        partial_workspace_absent = not (Path(temporary) / "applications").exists()
    source_after = load_package(package).snapshot_hash
    return {
        "schema_version": "1.0.0",
        "default_single_target_count": len(single),
        "default_target_set_absent": absent is None,
        "target_set": target_set.model_copy(
            update={"parent_candidate_id": parent.candidate_id}
        ).model_dump(mode="json"),
        "limits": {"targets": 2, "files": 2, "operations": 2},
        "same_parent": True,
        "same_failure_evidence": True,
        "topology_edits_allowed": False,
        "binary_mutation_allowed": False,
        "fault_injection_status": application.status.value,
        "fault_injection_error": application.error_code,
        "candidate_created": child is not None,
        "partial_workspace_absent": partial_workspace_absent,
        "source_hash_unchanged": source_before == source_after,
        "valid": bool(
            len(selected) == 2
            and target_set.causal_path_edge_ids
            and application.status.value == "invalid"
            and child is None
            and partial_workspace_absent
            and source_before == source_after
        ),
    }


def _gate(gate_id: str, passed: bool, detail: str, evidence: list[str]) -> dict[str, object]:
    return {
        "gate_id": gate_id,
        "status": "passed" if passed else "failed",
        "detail": detail,
        "evidence": evidence,
    }


def main() -> int:
    STAGE.mkdir(parents=True, exist_ok=True)
    before = _protected_hashes()
    old_snapshot = PackageSnapshot.model_validate_json(
        (R2 / "package/snapshot.json").read_text(encoding="utf-8")
    )
    old_graph = PackageGraph.model_validate_json(
        (R2 / "package/graph.json").read_text(encoding="utf-8")
    )
    current = PackageAnalyzer().analyze(PACKAGE)
    if current.snapshot.snapshot_hash != old_snapshot.snapshot_hash:
        raise ValueError("public canary source no longer matches the R2 frozen snapshot")
    summary = _load(R3 / "functional-run-summary.json")
    asi = _load(R3 / "asi-dataset.json")
    known_graph_nodes = {node.node_id for node in current.graph.nodes}
    asi_target_ids = {
        str(node_id)
        for row in asi["rows"]
        for analysis in row["analyses"]
        for node_id in analysis["target_node_ids"]
    }
    asi_reference_audit = {
        "source_ref": "artifacts/runs/r3-slack-gif-creator-paired/asi-dataset.json",
        "source_sha256": sha256_bytes((R3 / "asi-dataset.json").read_bytes()),
        "analyzer_submission_rows": len(asi["rows"]),
        "target_node_ids": len(asi_target_ids),
        "resolvable_target_node_ids": len(asi_target_ids & known_graph_nodes),
        "unresolved_target_node_ids": sorted(asi_target_ids - known_graph_nodes),
        "consumed_for_new_analysis": False,
        "audit_only": True,
    }
    train_tasks = {
        str(row["task_id"])
        for row in summary["pair_summaries"]
        if row["split"] == "train"
    }
    new_graph, overlay = overlay_package_access(
        current.graph,
        R3,
        allowed_task_ids=train_tasks,
        expected_graph_ref=GRAPH_REF,
    )
    old_coverage = audit_graph_coverage(old_snapshot, old_graph)
    new_coverage = audit_graph_coverage(current.snapshot, new_graph)
    old_replay, new_replay, comparison = _replay(old_graph, new_graph)
    target_fixture = _target_set_fixture()
    comparisons = comparison["comparisons"]
    assert isinstance(comparisons, list)
    ranking_changes = sum(bool(item["top1_changed"]) for item in comparisons)
    any_rank_changes = sum(int(item["common_target_rank_changes"]) for item in comparisons)
    dynamic_targets = sum(int(item["targets_with_nonzero_dynamic"]) for item in comparisons)
    executable_reachability_change = sum(
        int(item["new_executable_top10"]) - int(item["old_executable_top10"])
        for item in comparisons
    )
    explicit_status_gain = sum(row.parse_status_explicit for row in new_coverage.files) - sum(
        row.parse_status_explicit for row in old_coverage.files
    )
    risk_report = {
        "schema_version": "1.0.0",
        "ranking_risk_policy": {
            "risk_is_eligibility": False,
            "risk_penalty_is_capped": True,
            "risk_maps_to_validation_intensity": True,
        },
        "replay": [
            {
                "work_id": item["work_id"],
                "old_executable_top10": item["old_executable_top10"],
                "new_executable_top10": item["new_executable_top10"],
                "executable_alternative_present": item["executable_alternative_present"],
                "new_top1_validation_intensity": item["new_top1"][
                    "validation_intensity"
                ],
                "new_top1_score_breakdown": item["new_top1"]["score_breakdown"],
                "new_top1_eligible": item["new_top1"]["eligible"],
                "high_fan_out_targets": item["high_fan_out_targets"],
                "high_fan_out_ineligible": item["high_fan_out_ineligible"],
                "high_fan_out_with_full_validation": item[
                    "high_fan_out_with_full_validation"
                ],
            }
            for item in comparisons
        ],
        "all_replays_have_executable_alternative": all(
            bool(item["executable_alternative_present"]) for item in comparisons
        ),
        "real_replay_script_reached_top1": any(
            str(item["new_top1"]["path"]).endswith((".py", ".sh"))
            for item in comparisons
        ),
        "executable_top10_net_change": executable_reachability_change,
        "high_fan_out_targets": sum(
            int(item["high_fan_out_targets"]) for item in comparisons
        ),
        "high_fan_out_ineligible": sum(
            int(item["high_fan_out_ineligible"]) for item in comparisons
        ),
        "high_fan_out_with_full_validation": sum(
            int(item["high_fan_out_with_full_validation"]) for item in comparisons
        ),
    }
    offline_changes = {
        "coverage": explicit_status_gain > 0,
        "ranking": ranking_changes > 0 or any_rank_changes > 0,
        "reachability": executable_reachability_change != 0,
        "risk_explanation": all(
            bool(item["new_top1"]["score_breakdown"]) for item in comparisons
        ),
    }
    offline_value = bool(
        overlay.observed_edges > 0
        and dynamic_targets > 0
        and any(offline_changes.values())
    )

    commands: list[str] = []
    targeted = _run(
        (
            "uv",
            "run",
            "pytest",
            "-q",
            "tests/package/test_graph_hardening.py",
            "tests/package/test_graph_analysis.py",
            "tests/optimizer/test_selectors.py",
            "tests/mutation/test_target_set.py",
            "tests/mutation/test_schema_applier.py",
            "tests/mutation/test_proposer.py",
        ),
        commands,
    )
    full_pytest = _run(
        (
            "uv",
            "run",
            "pytest",
            "-q",
            "--junitxml",
            "artifacts/stages/GH-P0/test-results.xml",
        ),
        commands,
    )
    ruff = _run(("uv", "run", "ruff", "check", "."), commands)
    pyright = _run(("uv", "run", "pyright"), commands)
    schema_first = _run(("uv", "run", "python", "scripts/export_core_schemas.py"), commands)
    schemas_after_first = {
        path.name: path.read_bytes() for path in sorted((ROOT / "schemas").glob("*.json"))
    }
    schema_second = _run(("uv", "run", "python", "scripts/export_core_schemas.py"), commands)
    schemas_after_second = {
        path.name: path.read_bytes() for path in sorted((ROOT / "schemas").glob("*.json"))
    }
    schema_idempotent = schemas_after_first == schemas_after_second
    secrets = _run(
        ("uv", "run", "python", "scripts/check_secrets.py", "--format", "json"), commands
    )
    markdown = _run(("uv", "run", "python", "scripts/check_markdown_links.py"), commands)
    license_check = _run(("uv", "run", "python", "scripts/check_license.py"), commands)
    diff_check = _run(("git", "diff", "--check"), commands)
    verification = {
        "schema_version": "1.0.0",
        "checks": {
            "targeted_pytest": targeted,
            "full_pytest": full_pytest,
            "ruff": ruff,
            "pyright": pyright,
            "schema_export_first": schema_first,
            "schema_export_second": schema_second,
            "schema_idempotent": schema_idempotent,
            "schema_count": len(schemas_after_second),
            "secret_and_private_path_scan": secrets,
            "markdown_links": markdown,
            "license": license_check,
            "git_diff_check": diff_check,
        },
    }
    checks_passed = all(
        bool(value["ok"])
        for value in verification["checks"].values()
        if isinstance(value, dict) and "ok" in value
    ) and schema_idempotent
    after = _protected_hashes()
    immutable = before == after

    preflight = {
        "schema_version": "1.0.0",
        "stage_id": "GH-P0",
        "branch": subprocess.run(
            ("git", "branch", "--show-current"),
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.strip(),
        "head": subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.strip(),
        "offline_only": True,
        "input_snapshot_hash": old_snapshot.snapshot_hash,
        "sealed_inputs": {
            "r2_snapshot": sha256_bytes((R2 / "package/snapshot.json").read_bytes()),
            "r2_graph": sha256_bytes((R2 / "package/graph.json").read_bytes()),
            "r3_artifact_index": sha256_bytes((R3 / "artifact-index.json").read_bytes()),
            "r3_asi_dataset": asi_reference_audit["source_sha256"],
            "r4_artifact_index": sha256_bytes((R4 / "artifact-index.json").read_bytes()),
        },
        "protected_before": before,
        "protected_after": after,
        "protected_unchanged": immutable,
        "call_budget": {
            "agent": 0,
            "headless_api": 0,
            "executor": 0,
            "grader": 0,
            "comparator": 0,
            "analyzer": 0,
            "proposer": 0,
            "eval": 0,
            "new_candidate": 0,
            "new_skill_effect_score": 0,
        },
        "analyzer_asi_reference_audit": asi_reference_audit,
        "forbidden_features": {
            "gh_p1": False,
            "graphrag": False,
            "codebase_memory_mcp": False,
            "semantic_hypothesis_edges": False,
            "binary_asset_mutation": False,
        },
    }
    offline_report = {
        "schema_version": "1.0.0",
        "valid": offline_value,
        "status": "passed" if offline_value else "stalled",
        "pre_registered_changes": offline_changes,
        "metrics": {
            "explicit_parse_status_gain": explicit_status_gain,
            "observed_edges_added": overlay.observed_edges,
            "typed_mapping_rate": overlay.typed_mapping_rate,
            "targets_with_nonzero_dynamic": dynamic_targets,
            "top1_changes": ranking_changes,
            "common_target_rank_changes": any_rank_changes,
            "executable_top10_net_change": executable_reachability_change,
        },
        "conclusion_zh": (
            "同一 sealed evidence/failure slice 下, typed observed edge 已进入 selector; "
            "parse status、排名/脚本可达性和风险解释出现可审计变化。"
            if offline_value
            else "新旧 replay 未产生可解释变化, GH-P0 停滞且不解锁 GH-P1。"
        ),
        "does_not_claim_skill_improvement": True,
    }
    gates = [
        _gate(
            "GHP0-G00-offline_only",
            all(value == 0 for value in preflight["call_budget"].values()),
            "All Agent/API/evaluation/proposal/candidate/effect-score counters are zero.",
            ["preflight.json"],
        ),
        _gate(
            "GHP0-G01-source_and_artifact_immutable",
            immutable,
            "R2-R5/S10, public canary, deployable Package, and skills_test tree hashes match.",
            ["preflight.json"],
        ),
        _gate(
            "GHP0-G02-graph_coverage_explicit",
            bool(
                new_coverage.file_node_coverage == 1.0
                and all(item.parse_status_explicit for item in new_coverage.files)
            ),
            f"File coverage={new_coverage.file_node_coverage:.3f}; explicit statuses="
            f"{sum(item.parse_status_explicit for item in new_coverage.files)}/"
            f"{len(new_coverage.files)}.",
            ["coverage-audit.json"],
        ),
        _gate(
            "GHP0-G03-observed_overlay_bound",
            bool(
                overlay.source_run_seal_valid
                and overlay.typed_mapping_rate == 1.0
                and overlay.rejected_events == 0
                and overlay.planned_edges_added == 0
            ),
            f"Mapped typed events={overlay.mapped_events}; rejected={overlay.rejected_events}; "
            f"held-out/no-skill work filtered={len(overlay.filtered_work_ids)}.",
            ["overlay-mapping.json", "verification.json"],
        ),
        _gate(
            "GHP0-G04-selector_consumes_dynamic",
            overlay.observed_edges > 0 and dynamic_targets > 0,
            f"Observed edges={overlay.observed_edges}; replay targets with non-zero dynamic="
            f"{dynamic_targets}.",
            ["new-selector-replay.json", "overlay-mapping.json"],
        ),
        _gate(
            "GHP0-G05-risk_does_not_forbid_exploration",
            bool(
                risk_report["all_replays_have_executable_alternative"]
                and risk_report["real_replay_script_reached_top1"]
                and int(risk_report["high_fan_out_targets"]) > 0
                and int(risk_report["high_fan_out_ineligible"]) == 0
                and int(risk_report["high_fan_out_with_full_validation"])
                == int(risk_report["high_fan_out_targets"])
                and all(bool(item["new_top1_eligible"]) for item in risk_report["replay"])
            ),
            "Risk is separately reported, executable alternatives remain visible, and one "
            "sealed replay ranks a Python target first.",
            ["risk-intensity-report.json", "new-selector-replay.json"],
        ),
        _gate(
            "GHP0-G06-bounded_target_set_atomic",
            bool(target_fixture["valid"]),
            "Default is one target; connected same-parent fixture is bounded to 2/2/2 and "
            "fault injection leaves no partial candidate or source mutation.",
            ["target-set-fixture.json", "verification.json"],
        ),
        _gate(
            "GHP0-G07-offline_value_gate",
            offline_value,
            offline_report["conclusion_zh"],
            ["offline-value-gate.json", "selector-replay-comparison.json"],
        ),
        _gate(
            "GHP0-G08-regression_and_seal",
            checks_passed,
            f"Targeted/full tests, Ruff, Pyright, {len(schemas_after_second)} idempotent "
            f"schemas, security/docs/license and diff checks passed={checks_passed}.",
            ["verification.json", "test-results.xml", "artifact-index.json"],
        ),
    ]
    machine = {
        "schema_version": "1.0.0",
        "stage_id": "GH-P0",
        "valid": all(item["status"] == "passed" for item in gates),
        "passed": sum(item["status"] == "passed" for item in gates),
        "failed": sum(item["status"] == "failed" for item in gates),
        "offline_value_gate": offline_report,
        "gates": gates,
    }
    store = ArtifactStore(STAGE)
    store.write_json("preflight.json", preflight)
    store.write_json(
        "coverage-audit.json",
        {
            "schema_version": "1.0.0",
            "old": old_coverage.model_dump(mode="json"),
            "new": new_coverage.model_dump(mode="json"),
        },
    )
    store.write_json("overlay-mapping.json", overlay.model_dump(mode="json"))
    store.write_json("old-selector-replay.json", old_replay)
    store.write_json("new-selector-replay.json", new_replay)
    store.write_json("selector-replay-comparison.json", comparison)
    store.write_json("risk-intensity-report.json", risk_report)
    store.write_json("target-set-fixture.json", target_fixture)
    store.write_json("offline-value-gate.json", offline_report)
    store.write_json("verification.json", verification)
    store.write_json("machine-gates.json", machine)
    store.write_json("new-package-graph.json", new_graph.model_dump(mode="json"))
    store.write_text("commands.log", "\n".join(commands) + "\n")
    if (STAGE / "test-results.xml").is_file():
        store.index_existing("test-results.xml", "application/xml")
    head = str(preflight["head"])
    source_scope = [
        ROOT / "src",
        ROOT / "tests",
        ROOT / "scripts",
        ROOT / "schemas",
        ROOT / "state.md",
    ]
    source_digest = hashlib.sha256(
        canonical_json_bytes([tree_hash(path) for path in source_scope])
    ).hexdigest()
    report = {
        "schema_version": "1.0.0",
        "stage_id": "GH-P0",
        "status": "complete" if machine["valid"] else "blocked",
        "started_from_commit": head,
        "finished_commit": head,
        "source_tree_hash": source_digest,
        "generated_at": datetime.now(UTC).isoformat(),
        "input_artifacts": [
            {"path": GRAPH_REF, "sha256": preflight["sealed_inputs"]["r2_graph"]},
            {
                "path": "artifacts/runs/r3-slack-gif-creator-paired/artifact-index.json",
                "sha256": preflight["sealed_inputs"]["r3_artifact_index"],
            },
            {
                "path": "artifacts/runs/r4-slack-gif-creator-evolution/artifact-index.json",
                "sha256": preflight["sealed_inputs"]["r4_artifact_index"],
            },
        ],
        "output_artifacts": [
            item
            for item in _load(STAGE / "artifact-index.json")["artifacts"]
            if item["path"] != "stage_report.json"
        ],
        "commands": commands,
        "gate_results": gates,
        "real_agent_runs": 0,
        "headless_provider_runs": 0,
        "metrics": {
            **offline_report["metrics"],
            "machine_gates_passed": machine["passed"],
            "machine_gates_total": len(gates),
            "new_candidates": 0,
            "new_skill_effect_scores": 0,
        },
        "known_issues": [
            "GH-P0 is an offline mechanism validation; it adds no Skill-effect evidence.",
            "The only public effect result remains the sealed v0.1 canary; no GH-P1 work ran.",
        ],
        "design_decisions": [
            "Typed R3 parent-train package_access is the only dynamic overlay source.",
            "Risk is capped in ranking and primarily controls validation intensity.",
            (
                "Single target remains default; a two-target scope requires a typed "
                "static/observed path."
            ),
        ],
        "unlocks": ["GH-P1 design eligibility"] if offline_value and machine["valid"] else [],
        "conclusion_boundary": {
            "code_implemented": True,
            "engineering_mechanism_tested": bool(machine["valid"]),
            "new_algorithm_effect_validated": False,
        },
    }
    store.write_json("stage_report.json", report)
    post_generation_security = _run(
        ("uv", "run", "python", "scripts/check_secrets.py", "--format", "json"),
        commands,
    )
    if not post_generation_security["ok"]:
        raise ValueError("post-generation secret/private-path scan failed")
    verification["checks"]["post_generation_secret_and_private_path_scan"] = (
        post_generation_security
    )
    gates[-1]["detail"] = str(gates[-1]["detail"]) + (
        " Post-generation secret/private-path scan also passed."
    )
    machine["gates"] = gates
    report["commands"] = commands
    report["gate_results"] = gates
    store.write_json("verification.json", verification)
    store.write_json(
        "post-generation-security.json",
        {
            "schema_version": "1.0.0",
            "valid": True,
            "exit_code": post_generation_security["exit_code"],
            "summary": post_generation_security["summary"],
        },
    )
    store.write_json("machine-gates.json", machine)
    store.write_text("commands.log", "\n".join(commands) + "\n")
    report["output_artifacts"] = [
        item
        for item in _load(STAGE / "artifact-index.json")["artifacts"]
        if item["path"] != "stage_report.json"
    ]
    store.write_json("stage_report.json", report)
    final_seal = store.verify()
    if not final_seal.valid or final_seal.unindexed_files:
        raise ValueError(f"GH-P0 artifact seal failed: {final_seal.as_dict()}")
    # One final unpersisted scan covers the post-generation security record and final index.
    environment = dict(os.environ)
    environment["UV_CACHE_DIR"] = "/tmp/gepase-ghp0-uv-cache"
    final_security = subprocess.run(
        ("uv", "run", "python", "scripts/check_secrets.py", "--format", "json"),
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if final_security.returncode != 0:
        raise ValueError("final independent secret/private-path scan failed")
    return 0 if machine["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
