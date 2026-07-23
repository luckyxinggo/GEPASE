"""Gate 1: deterministic structure, syntax, lint, reference, and safety checks."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from gepase.mutation.schema import PatchApplication, PatchApplicationStatus
from gepase.optimizer.acceptance.models import (
    GateLevel,
    GateOutcome,
    GateResult,
    GateUsage,
)
from gepase.package.analyzer import PackageAnalyzer

_FORBIDDEN_PATTERNS = (
    "os.system(",
    "subprocess.Popen(",
    "eval(",
    "exec(",
    "requests.post(",
    "urllib.request.urlopen(",
)


def _python_syntax(package_root: Path) -> tuple[bool, list[str]]:
    errors = []
    for path in sorted(package_root.rglob("*.py")):
        try:
            compile(path.read_text(encoding="utf-8"), path.as_posix(), "exec")
        except (SyntaxError, UnicodeError) as error:
            errors.append(f"{path.relative_to(package_root).as_posix()}:{error}")
    return not errors, errors


def _security(package_root: Path) -> tuple[bool, list[str]]:
    findings = []
    for path in sorted(package_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".py", ".sh", ".md"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        for pattern in _FORBIDDEN_PATTERNS:
            if pattern in text:
                findings.append(f"{path.relative_to(package_root).as_posix()}:{pattern}")
    return not findings, findings


def _ruff(package_root: Path) -> tuple[bool, dict[str, object]]:
    executable = shutil.which("ruff")
    if executable is None:
        return False, {"error": "ruff executable unavailable"}
    process = subprocess.run(
        [executable, "check", ".", "--output-format", "concise"],
        cwd=package_root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    version = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return process.returncode == 0, {
        "command": ["ruff", "check", "<candidate-package>"],
        "version": version.stdout.strip(),
        "returncode": process.returncode,
        "stdout": process.stdout[-4_000:],
        "stderr": process.stderr[-4_000:],
    }


def run_static_gate(
    project_root: Path,
    application: PatchApplication,
    *,
    baseline_package_root: Path | None = None,
) -> GateResult:
    if application.status is not PatchApplicationStatus.APPLIED or not application.workspace_ref:
        return GateResult(
            level=GateLevel.GATE_1_STATIC,
            outcome=GateOutcome.NOT_RUN,
            reason_codes=("patch_not_applied",),
            human_summary="Static validation was not run because patch application failed.",
            checks={},
            target_calls=0,
        )
    root = project_root.resolve()
    package_root = (root / application.workspace_ref).resolve(strict=True)
    if not package_root.is_relative_to(root):
        raise ValueError("candidate package escapes project root")
    analysis = PackageAnalyzer().analyze(package_root)
    error_diagnostics = [
        item.model_dump(mode="json")
        for item in analysis.graph.diagnostics
        if item.severity == "error"
    ]
    syntax_ok, syntax_errors = _python_syntax(package_root)
    safety_ok, safety_findings = _security(package_root)
    ruff_ok, ruff_report = _ruff(package_root)
    references_ok = not any(
        item.kind in {"broken_reference", "unsafe_path"} for item in analysis.graph.diagnostics
    )
    frontmatter_ok = not any(
        item.kind in {"missing_frontmatter", "invalid_frontmatter"}
        for item in analysis.graph.diagnostics
    )
    baseline_checks: dict[str, object] | None = None
    regression_aware_pass = False
    if baseline_package_root is not None:
        baseline = baseline_package_root.resolve(strict=True)
        if not baseline.is_relative_to(root):
            raise ValueError("static baseline escapes project root")
        baseline_analysis = PackageAnalyzer().analyze(baseline)
        baseline_errors = [
            item.model_dump(mode="json")
            for item in baseline_analysis.graph.diagnostics
            if item.severity == "error"
        ]
        baseline_syntax_ok, baseline_syntax_errors = _python_syntax(baseline)
        baseline_safety_ok, baseline_safety_findings = _security(baseline)
        baseline_ruff_ok, baseline_ruff_report = _ruff(baseline)
        baseline_references_ok = not any(
            item.kind in {"broken_reference", "unsafe_path"}
            for item in baseline_analysis.graph.diagnostics
        )
        baseline_frontmatter_ok = not any(
            item.kind in {"missing_frontmatter", "invalid_frontmatter"}
            for item in baseline_analysis.graph.diagnostics
        )
        candidate_issue_count = sum(
            (
                len(error_diagnostics),
                len(syntax_errors),
                len(safety_findings),
                int(not ruff_ok),
                int(not references_ok),
                int(not frontmatter_ok),
            )
        )
        baseline_issue_count = sum(
            (
                len(baseline_errors),
                len(baseline_syntax_errors),
                len(baseline_safety_findings),
                int(not baseline_ruff_ok),
                int(not baseline_references_ok),
                int(not baseline_frontmatter_ok),
            )
        )
        regression_aware_pass = (
            candidate_issue_count <= baseline_issue_count
            and set(safety_findings) <= set(baseline_safety_findings)
            and len(syntax_errors) <= len(baseline_syntax_errors)
        )
        baseline_checks = {
            "issue_count": baseline_issue_count,
            "error_diagnostics": baseline_errors,
            "python_syntax_valid": baseline_syntax_ok,
            "security_valid": baseline_safety_ok,
            "ruff_valid": baseline_ruff_ok,
            "ruff": baseline_ruff_report,
            "references_valid": baseline_references_ok,
            "frontmatter_valid": baseline_frontmatter_ok,
        }
    checks = {
        "package_parse": True,
        "error_diagnostics": error_diagnostics,
        "frontmatter_valid": frontmatter_ok,
        "references_valid": references_ok,
        "python_syntax_valid": syntax_ok,
        "python_syntax_errors": syntax_errors,
        "ruff_valid": ruff_ok,
        "ruff": ruff_report,
        "security_valid": safety_ok,
        "security_findings": safety_findings,
        "unit_tests": "not_declared_in_public_benchmark_package",
        "command_whitelist": ["python.compile", "ruff check"],
        "regression_aware": baseline_package_root is not None,
        "baseline": baseline_checks,
        "no_new_static_regression": regression_aware_pass,
    }
    absolute_pass = (
        frontmatter_ok
        and references_ok
        and syntax_ok
        and ruff_ok
        and safety_ok
        and not error_diagnostics
    )
    passed = absolute_pass or regression_aware_pass
    reasons = []
    if not frontmatter_ok and baseline_package_root is None:
        reasons.append("frontmatter_invalid")
    if not references_ok and baseline_package_root is None:
        reasons.append("reference_invalid")
    if not syntax_ok and baseline_package_root is None:
        reasons.append("syntax_invalid")
    if not ruff_ok and baseline_package_root is None:
        reasons.append("lint_invalid")
    if not safety_ok and baseline_package_root is None:
        reasons.append("security_invalid")
    if error_diagnostics and baseline_package_root is None:
        reasons.append("package_diagnostic_error")
    if baseline_package_root is not None and not regression_aware_pass:
        reasons.append("new_static_regression")
    return GateResult(
        level=GateLevel.GATE_1_STATIC,
        outcome=GateOutcome.PASSED if passed else GateOutcome.FAILED,
        reason_codes=(
            ("static_validation_passed",)
            if absolute_pass
            else ("static_non_regression_passed",)
            if passed
            else tuple(reasons)
        ),
        human_summary=(
            "Candidate package passed deterministic structure, syntax, lint, and safety checks."
            if passed
            else "Candidate package failed deterministic static validation."
        ),
        evidence_refs=(f"application:{application.application_id}",),
        checks=checks,
        usage=GateUsage(duration_ms=0),
        target_calls=0,
    )
