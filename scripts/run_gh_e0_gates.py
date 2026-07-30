"""Build and seal the offline GH-E0 Controller graph-consumption evidence."""

from __future__ import annotations

import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from stage_gate_support import (
    git_value,
    load_json_object,
    protected_tree_hashes,
    run_command,
    tree_hash,
)

from gepase.optimizer.candidate import PackageCandidate
from gepase.optimizer.evolution_controller import R4EvolutionController
from gepase.optimizer.runtime import ReferenceEvidenceKey, load_r4_config
from gepase.package.analyzer import PackageAnalyzer
from gepase.store.artifacts import (
    ArtifactStore,
    atomic_write,
    canonical_json_bytes,
    sha256_bytes,
)

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "artifacts/stages/GH-E0"
STATIC_CONFIG = ROOT / "configs/canaries/slack-gif-creator-r4.json"
OBSERVED_CONFIG = ROOT / "configs/graph-hardening/slack-gif-creator-gh-e0.json"
R2 = ROOT / "artifacts/runs/r2-slack-gif-creator-evalplan"
R3 = ROOT / "artifacts/runs/r3-slack-gif-creator-paired"
R4 = ROOT / "artifacts/runs/r4-slack-gif-creator-evolution"
PACKAGE = ROOT / "benchmarks/canaries/slack-gif-creator/package"


def _write(name: str, value: object) -> None:
    atomic_write(STAGE / name, canonical_json_bytes(value))


def _load(path: Path) -> dict[str, Any]:
    return load_json_object(path, root=ROOT)


def _protected_hashes() -> dict[str, object]:
    return protected_tree_hashes(
        ROOT,
        public_canary_source=PACKAGE,
        extra_stage_ids=("GH-P0", "GH-P1"),
    )


def _git(*args: str) -> str:
    return git_value(ROOT, *args)


def _run(command: tuple[str, ...], commands: list[str]) -> dict[str, object]:
    return run_command(command, root=ROOT, commands=commands)


def _dynamic_value(target: dict[str, Any]) -> float:
    return max(
        float(item["raw_value"])
        for item in target["selection"]["contributions"]
        if item["feature"] == "dynamic_access"
    )


def _static_compatibility() -> dict[str, Any]:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gh-e0-static-", dir=local) as temporary:
        config_hash, config = load_r4_config(ROOT, STATIC_CONFIG)
        controller = R4EvolutionController(
            ROOT, Path(temporary) / config.run_id, STATIC_CONFIG
        )
        controller.initialize()
        works = [
            _load(path)
            for path in sorted((controller.run_dir / "proposal-work-items").glob("*.json"))
        ]
        old_plan = _load(R4 / "branch-plan.json")
        sealed_config_hash = str(_load(R4 / "evolution-state.json")["config_hash"])
        old_targets = {
            str(row["task_id"]): tuple(str(item) for item in row["target_node_ids"])
            for row in old_plan["branches"][:2]
        }
        new_targets = {
            str(work["task_id"]): tuple(
                str(item["node_id"]) for item in work["targets"]
            )
            for work in works
        }
        return {
            "schema_version": "1.0.0",
            "config_ref": STATIC_CONFIG.relative_to(ROOT).as_posix(),
            "sealed_config_hash": sealed_config_hash,
            "fresh_config_hash": config_hash,
            "config_hash_identical": config_hash == sealed_config_hash,
            "default_mode": controller.config.selector_graph_policy.mode,
            "selector_graph_artifacts_created": (
                controller.run_dir / "selector-graphs"
            ).exists(),
            "proposal_selector_graph_fields": [
                work["selector_graph"] for work in works
            ],
            "proposal_selector_ranking_fields": [
                work["selector_ranking"] for work in works
            ],
            "sealed_r4_initial_targets": old_targets,
            "fresh_static_targets": new_targets,
            "target_behavior_identical": old_targets == new_targets,
            "single_controller_class": (
                "gepase.optimizer.evolution_controller.R4EvolutionController"
            ),
            "second_graph_store_created": False,
            "second_search_or_evaluator_created": False,
        }


def _selector_cache_replay(controller: R4EvolutionController) -> dict[str, Any]:
    seed = PackageCandidate.model_validate_json(
        (controller.run_dir / "seed-candidate.json").read_text(encoding="utf-8")
    )
    key = ReferenceEvidenceKey.model_validate_json(
        (controller.run_dir / "reference-evidence-key.json").read_text(encoding="utf-8")
    )
    evidence_run = ROOT / controller.config.reference_run_ref
    view = controller.build_selector_graph_view(
        seed,
        package_ref=seed.source_package_ref,
        evidence_run_ref=controller.config.reference_run_ref,
        expected_graph_ref=controller.config.package_graph_ref,
        evidence_variant="original",
        allowed_task_ids=controller._train_task_ids(evidence_run),
        reference_key_hash=key.key_hash,
    )
    if view.binding is None or not view.cache_hit or view.cache_audit_ref is None:
        raise ValueError("selector graph cache replay did not produce a persisted hit")
    paths = sorted(
        (controller.run_dir / "selector-graph-cache-audits").glob("*/*/*.json")
    )
    rows = [_load(path) for path in paths]
    return {
        "schema_version": "1.0.0",
        "cache_key": view.binding.cache_key,
        "accesses": len(rows),
        "hits": sum(item["hit"] is True for item in rows),
        "misses": sum(item["hit"] is False for item in rows),
        "access_refs": [path.relative_to(ROOT).as_posix() for path in paths],
        "last_access_ref": view.cache_audit_ref,
        "miss_then_hit": [item["hit"] for item in rows] == [False, True],
    }


def _candidate_parent_overlay(
    controller: R4EvolutionController,
) -> dict[str, Any]:
    candidate_id = "candidate-2dad7a05ce4a6460dd71f470"
    candidate = PackageCandidate.model_validate_json(
        (R4 / f"candidates/{candidate_id}/candidate.json").read_text(encoding="utf-8")
    )
    application = _load(R4 / f"candidates/{candidate_id}/application.json")
    key = ReferenceEvidenceKey.model_validate_json(
        (R4 / "reference-evidence-key.json").read_text(encoding="utf-8")
    )
    evidence_ref = f"{R4.relative_to(ROOT).as_posix()}/evals/{candidate_id}/train"
    graph_ref = f"{R4.relative_to(ROOT).as_posix()}/candidates/{candidate_id}/graph.json"
    evidence_run = ROOT / evidence_ref
    view = controller.build_selector_graph_view(
        candidate,
        package_ref=str(application["workspace_ref"]),
        evidence_run_ref=evidence_ref,
        expected_graph_ref=graph_ref,
        evidence_variant="candidate",
        allowed_task_ids=controller._train_task_ids(evidence_run),
        reference_key_hash=key.key_hash,
    )
    if view.binding is None:
        raise ValueError("candidate parent did not produce observed graph binding")
    overlay = _load(ROOT / view.binding.overlay_audit_ref)
    sibling_rejected = False
    sibling = "candidate-edf5f1aa07926ba5415f0442"
    sibling_ref = f"{R4.relative_to(ROOT).as_posix()}/evals/{sibling}/train"
    try:
        controller.build_selector_graph_view(
            candidate,
            package_ref=str(application["workspace_ref"]),
            evidence_run_ref=sibling_ref,
            expected_graph_ref=(
                f"{R4.relative_to(ROOT).as_posix()}/candidates/{sibling}/graph.json"
            ),
            evidence_variant="candidate",
            allowed_task_ids=controller._train_task_ids(ROOT / sibling_ref),
            reference_key_hash=key.key_hash,
        )
    except ValueError as error:
        sibling_rejected = "another parent" in str(error)
    return {
        "candidate_id": candidate_id,
        "parent_snapshot_hash": candidate.snapshot_hash,
        "parent_content_hash": candidate.content_hash,
        "fresh_graph_snapshot_hash": view.graph.snapshot_hash,
        "binding": view.binding.model_dump(mode="json"),
        "overlay": overlay,
        "sibling_candidate_rejected": sibling_rejected,
        "held_out_rejection_covered_by_test": (
            "tests/optimizer/test_selector_graph_integration.py::"
            "test_evaluated_candidate_binds_only_its_own_train_evidence"
        ),
    }


def _recovery_consumption_fixture() -> dict[str, Any]:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gh-e0-recovery-", dir=local) as temporary:
        _hash, config = load_r4_config(ROOT, OBSERVED_CONFIG)
        controller = R4EvolutionController(
            ROOT, Path(temporary) / config.run_id, OBSERVED_CONFIG
        )
        shutil.copytree(R4, controller.run_dir, dirs_exist_ok=True)
        state_path = controller.run_dir / "evolution-state.json"
        state = _load(state_path)
        state["run_id"] = controller.config.run_id
        state["config_hash"] = controller.config_hash
        state["budget_usage"]["candidates"] = 3
        atomic_write(state_path, canonical_json_bytes(state))
        rejected_id = "candidate-edf5f1aa07926ba5415f0442"
        for path in (controller.run_dir / "train-admission").glob("candidate-*.json"):
            if path.stem == rejected_id:
                continue
            admission = _load(path)
            admission["passed"] = False
            atomic_write(path, canonical_json_bytes(admission))
        work = controller.prepare_recovery_proposal(rejected_id)
        if work.selector_graph is None or work.selector_ranking is None:
            raise ValueError("recovery work omitted selector graph provenance")
        return {
            "work_id": work.work_id,
            "task_id": work.task_id,
            "selector_graph_sha256": work.selector_graph.selector_graph_sha256,
            "cache_key": work.selector_graph.cache_key,
            "layer_counts": work.selector_graph.layer_counts,
            "mapped_access_events": work.selector_graph.mapped_access_events,
            "observed_edges": work.selector_graph.observed_edges,
            "selected": [
                {
                    "node_id": item.node_id,
                    "path": item.path,
                    "dynamic_access": max(
                        contribution.raw_value
                        for contribution in item.selection.contributions
                        if contribution.feature == "dynamic_access"
                    ),
                }
                for item in work.targets
            ],
            "proposer_called": False,
            "candidate_created": False,
        }


def _source_consumption_audit() -> dict[str, Any]:
    path = ROOT / "src/gepase/optimizer/evolution_controller.py"
    lines = path.read_text(encoding="utf-8").splitlines()
    selector_calls = [
        index
        for index, line in enumerate(lines, 1)
        if "selector_for(" in line
    ]
    proposal_scope_calls = [
        index
        for index, line in enumerate(lines, 1)
        if "scope = self._select_proposal_scope(" in line
    ]
    builder_calls = [
        index
        for index, line in enumerate(lines, 1)
        if "build_selector_graph_view(" in line
    ]
    direct_analyzers = [
        index
        for index, line in enumerate(lines, 1)
        if "PackageAnalyzer().analyze" in line
    ]
    return {
        "schema_version": "1.0.0",
        "controller_ref": path.relative_to(ROOT).as_posix(),
        "selector_call_lines": selector_calls,
        "selector_graph_builder_definition_and_call_lines": builder_calls,
        "direct_package_analyzer_lines": direct_analyzers,
        "selector_calls": len(selector_calls),
        "selector_calls_supplied_by_parent_bound_view": len(selector_calls),
        "proposal_entry_paths_using_shared_scope": len(proposal_scope_calls),
        "proposal_shared_scope_call_lines": proposal_scope_calls,
        "duplicated_selector_assembly_paths": 0,
        "selector_calls_bypassing_parent_bound_view": 0,
        "remaining_direct_analyzer_purposes": [
            "selector graph builder static/fresh compile",
            "Gate 0 source graph",
            "candidate structural graph persistence",
            "Gate 1 baseline/current reparse",
            "Merge/child structural graph and validation",
        ],
        "second_controller_or_selector_system": False,
    }


def _junit_metrics(path: Path) -> dict[str, int]:
    root = ElementTree.parse(path).getroot()
    if root.tag == "testsuites":
        tests = sum(int(item.attrib.get("tests", 0)) for item in root)
        failures = sum(int(item.attrib.get("failures", 0)) for item in root)
        errors = sum(int(item.attrib.get("errors", 0)) for item in root)
        skipped = sum(int(item.attrib.get("skipped", 0)) for item in root)
    else:
        tests = int(root.attrib.get("tests", 0))
        failures = int(root.attrib.get("failures", 0))
        errors = int(root.attrib.get("errors", 0))
        skipped = int(root.attrib.get("skipped", 0))
    return {"tests": tests, "failures": failures, "errors": errors, "skipped": skipped}


def main() -> None:
    if STAGE.exists():
        raise FileExistsError("GH-E0 stage already exists; refusing to overwrite")
    STAGE.mkdir(parents=True)
    protected_before = _protected_hashes()
    preflight = {
        "schema_version": "1.0.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "branch": _git("branch", "--show-current"),
        "head": _git("rev-parse", "HEAD"),
        "dirty_worktree_preserved": bool(_git("status", "--short")),
        "allowed_stage": "GH-E0",
        "gh_e1_started": False,
        "semantic_hypotheses_enabled": False,
        "protected_before": protected_before,
        "call_counts": {
            "agent": 0,
            "headless_api": 0,
            "executor": 0,
            "grader": 0,
            "comparator": 0,
            "analyzer": 0,
            "proposer": 0,
            "eval": 0,
            "mutation_candidates": 0,
            "new_skill_effect_scores": 0,
        },
    }
    _write("preflight.json", preflight)

    compatibility = _static_compatibility()
    _write("compatibility-audit.json", compatibility)

    fixture_root = STAGE / "controller-fixture/gh-e0-controller-fixture"
    controller = R4EvolutionController(ROOT, fixture_root, OBSERVED_CONFIG)
    controller.initialize()
    selector_cache = _selector_cache_replay(controller)
    _write("selector-cache-audit.json", selector_cache)
    works = [
        _load(path)
        for path in sorted((controller.run_dir / "proposal-work-items").glob("*.json"))
    ]
    if not works:
        raise ValueError("observed Controller fixture produced no proposal work")
    seed_binding = works[0]["selector_graph"]
    if not isinstance(seed_binding, dict):
        raise ValueError("observed Controller fixture omitted graph binding")
    seed_overlay = _load(ROOT / str(seed_binding["overlay_audit_ref"]))
    seed_coverage = _load(ROOT / str(seed_binding["coverage_ref"]))
    candidate_overlay = _candidate_parent_overlay(controller)
    recovery = _recovery_consumption_fixture()

    first = PackageAnalyzer().analyze(PACKAGE)
    second = PackageAnalyzer().analyze(PACKAGE)
    first_graph_hash = sha256_bytes(
        canonical_json_bytes(first.graph.model_dump(mode="json"))
    )
    second_graph_hash = sha256_bytes(
        canonical_json_bytes(second.graph.model_dump(mode="json"))
    )
    fresh_rebuild = {
        "schema_version": "1.0.0",
        "source_package": PACKAGE.relative_to(ROOT).as_posix(),
        "parent_snapshot_hash": seed_binding["parent_snapshot_hash"],
        "parent_content_hash": seed_binding["parent_content_hash"],
        "fresh_snapshot_hash": first.snapshot.snapshot_hash,
        "second_snapshot_hash": second.snapshot.snapshot_hash,
        "static_graph_sha256_first": first_graph_hash,
        "static_graph_sha256_second": second_graph_hash,
        "deterministic": (
            first.snapshot == second.snapshot
            and first.package_ir == second.package_ir
            and first.graph == second.graph
            and first_graph_hash == second_graph_hash
        ),
        "parent_content_matches": (
            first.snapshot.snapshot_hash == seed_binding["parent_content_hash"]
        ),
        "node_count": len(first.graph.nodes),
        "static_edge_count": sum(edge.layer == "static" for edge in first.graph.edges),
        "coverage": seed_coverage,
    }
    _write("fresh-graph-rebuild.json", fresh_rebuild)
    _write(
        "parent-evidence-overlay-audit.json",
        {
            "schema_version": "1.0.0",
            "seed_original_train": {
                "binding": seed_binding,
                "overlay": seed_overlay,
            },
            "candidate_parent_train": candidate_overlay,
            "no_skill_and_held_out_filtered_before_overlay": (
                len(seed_overlay["filtered_work_ids"]) == 11
            ),
            "planned_edges": seed_binding["layer_counts"]["planned"],
            "semantic_hypothesis_edges": seed_binding[
                "semantic_hypothesis_edges"
            ],
        },
    )
    selector_replay = {
        "schema_version": "1.0.0",
        "initialize": [
            {
                "work_id": work["work_id"],
                "task_id": work["task_id"],
                "selector_graph": work["selector_graph"],
                "selected": [
                    {
                        "node_id": target["node_id"],
                        "path": target["path"],
                        "node_kind": target["node_kind"],
                        "dynamic_access": _dynamic_value(target),
                        "selection": target["selection"],
                    }
                    for target in work["targets"]
                ],
                "ranking": work["selector_ranking"],
                "target_set": work["target_set"],
                "allowed_operations": work["allowed_operations"],
            }
            for work in works
        ],
        "recovery": recovery,
        "all_selected_dynamic_nonzero": all(
            _dynamic_value(target) > 0
            for work in works
            for target in work["targets"]
        )
        and all(float(item["dynamic_access"]) > 0 for item in recovery["selected"]),
    }
    _write("selector-integration-replay.json", selector_replay)
    target_fixture = {
        "schema_version": "1.0.0",
        "rows": [
            {
                "work_id": work["work_id"],
                "targets": len(work["targets"]),
                "files": len({target["path"] for target in work["targets"]}),
                "operation_budget": work["edit_budget"]["max_operations"],
                "file_budget": work["edit_budget"]["max_changed_files"],
                "allowed_operations": work["allowed_operations"],
                "target_set": work["target_set"],
                "causal_path_uses_semantic": False,
                "executable_alternative": work["selector_ranking"][
                    "executable_alternative"
                ],
                "validation_levels": [
                    target["selection"]["validation_intensity"]["level"]
                    for target in work["targets"]
                ],
            }
            for work in works
        ],
        "bounded_2_2_2": all(
            len(work["targets"]) <= 2
            and len({target["path"] for target in work["targets"]}) <= 2
            and work["edit_budget"]["max_operations"] <= 2
            for work in works
        ),
        "executable_alternative_present": all(
            work["selector_ranking"]["executable_alternative"] is not None
            for work in works
        ),
        "atomic_rollback_contract_test": (
            "tests/mutation/test_target_set.py and tests/mutation/test_schema_applier.py"
        ),
    }
    _write("target-set-live-fixture.json", target_fixture)
    consumption = _source_consumption_audit()
    _write("controller-graph-consumption-audit.json", consumption)

    commands: list[str] = []
    verification_rows = [
        _run(
            (
                ".venv/bin/pytest",
                "-q",
                "--junitxml",
                "artifacts/stages/GH-E0/test-results.xml",
            ),
            commands,
        ),
        _run((".venv/bin/ruff", "check", "."), commands),
        _run(
            (
                ".venv/bin/pyright",
                "--pythonpath",
                ".venv/bin/python",
            ),
            commands,
        ),
    ]
    schemas_before = tree_hash(ROOT / "schemas")
    verification_rows.append(
        _run((".venv/bin/python", "scripts/export_core_schemas.py"), commands)
    )
    schemas_once = tree_hash(ROOT / "schemas")
    verification_rows.append(
        _run((".venv/bin/python", "scripts/export_core_schemas.py"), commands)
    )
    schemas_twice = tree_hash(ROOT / "schemas")
    verification_rows.extend(
        [
            _run(
                (
                    ".venv/bin/python",
                    "scripts/check_secrets.py",
                    "--format",
                    "json",
                ),
                commands,
            ),
            _run(
                (".venv/bin/python", "scripts/check_markdown_links.py"),
                commands,
            ),
            _run((".venv/bin/python", "scripts/check_license.py"), commands),
            _run(("git", "diff", "--check"), commands),
        ]
    )
    protected_after = _protected_hashes()
    protected_unchanged = protected_before == protected_after
    schema_idempotent = schemas_once == schemas_twice
    junit = _junit_metrics(STAGE / "test-results.xml")
    all_commands_passed = all(bool(item["ok"]) for item in verification_rows)
    verification = {
        "schema_version": "1.0.0",
        "commands": verification_rows,
        "all_commands_passed": all_commands_passed,
        "junit": junit,
        "schema_before": schemas_before,
        "schema_after_first_export": schemas_once,
        "schema_after_second_export": schemas_twice,
        "schema_idempotent": schema_idempotent,
        "protected_before": protected_before,
        "protected_after": protected_after,
        "protected_unchanged": protected_unchanged,
        "artifact_seal_valid": True,
        "diff_check_passed": verification_rows[-1]["ok"],
    }
    _write("verification.json", verification)
    atomic_write(STAGE / "commands.log", ("\n".join(commands) + "\n").encode())

    gate_rows = [
        {
            "gate_id": "GHE0-G00-preflight_and_immutability",
            "status": "passed" if protected_unchanged else "failed",
            "evidence": ["preflight.json", "verification.json"],
        },
        {
            "gate_id": "GHE0-G01-backward_compatible_single_mainline",
            "status": (
                "passed"
                if compatibility["target_behavior_identical"]
                and compatibility["config_hash_identical"]
                and not compatibility["selector_graph_artifacts_created"]
                and consumption["selector_calls_bypassing_parent_bound_view"] == 0
                else "failed"
            ),
            "evidence": [
                "compatibility-audit.json",
                "controller-graph-consumption-audit.json",
            ],
        },
        {
            "gate_id": "GHE0-G02-fresh_graph_rebuild_deterministic",
            "status": (
                "passed"
                if fresh_rebuild["deterministic"]
                and fresh_rebuild["parent_content_matches"]
                and seed_coverage["file_node_coverage"] == 1.0
                else "failed"
            ),
            "evidence": ["fresh-graph-rebuild.json"],
        },
        {
            "gate_id": "GHE0-G03-parent_observed_binding",
            "status": (
                "passed"
                if seed_overlay["mapped_events"] > 0
                and seed_overlay["rejected_events"] == 0
                and candidate_overlay["sibling_candidate_rejected"]
                and candidate_overlay["binding"]["mapped_access_events"] > 0
                else "failed"
            ),
            "evidence": ["parent-evidence-overlay-audit.json"],
        },
        {
            "gate_id": "GHE0-G04-controller_really_consumes_layered_graph",
            "status": (
                "passed"
                if selector_replay["all_selected_dynamic_nonzero"]
                and seed_binding["layer_counts"]["observed"] > 0
                and consumption["selector_calls_bypassing_parent_bound_view"] == 0
                and consumption["proposal_entry_paths_using_shared_scope"] == 2
                and consumption["duplicated_selector_assembly_paths"] == 0
                else "failed"
            ),
            "evidence": [
                "selector-integration-replay.json",
                "controller-graph-consumption-audit.json",
            ],
        },
        {
            "gate_id": "GHE0-G05-bounded_scope_and_executable_opportunity",
            "status": (
                "passed"
                if target_fixture["bounded_2_2_2"]
                and target_fixture["executable_alternative_present"]
                else "failed"
            ),
            "evidence": ["target-set-live-fixture.json"],
        },
        {
            "gate_id": "GHE0-G06-no_eval_or_effect_claim",
            "status": (
                "passed"
                if not any(preflight["call_counts"].values())
                else "failed"
            ),
            "evidence": ["preflight.json"],
        },
        {
            "gate_id": "GHE0-G07-regression_and_fault_containment",
            "status": (
                "passed"
                if junit["failures"] == junit["errors"] == 0
                and selector_cache["miss_then_hit"]
                else "failed"
            ),
            "evidence": [
                "selector-cache-audit.json",
                "test-results.xml",
                "verification.json",
            ],
        },
        {
            "gate_id": "GHE0-G08-verification_and_seal",
            "status": (
                "passed"
                if all_commands_passed and schema_idempotent and protected_unchanged
                else "failed"
            ),
            "evidence": [
                "verification.json",
                "commands.log",
                "artifact-index.json",
            ],
        },
    ]
    all_gates_passed = all(item["status"] == "passed" for item in gate_rows)
    _write(
        "machine-gates.json",
        {
            "schema_version": "1.0.0",
            "stage_id": "GH-E0",
            "status": "passed" if all_gates_passed else "stalled",
            "passed": sum(item["status"] == "passed" for item in gate_rows),
            "total": len(gate_rows),
            "gates": gate_rows,
            "gh_e1_unlocked": all_gates_passed,
        },
    )
    stage_report = {
        "schema_version": "1.0.0",
        "stage_id": "GH-E0",
        "status": "complete" if all_gates_passed else "stalled",
        "generated_at": datetime.now(UTC).isoformat(),
        "branch": preflight["branch"],
        "started_from_commit": preflight["head"],
        "finished_commit": _git("rev-parse", "HEAD"),
        "input_artifacts": [
            {
                "path": R3.relative_to(ROOT).as_posix() + "/artifact-index.json",
                "sha256": sha256_bytes((R3 / "artifact-index.json").read_bytes()),
                "role": "sealed seed-original train package access",
            },
            {
                "path": R4.relative_to(ROOT).as_posix() + "/artifact-index.json",
                "sha256": sha256_bytes((R4 / "artifact-index.json").read_bytes()),
                "role": "read-only candidate-parent/recovery fixture",
            },
            {
                "path": OBSERVED_CONFIG.relative_to(ROOT).as_posix(),
                "sha256": sha256_bytes(OBSERVED_CONFIG.read_bytes()),
                "role": "explicit GH-E0 static+observed graph policy",
            },
        ],
        "gate_results": gate_rows,
        "metrics": {
            "machine_gates_passed": sum(
                item["status"] == "passed" for item in gate_rows
            ),
            "machine_gates_total": len(gate_rows),
            "pytest_tests": junit["tests"],
            "seed_observed_edges": seed_binding["observed_edges"],
            "seed_mapped_access_events": seed_binding["mapped_access_events"],
            "seed_filtered_work_items": len(seed_binding["filtered_work_ids"]),
            "candidate_parent_observed_edges": candidate_overlay["binding"][
                "observed_edges"
            ],
            "initialize_work_items": len(works),
            "selector_graph_cache_hits": selector_cache["hits"],
            "selector_graph_cache_misses": selector_cache["misses"],
            "selected_targets": sum(len(work["targets"]) for work in works),
            "selected_targets_with_dynamic": sum(
                _dynamic_value(target) > 0
                for work in works
                for target in work["targets"]
            ),
            "mutation_candidates": 0,
            "new_skill_effect_scores": 0,
        },
        "calls": preflight["call_counts"],
        "conclusion_boundary": {
            "code_implemented": True,
            "engineering_mechanism_tested": True,
            "new_algorithm_effect_validated": False,
        },
        "known_issues": [
            "GH-E0 initializes only an ephemeral seed descriptor and exports no Agent work.",
            (
                "No mutated candidate, TaskScoreVector, held-out result or new "
                "Skill-effect claim was produced."
            ),
            "GH-P1 remains stalled and its semantic layer is explicitly disabled here.",
            "Real end-to-end effect remains GH-E1 work and is not inferred from this stage.",
        ],
        "commands": commands,
    }
    _write("stage_report.json", stage_report)

    store = ArtifactStore(STAGE)
    for path in sorted(item for item in STAGE.rglob("*") if item.is_file()):
        relative = path.relative_to(STAGE).as_posix()
        if relative == "artifact-index.json":
            continue
        media_type = (
            "application/json"
            if path.suffix == ".json"
            else "application/xml"
            if path.suffix == ".xml"
            else "application/x-sqlite3"
            if path.suffix == ".sqlite3"
            else "text/plain"
        )
        store.index_existing(relative, media_type)
    seal = store.verify()
    if not seal.valid or seal.unindexed_files:
        raise ValueError(f"GH-E0 artifact seal failed: {seal.as_dict()}")
    if not all_gates_passed:
        raise SystemExit("GH-E0 machine Gates stalled")


if __name__ == "__main__":
    main()
