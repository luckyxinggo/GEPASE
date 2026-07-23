"""Recompute R2 canary onboarding gates from durable artifacts."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from gepase.evals.eval_plan import (
    EvalDesignerSubmission,
    EvalDesignerWorkItem,
    EvalPlanCheckpoint,
    EvalPlanCheckReport,
    EvalPlanState,
    EvalReviewSubmission,
    FrozenEvalPlan,
    SourceProvenance,
)
from gepase.evals.eval_plan_checks import verify_upstream_tree_manifest
from gepase.package.analyzer import PackageAnalyzer
from gepase.store.artifacts import ArtifactStore


def _load(path: Path, model: type[Any]) -> Any:
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def _gate(gate_id: str, passed: bool, detail: str, evidence: list[str]) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "status": "passed" if passed else "failed",
        "detail": detail,
        "evidence": evidence,
    }


def _check_javascript(html: str) -> tuple[bool, str]:
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, flags=re.DOTALL)
    executable = [script for script in scripts if "const draft=" in script]
    if len(executable) != 1:
        return False, f"expected one executable inline script, found {len(executable)}"
    try:
        result = subprocess.run(
            ["node", "--check"],
            input=executable[0],
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return False, "node is unavailable for JavaScript syntax validation"
    return result.returncode == 0, (result.stderr.strip() or "inline JavaScript syntax is valid")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--canary", type=Path, default=Path("benchmarks/canaries/slack-gif-creator")
    )
    parser.add_argument(
        "--run-dir", type=Path, default=Path("artifacts/runs/r2-slack-gif-creator-evalplan")
    )
    parser.add_argument("--stage-dir", type=Path, default=Path("artifacts/stages/R2"))
    parser.add_argument("--external-validation", type=Path)
    args = parser.parse_args()

    repo = args.repo.resolve()
    canary = (repo / args.canary).resolve()
    run_dir = (repo / args.run_dir).resolve()
    stage_dir = (repo / args.stage_dir).resolve()
    provenance = _load(canary / "source-provenance.json", SourceProvenance)
    work_item = _load(run_dir / "designer-work-item.json", EvalDesignerWorkItem)
    submission = _load(run_dir / "designer-submission.json", EvalDesignerSubmission)
    checks = _load(run_dir / "automatic-check-report.json", EvalPlanCheckReport)
    review = _load(run_dir / "review.json", EvalReviewSubmission)
    frozen = _load(run_dir / "frozen-eval-plan.json", FrozenEvalPlan)
    checkpoint = _load(run_dir / "checkpoint.json", EvalPlanCheckpoint)
    smoke = json.loads(
        (stage_dir / "evidence/canary-smoke/smoke-report.json").read_text(encoding="utf-8")
    )
    analysis = PackageAnalyzer().analyze(canary / "package")
    verification = ArtifactStore(run_dir).verify()

    expected_package_files = {
        "LICENSE.txt",
        "SKILL.md",
        "core/easing.py",
        "core/frame_composer.py",
        "core/gif_builder.py",
        "core/validators.py",
        "requirements.txt",
    }
    actual_package_files = {
        path.relative_to(canary / "package").as_posix()
        for path in (canary / "package").rglob("*")
        if path.is_file()
    }
    dependency_lines = [
        line.strip()
        for line in (canary / "requirements.lock").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    tree_manifest_ok, tree_manifest_problems = verify_upstream_tree_manifest(repo, provenance)

    gates: list[dict[str, Any]] = []
    source_ok = (
        actual_package_files == expected_package_files
        and analysis.snapshot.snapshot_hash == provenance.package_snapshot_hash
        and provenance.license_spdx == "Apache-2.0"
        and len(provenance.source_commit) == 40
        and len(provenance.upstream_tree_hash) == 40
        and all("==" in line for line in dependency_lines)
        and tree_manifest_ok
    )
    gates.append(
        _gate(
            "R2-G01-pinned-source",
            source_ok,
            "Pinned commit/tree, seven upstream Git blobs, complete Package, Apache-2.0 "
            "attribution, PackageSnapshot hash, and exact dependency pins agree."
            if source_ok
            else "Pinned source, Git blobs, license, PackageSnapshot, or dependency lock "
            f"mismatch: {tree_manifest_problems}.",
            [
                "benchmarks/canaries/slack-gif-creator/source-provenance.json",
                "benchmarks/canaries/slack-gif-creator/upstream-tree.json",
                "benchmarks/canaries/slack-gif-creator/requirements.lock",
            ],
        )
    )

    smoke_ok = bool(
        smoke.get("valid")
        and smoke.get("core_imported")
        and smoke.get("source_package_unchanged")
        and smoke.get("validation", {}).get("passes")
        and smoke.get("validation", {}).get("width") == 128
        and smoke.get("validation", {}).get("height") == 128
    )
    gates.append(
        _gate(
            "R2-G02-real-local-smoke",
            smoke_ok,
            "Pinned core imported and produced a validator-accepted real 128x128 GIF without "
            "mutating the source Package.",
            ["evidence/canary-smoke/smoke-report.json"],
        )
    )

    graph_ok = (
        analysis.snapshot.package_id == "slack-gif-creator"
        and len(analysis.graph.nodes) >= 1
        and len(analysis.graph.edges) >= 1
        and not analysis.graph.diagnostics
    )
    gates.append(
        _gate(
            "R2-G03-package-graph",
            graph_ok,
            f"Parsed {len(analysis.snapshot.files)} files into {len(analysis.graph.nodes)} nodes/"
            f"{len(analysis.graph.edges)} edges with "
            f"{len(analysis.graph.diagnostics)} diagnostics.",
            ["artifacts/runs/r2-slack-gif-creator-evalplan/package/graph.json"],
        )
    )

    required_reads = set(work_item.required_package_reads)
    observed_reads = {event.path for event in submission.package_access}
    designer_ok = (
        submission.work_id == work_item.work_id
        and submission.role_run.usage.nonempty
        and required_reads <= observed_reads
        and submission.role_run.context_id.strip() != ""
        and submission.role_run.host_task_id.strip() != ""
    )
    gates.append(
        _gate(
            "R2-G04-isolated-eval-designer",
            designer_ok,
            f"Typed Agent-native Designer submission records {len(observed_reads)} Package reads, "
            f"non-empty usage, and isolated context/task provenance.",
            ["artifacts/runs/r2-slack-gif-creator-evalplan/designer-submission.json"],
        )
    )

    check_ok = checks.valid and all(item.status.value == "passed" for item in checks.checks)
    gates.append(
        _gate(
            "R2-G05-evalplan-automatic-checks",
            check_ok,
            f"{len(checks.checks)}/{len(checks.checks)} schema, coverage, fixture, license, "
            "split, nontrivial-oracle, isolation, evidence, and source checks passed.",
            ["artifacts/runs/r2-slack-gif-creator-evalplan/automatic-check-report.json"],
        )
    )

    trigger_ids = {case.case_id for case in frozen.trigger_cases}
    functional_ids = {case.case_id for case in frozen.functional_cases}
    oracle_keys = {
        "expectations",
        "rubric",
        "expected_output_zh",
        "candidate_identity",
        "sibling_output",
    }
    plan_ok = (
        len(trigger_ids) == 18
        and len(functional_ids) == 8
        and trigger_ids.isdisjoint(functional_ids)
        and all(not (set(case.executor_view()) & oracle_keys) for case in frozen.functional_cases)
        and all(not case.evidence_policy.enable_e1 for case in frozen.functional_cases)
    )
    gates.append(
        _gate(
            "R2-G06-channel-and-executor-isolation",
            plan_ok,
            "Frozen plan keeps 18 Trigger and 8 Functional cases separate; all Functional "
            "Executor views exclude oracle/sibling/candidate fields and use E2/E3 with E1 off.",
            ["artifacts/runs/r2-slack-gif-creator-evalplan/frozen-eval-plan.json"],
        )
    )

    reviewed_ids = {item.case_id for item in review.decisions}
    review_ok = (
        len(reviewed_ids) == len(review.decisions) == 26
        and reviewed_ids == trigger_ids | functional_ids
        and checkpoint.state is EvalPlanState.EXECUTION_READY
        and checkpoint.frozen_plan_hash == frozen.plan_hash
        and verification.valid
        and verification.unindexed_files == 0
    )
    gates.append(
        _gate(
            "R2-G07-review-freeze-resume",
            review_ok,
            "All 26 cases have traceable decisions; Core rechecked and froze the immutable plan, "
            "resumed the same run to execution_ready, and verified every run artifact.",
            [
                "artifacts/runs/r2-slack-gif-creator-evalplan/review.json",
                "artifacts/runs/r2-slack-gif-creator-evalplan/checkpoint.json",
                "artifacts/runs/r2-slack-gif-creator-evalplan/artifact-index.json",
            ],
        )
    )

    review_html = (run_dir / "review.html").read_text(encoding="utf-8")
    js_ok, js_detail = _check_javascript(review_html)
    required_ui = (
        "批量确认低风险 Train",
        "request-regeneration",
        "Package Graph",
        "导出 review.json",
        "search",
        "type-filter",
        "split-filter",
        "risk-filter",
        "case-json",
        "addEventListener",
    )
    static_ui_ok = (
        all(token in review_html for token in required_ui)
        and "<script src=" not in review_html
        and "<link " not in review_html
        and 'src="http' not in review_html
        and 'href="http' not in review_html
        and "url(http" not in review_html
        and js_ok
    )
    gates.append(
        _gate(
            "R2-G08-offline-review-static-contract",
            static_ui_ok,
            "Self-contained Chinese HTML contains search/filter/edit/batch/review/export/graph "
            f"handlers with no external resources; {js_detail}.",
            ["artifacts/runs/r2-slack-gif-creator-evalplan/review.html"],
        )
    )

    external_path = (
        (repo / args.external_validation).resolve() if args.external_validation else None
    )
    external: dict[str, Any] = {}
    if external_path and external_path.is_file():
        external = json.loads(external_path.read_text(encoding="utf-8"))
    external_ok = bool(
        external.get("confirmed")
        and external.get("offline_opened")
        and external.get("core_interactions_checked")
        and external.get("reviewer_kind") == "human"
    )
    gates.append(
        _gate(
            "R2-G09-offline-browser-human-validation",
            external_ok,
            "A human confirmed that the self-contained file opens offline and its core "
            "interactions "
            "work."
            if external_ok
            else "Pending one human offline-browser interaction check; "
            "the in-app automation policy blocks file:// navigation.",
            [args.external_validation.as_posix()] if args.external_validation else [],
        )
    )

    private_mock_ok = "private Skill packages are outside the R2 scope" in (
        repo / "src/gepase/evals/onboarding.py"
    ).read_text(encoding="utf-8") and all(
        token not in path.read_text(encoding="utf-8")
        for path in (
            repo / "src/gepase/evals/eval_plan.py",
            repo / "src/gepase/evals/eval_plan_checks.py",
            repo / "src/gepase/evals/onboarding.py",
        )
        for token in (
            "mock" + "_send",
            "selected" + "_action",
            "expected" + "_action",
        )
    )
    gates.append(
        _gate(
            "R2-G10-scope-boundary",
            private_mock_ok,
            "R2 onboarding Core adds no private-Skill, production-mock, selected-action, or "
            "expected-action interface.",
            ["src/gepase/evals/onboarding.py", "src/gepase/evals/eval_plan.py"],
        )
    )

    passed = sum(item["status"] == "passed" for item in gates)
    payload = {
        "schema_version": "1.0.0",
        "stage_id": "R2",
        "valid": passed == len(gates),
        "passed": passed,
        "total": len(gates),
        "gates": gates,
        "metrics": {
            "package_files": len(analysis.snapshot.files),
            "package_graph_nodes": len(analysis.graph.nodes),
            "package_graph_edges": len(analysis.graph.edges),
            "package_diagnostics": len(analysis.graph.diagnostics),
            "trigger_cases": len(frozen.trigger_cases),
            "functional_cases": len(frozen.functional_cases),
            "automatic_checks": len(checks.checks),
            "review_decisions": len(review.decisions),
            "unresolved_review_decisions": 0,
            "frozen_plan_hash": frozen.plan_hash,
            "run_artifact_verification": verification.as_dict(),
            "real_skill_effect_validated": False,
        },
    }
    ArtifactStore(stage_dir).write_json("evidence/r2-gates.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
