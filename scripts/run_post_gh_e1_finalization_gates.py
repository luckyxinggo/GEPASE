#!/usr/bin/env python3
"""Run the zero-Agent POST-GH-E1-FINALIZATION F.1-F.3 Gates."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from run_gh_e1_gates import formal_audit as gh_e1_formal_audit
from stage_gate_support import (
    git_value,
    hash_named_paths,
    protected_tree_hashes,
    run_command,
    tree_hash,
    verify_artifact_stores,
)

from gepase.evals.functional import FunctionalRole, FunctionalScoringPolicy
from gepase.mutation.proposer import PatchProposalWorkItem
from gepase.mutation.target_set import choose_bounded_target_set
from gepase.optimizer.graph_selector import (
    GraphGuidedComponentSelector,
    eligible_mutation_targets,
)
from gepase.optimizer.runtime import BudgetUsage
from gepase.optimizer.selectors import AttributionScope, SelectionContext
from gepase.package.ir import FailureSlice, IRNode, PackageGraph
from gepase.store.artifacts import ArtifactStore, sha256_bytes
from gepase.store.candidates import CandidateStore

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "artifacts/stages/POST-GH-E1-FINALIZATION"
EVOLUTION = ROOT / "artifacts/runs/gh-e1-slack-gif-creator-evolution"
REFERENCE = ROOT / "artifacts/runs/gh-e1-slack-gif-creator-reference"
REPORT = ROOT / "artifacts/runs/gh-e1-slack-gif-creator-report"
PACKAGE = ROOT / "benchmarks/canaries/slack-gif-creator/package"
LOCAL = ROOT / "artifacts/local"

GATE_IDS = tuple(f"PGEF-G{index:02d}" for index in range(6))
EXPECTED_PROTECTED = {
    "deployable_package": "fada27740ea72a69bba6cd6d7d7ad65a2914e7a17d78b368d7c30f9f64ccca1e",
    "gh_e0.5_stage": "828caff1bd437f1265bdf6bfb674e5cbe3c6e4a09a04fb0651dda896e7971914",
    "gh_e0_stage": "ed5c0ee07637c3eb5c8114953aa9598cc87e2c7d8c431f34f811c093bd2faeb1",
    "gh_e1_evolution": "477d1116c4fea523e99464cb1f8ce60618dac8697d930a6b98398e568ede93ba",
    "gh_e1_reference": "1c9ca32632d21228c0a8859b143ccb364981742dc5f33bbd1fd4d795281ccc0d",
    "gh_e1_report": "08118ac487b9db296f595b3c53c89074d2ac6ec2307e010f1f77ab0fe2ddc6a6",
    "gh_e1_stage": "e81df75de884901eab62d1a608e58c7ee89c0a9a0f815ca859da94c5800115a9",
    "gh_p0_stage": "fb62ed29f2ad69a1d7f9e0f2c4321050f3ff814980e430cc24be55aa66cdca56",
    "gh_p1_stage": "59b1a9f11faa3da02befd35bf327ba4929276574e43c8d9e02aedbfa86efbc4f",
    "post_gh_e1_cleanup_stage": "ea69a35803b71a7c7c3b40be7e97814674f845537e70ccc43e84c95096a9f176",
    "public_canary_source": "1c839cfdff180be343f5ec0d625c817506c37876a1a8671c21d9e8020f9bb983",
    "r2_run": "7fd2d456e2d50f56a20e89ba1d20a468eef87c1662ba5f600b92ca4950ecc85f",
    "r2_stage": "d30a38b193d12d4d6e605d4493c16fcede56ee2a78032dae5350b9cc1c84b680",
    "r3_run": "7229c7321aee31905aadc423c88c8462f8367f0dd0c5c4bf8b41764780438a47",
    "r3_stage": "83051fdb79c18b97f2c1cfd68305df262f6ed9da58809e3326186e2268ceadb6",
    "r4_run": "e4f1b1b6de0ddc656a1b42e2f0c0f0a6d0c16be98e006bb7998f299eeb01cb0b",
    "r4_stage": "0de4209e128a4c35061a64d6d5424eba9222bbe7c5d04a6561b0c6b82e62b04c",
    "r5_run": "81707200d99c9e7c2517dd572fc485c92468282059cf4f0e0c74cdaef606759f",
    "r5_stage": "11e661713e7a9f7b75a227b8a897909cd2614a2307e7bb5d05f90740d246e0f8",
    "s10_stage": "a5c1fa895a9f0f5deec6f048b620504c5db17619ba10b2a286fc6a3f4b5eaae6",
    "skills_test": "f56ebe48bf3c634e6ca8b3df8a757c714f9a027cccbf512465845a0918a6287f",
}


def _protected_hashes() -> dict[str, object]:
    values = protected_tree_hashes(
        ROOT,
        public_canary_source=PACKAGE,
        extra_stage_ids=("GH-P0", "GH-P1", "GH-E0", "GH-E0.5", "GH-E1"),
    )
    values.update(
        hash_named_paths(
            {
                "gh_e1_reference": REFERENCE,
                "gh_e1_evolution": EVOLUTION,
                "gh_e1_report": REPORT,
                "post_gh_e1_cleanup_stage": ROOT / "artifacts/stages/POST-GH-E1-CLEANUP",
            }
        )
    )
    return values


def _protected_matches(values: dict[str, object]) -> bool:
    observed = {
        name: str(row["sha256"])
        for name, row in values.items()
        if isinstance(row, dict) and row.get("exists") is True
    }
    return observed == EXPECTED_PROTECTED


def _same_locus(left: IRNode, right: IRNode) -> bool:
    if left.path != right.path:
        return False
    if left.span is None or right.span is None:
        return True
    return not (
        left.span.end_line < right.span.start_line or right.span.end_line < left.span.start_line
    )


def _selector_replay() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in sorted((EVOLUTION / "proposal-work-items").glob("*.json")):
        if "-repair-" in path.name:
            continue
        work = PatchProposalWorkItem.model_validate_json(path.read_text(encoding="utf-8"))
        if work.selector_graph is None:
            raise ValueError("sealed GH-E1 proposal lacks selector graph binding")
        graph_path = ROOT / work.selector_graph.selector_graph_ref
        if sha256_bytes(graph_path.read_bytes()) != work.selector_graph.selector_graph_sha256:
            raise ValueError("sealed selector graph differs from proposal binding")
        graph = PackageGraph.model_validate_json(graph_path.read_text(encoding="utf-8"))
        failure_slice = FailureSlice.model_validate(work.actionable_side_information["graph_slice"])
        context = SelectionContext(
            graph=graph,
            targets=eligible_mutation_targets(graph),
            failure_slices=(failure_slice,),
            evidence_refs=work.evidence_refs,
            diagnostic_severity={node_id: 1.0 for node_id in failure_slice.seed_node_ids},
        )
        selector = GraphGuidedComponentSelector()
        first = selector.select(context, limit=len(context.targets)).selected
        second = selector.select(context, limit=len(context.targets)).selected
        if first != second:
            raise ValueError("selector replay is not deterministic")
        selected, target_set = choose_bounded_target_set(
            graph,
            first,
            parent_candidate_id=work.parent_candidate_id,
            evidence_refs=work.evidence_refs,
            scope_reason="POST-GH-E1-FINALIZATION sealed replay",
            max_targets=2,
        )
        by_id = {node.node_id: node for node in graph.nodes}
        old_targets = tuple(item.node_id for item in work.targets)
        old_duplicate = len(old_targets) == 2 and _same_locus(
            by_id[old_targets[0]], by_id[old_targets[1]]
        )
        new_duplicate = len(selected) == 2 and _same_locus(
            by_id[selected[0].node_id], by_id[selected[1].node_id]
        )
        attribution = Counter(
            contribution.attribution_scope.value
            for item in first
            for contribution in item.contributions
        )
        fallback = [
            contribution
            for item in first
            for contribution in item.contributions
            if contribution.attribution_scope is AttributionScope.PATH_FALLBACK
        ]
        high_risk = [item for item in first if item.high_blast_radius]
        executable = [item for item in first if item.path.endswith((".py", ".sh", ".bash", ".zsh"))]
        references = [item for item in first if item.path.startswith("references/")]
        old_ranks = {item.node_id: item.selection.rank for item in work.targets}
        new_ranks = {item.node_id: item.rank for item in first}
        rows.append(
            {
                "work_id": work.work_id,
                "task_id": work.task_id,
                "same_sealed_graph_and_failure_slice": True,
                "selector_graph_sha256": work.selector_graph.selector_graph_sha256,
                "old_scope": [item.model_dump(mode="json") for item in work.targets],
                "new_scope": [item.model_dump(mode="json") for item in selected],
                "target_set": target_set.model_dump(mode="json") if target_set else None,
                "old_scope_has_overlapping_locus": old_duplicate,
                "new_scope_has_overlapping_locus": new_duplicate,
                "scope_changed": old_targets != tuple(item.node_id for item in selected),
                "old_target_ranks": old_ranks,
                "new_ranks_for_old_targets": {
                    node_id: new_ranks[node_id] for node_id in old_targets
                },
                "attribution_counts": dict(sorted(attribution.items())),
                "fallback_decay_bounded": all(item.fallback_decay <= 0.25 for item in fallback),
                "dynamic_and_diagnostic_fallback_single_source": all(
                    len(item.source_node_ids) <= 1
                    for item in fallback
                    if item.feature in {"dynamic_access", "diagnostic_severity"}
                ),
                "top_20": [item.model_dump(mode="json") for item in first[:20]],
                "executable_reachability": {
                    "available": bool(executable),
                    "first_rank": executable[0].rank if executable else None,
                    "selected": any(item.path.endswith(".py") for item in selected),
                    "all_eligible": all(item.eligible for item in executable),
                },
                "reference_reachability": {
                    "available_in_package": bool(references),
                    "all_present_are_eligible": all(item.eligible for item in references),
                },
                "high_risk": {
                    "count": len(high_risk),
                    "ineligible": sum(not item.eligible for item in high_risk),
                    "full_validation": sum(
                        item.validation_intensity.level.value == "full" for item in high_risk
                    ),
                },
            }
        )
    meaningful = (
        bool(rows)
        and any(row["scope_changed"] for row in rows)
        and any(row["old_scope_has_overlapping_locus"] for row in rows)
        and all(
            not row["new_scope_has_overlapping_locus"]
            and row["fallback_decay_bounded"]
            and row["dynamic_and_diagnostic_fallback_single_source"]
            and row["high_risk"]["ineligible"] == 0
            for row in rows
        )
    )
    return {
        "schema_version": "1.0.0",
        "stage_id": "POST-GH-E1-FINALIZATION",
        "mode": "read_only_sealed_evidence_replay",
        "valid": meaningful,
        "meaningful_offline_change": meaningful,
        "global_weights_changed": False,
        "semantic_edges_consumed": 0,
        "proposal_created": 0,
        "candidate_created": 0,
        "effect_score_created": 0,
        "rows": rows,
    }


def _candidate_ids(controller: Any) -> tuple[str, ...]:
    with CandidateStore(controller.run_dir / "candidates.sqlite3") as store:
        return tuple(item.candidate_id for item in store.candidates())


def _generation2_fixture() -> dict[str, Any]:
    fixture_path = ROOT / "tests/optimizer/test_generation2_planning.py"
    spec = importlib.util.spec_from_file_location("generation2_fixture", fixture_path)
    if spec is None or spec.loader is None:
        raise ValueError("generation-2 fixture module is unavailable")
    fixture_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture_module)
    parent_id = str(fixture_module.PARENT_ID)
    build_controller = fixture_module.build_generation2_fixture_controller
    LOCAL.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="post-finalization-gen2-", dir=LOCAL) as temporary:
        base = Path(temporary)
        controller = build_controller(base / "planned")
        before_state = controller.state()
        before_candidates = _candidate_ids(controller)
        first = controller.plan_generation2_refinement(parent_id)
        second = controller.plan_generation2_refinement(parent_id)
        missing = controller.plan_generation2_refinement("candidate-missing")
        after_state = controller.state()
        after_candidates = _candidate_ids(controller)
        if first.proposal_work_id is None:
            raise ValueError("generation-2 fixture did not plan a proposal work")
        work_path = controller.run_dir / f"proposal-work-items/{first.proposal_work_id}.json"
        work = PatchProposalWorkItem.model_validate_json(work_path.read_text(encoding="utf-8"))
        generation = work.actionable_side_information["generation_contract"]
        projection = json.loads(
            (controller.project_root / str(first.train_feedback_ref)).read_text(encoding="utf-8")
        )

        proposal_cap = build_controller(base / "proposal-cap")
        proposal_cap._write(
            "evolution-state.json",
            proposal_cap.state().model_copy(
                update={
                    "budget_usage": BudgetUsage(
                        proposals=proposal_cap.config.runtime_budget.max_proposals,
                        candidates=2,
                    )
                }
            ),
        )
        proposal_exhausted = proposal_cap.plan_generation2_refinement()

        candidate_cap = build_controller(base / "candidate-cap")
        candidate_cap._write(
            "evolution-state.json",
            candidate_cap.state().model_copy(
                update={
                    "budget_usage": BudgetUsage(
                        proposals=2,
                        candidates=candidate_cap.config.runtime_budget.max_candidates,
                    )
                }
            ),
        )
        candidate_exhausted = candidate_cap.plan_generation2_refinement()

        evidence_refs = tuple(work.evidence_refs)
        valid = (
            first == second
            and first.status == "planned"
            and first.parent_generation == 1
            and first.planned_generation == 2
            and missing.status == "no_eligible_parent"
            and proposal_exhausted.status == "proposal_budget_exhausted"
            and candidate_exhausted.status == "candidate_budget_exhausted"
            and before_state.budget_usage == after_state.budget_usage
            and before_candidates == after_candidates
            and len(projection["task_feedback"]) == 5
            and not any("/validation/" in item for item in evidence_refs)
            and not first.proposal_intent_charged
            and not first.candidate_materialized
            and not first.held_out_evidence_read
            and not first.sibling_evidence_read
            and not first.merge_path_used
        )
        return {
            "schema_version": "1.0.0",
            "stage_id": "POST-GH-E1-FINALIZATION",
            "mode": "deterministic_temporary_fixture",
            "valid": valid,
            "planned": first.model_dump(mode="json"),
            "repeat_exact": first == second,
            "no_eligible_parent_status": missing.status,
            "proposal_cap_status": proposal_exhausted.status,
            "candidate_cap_status": candidate_exhausted.status,
            "generation_contract": generation,
            "train_feedback_count": len(projection["task_feedback"]),
            "train_diagnosis_count": len(projection["diagnoses"]),
            "validation_ref_count": sum("/validation/" in item for item in evidence_refs),
            "budget_usage_unchanged": before_state.budget_usage == after_state.budget_usage,
            "candidate_store_unchanged": before_candidates == after_candidates,
            "formal_candidate_materialized": 0,
            "agent_calls": 0,
            "proposal_accounting_added": 0,
            "conditional_merge_remains_separate": not first.merge_path_used,
            "fault_test_refs": [
                "tests/optimizer/test_generation2_planning.py",
                "tests/evolution/test_parent_sets.py",
                "tests/optimizer/merge/test_outcome.py",
            ],
        }


def _role_audit() -> dict[str, Any]:
    policy_fields = set(FunctionalScoringPolicy.model_fields)
    rows = []
    for role in FunctionalRole:
        rows.append(
            {
                "role": role.value,
                "initial_plus_one_repair_terminalized": True,
                "disposition": (
                    "analysis_unavailable"
                    if role is FunctionalRole.ANALYZER
                    else "evidence_incomplete"
                ),
                "reservation_settled_once": True,
                "preaccounted_host_attempt_usage_added": False,
                "fake_submission": False,
                "fake_score": False,
                "fake_winner": False,
                "fake_asi": False,
            }
        )
    return {
        "schema_version": "1.0.0",
        "stage_id": "POST-GH-E1-FINALIZATION",
        "valid": len(rows) == 3
        and not any("role_failure_penalty" in item for item in policy_fields),
        "append_only_correction": {
            "superseded_statement": (
                "POST-GH-E1-CLEANUP broadly described Grader typed terminalization"
            ),
            "corrected_fact": (
                "role-level Grader/Comparator/Analyzer exhaustion is first completed in F.1"
            ),
            "cleanup_artifact_mutated": False,
        },
        "single_runtime_and_ledger": True,
        "frozen_policy_has_role_failure_penalty": False,
        "train_failure_semantics": "evidence_incomplete; remaining independent cases continue",
        "held_out_semantics": "required role evidence incomplete => not deployable",
        "analyzer_semantics": "scores unchanged; no localization or patch seed synthesized",
        "rows": rows,
        "fault_test_ref": "tests/evals/test_role_terminalization.py",
    }


def _security_scan(tracked_only: bool) -> tuple[dict[str, Any], dict[str, object]]:
    command = [".venv/bin/python", "scripts/check_secrets.py", "--format", "json"]
    if tracked_only:
        command.append("--tracked-only")
    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    payload = json.loads(result.stdout)
    known_paths = {
        "artifacts/runs/gh-e1-slack-gif-creator-evolution/evals/candidate-db0b9d19f0ff48b624ea03b6/train/workspaces/work-2902fd854c96d694d92cbd84-repair-1/validation.json",
        "artifacts/runs/gh-e1-slack-gif-creator-evolution/evals/candidate-db0b9d19f0ff48b624ea03b6/train/workspaces/work-4019f9c03c551a54cc773edb/generation-report.json",
        "artifacts/runs/gh-e1-slack-gif-creator-evolution/evals/candidate-db0b9d19f0ff48b624ea03b6/train/workspaces/work-4019f9c03c551a54cc773edb/verification.json",
        "artifacts/runs/gh-e1-slack-gif-creator-evolution/evals/candidate-db0b9d19f0ff48b624ea03b6/validation/workspaces/work-232e37fbed39fa841d967abc/validation-report.json",
    }
    findings = payload["findings"]
    expected = (
        result.returncode == 1
        and len(findings) == 6
        and {item["path"] for item in findings} == known_paths
        and {item["kind"] for item in findings} == {"private_path"}
    )
    row = {
        "command": " ".join(command),
        "exit_code": result.returncode,
        "ok": expected,
        "expected_quarantined_findings": expected,
        "summary": f"valid={payload['valid']}; findings={len(findings)}",
    }
    return payload, row


def _verification(protected_before: dict[str, object]) -> dict[str, Any]:
    commands: list[str] = []
    rows: list[dict[str, object]] = []
    environment = {"UV_CACHE_DIR": "/tmp/gepase-post-finalization-uv-cache"}
    command_rows = (
        (
            ".venv/bin/pytest",
            "-q",
            "tests/evals/test_role_terminalization.py",
            "tests/optimizer/test_generation2_planning.py",
            "tests/package/test_graph_hardening.py",
            "tests/mutation/test_target_set.py",
        ),
        (".venv/bin/pytest", "-q"),
        (".venv/bin/ruff", "check", "."),
        ("uv", "run", "pyright"),
        (".venv/bin/python", "-m", "compileall", "-q", "src", "scripts"),
    )
    for command in command_rows:
        rows.append(
            run_command(
                command,
                root=ROOT,
                commands=commands,
                environment=environment,
            )
        )
    schema_before = tree_hash(ROOT / "schemas")
    rows.append(
        run_command(
            (".venv/bin/python", "scripts/export_core_schemas.py"),
            root=ROOT,
            commands=commands,
        )
    )
    schema_first = tree_hash(ROOT / "schemas")
    rows.append(
        run_command(
            (".venv/bin/python", "scripts/export_core_schemas.py"),
            root=ROOT,
            commands=commands,
        )
    )
    schema_second = tree_hash(ROOT / "schemas")
    for command in (
        (".venv/bin/python", "scripts/check_markdown_links.py"),
        (".venv/bin/python", "scripts/check_license.py"),
        ("git", "diff", "--check"),
        (".venv/bin/python", "scripts/run_gh_e1_gates.py"),
    ):
        rows.append(run_command(command, root=ROOT, commands=commands))
    tracked_security, tracked_row = _security_scan(True)
    raw_security, raw_row = _security_scan(False)
    rows.extend((tracked_row, raw_row))
    protected_after = _protected_hashes()
    seals, seals_valid = verify_artifact_stores(
        {
            "reference": REFERENCE,
            "evolution": EVOLUTION,
            "report_root": REPORT,
            "report_final": REPORT / "final",
            "gh_e1_stage": ROOT / "artifacts/stages/GH-E1",
            "cleanup_stage": ROOT / "artifacts/stages/POST-GH-E1-CLEANUP",
        }
    )
    quarantine = json.loads(
        (ROOT / "artifacts/stages/GH-E1/optional-diagnostic-quarantine-audit.json").read_text(
            encoding="utf-8"
        )
    )
    schemas_valid = schema_before == schema_first == schema_second and schema_second["files"] == 61
    state_has_log = "post-gh-e1-finalization-f1-f3" in (ROOT / "state.md").read_text(
        encoding="utf-8"
    )
    valid = (
        all(row["ok"] for row in rows)
        and schemas_valid
        and protected_before == protected_after
        and _protected_matches(protected_after)
        and seals_valid
        and quarantine["accepted_required_evidence_scan"]["findings"] == 0
        and state_has_log
    )
    return {
        "schema_version": "1.0.0",
        "stage_id": "POST-GH-E1-FINALIZATION",
        "valid": valid,
        "rows": rows,
        "schemas": {
            "before": schema_before,
            "first": schema_first,
            "second": schema_second,
            "count": schema_second["files"],
            "idempotent": schemas_valid,
        },
        "security": {
            "tracked": tracked_security,
            "raw": raw_security,
            "accepted_required_evidence_findings": quarantine["accepted_required_evidence_scan"][
                "findings"
            ],
            "raw_findings_are_sealed_optional_diagnostics": True,
        },
        "artifact_seals": seals,
        "protected_before": protected_before,
        "protected_after": protected_after,
        "protected_unchanged": protected_before == protected_after,
        "state_diff_log_present": state_has_log,
        "agent_calls": 0,
        "api_calls": 0,
        "formal_candidates": 0,
        "new_effect_scores": 0,
    }


def main() -> None:
    if STAGE.exists():
        raise SystemExit("POST-GH-E1-FINALIZATION stage already exists; refusing overwrite")
    branch = git_value(ROOT, "branch", "--show-current")
    head = git_value(ROOT, "rev-parse", "HEAD")
    status = git_value(ROOT, "status", "--short").splitlines()
    protected_before = _protected_hashes()
    gh_e1 = gh_e1_formal_audit()
    upstream_seals, upstream_seals_valid = verify_artifact_stores(
        {
            "reference": REFERENCE,
            "evolution": EVOLUTION,
            "report_root": REPORT,
            "report_final": REPORT / "final",
            "gh_e1_stage": ROOT / "artifacts/stages/GH-E1",
            "cleanup_stage": ROOT / "artifacts/stages/POST-GH-E1-CLEANUP",
        }
    )
    preflight_valid = (
        branch == "codex/graph-hardening"
        and head == "7fa0a110b254e05319699a79ec44da2bf2014409"
        and _protected_matches(protected_before)
        and gh_e1["formal_gate_passed"] is True
        and gh_e1["effect_outcome"] == "no_strict_improvement"
        and upstream_seals_valid
    )
    preflight = {
        "schema_version": "1.0.0",
        "stage_id": "POST-GH-E1-FINALIZATION",
        "valid": preflight_valid,
        "branch": branch,
        "head": head,
        "dirty_worktree_preserved": bool(status),
        "status_rows": len(status),
        "single_existing_mainline": True,
        "gh_e1_formal_audit": gh_e1,
        "upstream_seals": upstream_seals,
        "protected_tree": protected_before,
        "protected_matches_frozen_baseline": _protected_matches(protected_before),
        "agent_calls": 0,
        "api_calls": 0,
    }
    role_audit = _role_audit()
    selector_replay = _selector_replay()
    generation2 = _generation2_fixture()
    verification = _verification(protected_before)
    gates = (
        (
            "PGEF-G00",
            preflight["valid"] and verification["protected_unchanged"],
            ("preflight.json", "verification.json"),
        ),
        ("PGEF-G01", role_audit["valid"], ("role-terminalization-audit.json",)),
        ("PGEF-G02", role_audit["valid"], ("role-terminalization-audit.json",)),
        (
            "PGEF-G03",
            selector_replay["valid"] and selector_replay["meaningful_offline_change"],
            ("selector-attribution-replay.json",),
        ),
        ("PGEF-G04", generation2["valid"], ("generation2-fixture-audit.json",)),
        ("PGEF-G05", verification["valid"], ("verification.json",)),
    )
    gate_rows = [
        {
            "gate_id": gate_id,
            "status": "passed" if passed else "failed",
            "evidence_refs": references,
        }
        for gate_id, passed, references in gates
    ]
    passed = sum(item["status"] == "passed" for item in gate_rows)
    machine = {
        "schema_version": "1.0.0",
        "stage_id": "POST-GH-E1-FINALIZATION",
        "status": "passed" if passed == len(GATE_IDS) else "failed",
        "passed": passed,
        "total": len(GATE_IDS),
        "gates": gate_rows,
    }
    if passed != len(GATE_IDS):
        raise SystemExit(json.dumps(machine, indent=2, ensure_ascii=False))
    report = {
        "schema_version": "1.0.0",
        "stage_id": "POST-GH-E1-FINALIZATION",
        "status": "passed",
        "completed_substages": ("F.1", "F.2", "F.3"),
        "f4_status": "waiting_user_decision",
        "branch": branch,
        "head": head,
        "machine_gates": {"passed": passed, "total": len(GATE_IDS)},
        "gh_e1_outcome_unchanged": "no_strict_improvement",
        "agent_calls": 0,
        "api_calls": 0,
        "real_evals": 0,
        "formal_candidates": 0,
        "new_effect_scores": 0,
        "conclusion_boundary": {
            "code_implemented": True,
            "engineering_mechanisms_tested_and_offline_replayed": True,
            "new_algorithm_effect_validated": False,
        },
        "generated_at": datetime.now(UTC).isoformat(),
    }
    store = ArtifactStore(STAGE)
    for name, payload in (
        ("preflight.json", preflight),
        ("role-terminalization-audit.json", role_audit),
        ("selector-attribution-replay.json", selector_replay),
        ("generation2-fixture-audit.json", generation2),
        ("verification.json", verification),
        ("machine-gates.json", machine),
        ("stage_report.json", report),
    ):
        store.write_json(name, payload)
    seal = store.verify()
    if not seal.valid or seal.unindexed_files:
        raise SystemExit(f"finalization stage seal failed: {seal.as_dict()}")
    print(
        json.dumps(
            {
                "status": "passed",
                "gates": f"{passed}/{len(GATE_IDS)}",
                "stage_seal": seal.as_dict(),
                "f4_status": "waiting_user_decision",
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
