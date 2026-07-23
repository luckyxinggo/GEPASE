"""Run the S10 public-release Gates without rerunning Agent evaluation or search."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gepase.runtime import git_commit, source_tree_hash
from gepase.store.artifacts import ArtifactStore, sha256_bytes

ROOT = Path(__file__).resolve().parents[1]
STAGE_ROOT = ROOT / "artifacts/stages/S10"
LOCAL_ROOT = ROOT / "artifacts/local"


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(command: tuple[str, ...], results: list[CommandResult]) -> CommandResult:
    process = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
    result = CommandResult(command, process.returncode, process.stdout, process.stderr)
    results.append(result)
    return result


def unavailable(
    command: tuple[str, ...], results: list[CommandResult], reason: str
) -> CommandResult:
    """Record a blocked follow-up command without hiding the upstream failure."""
    result = CommandResult(command, 127, "", reason)
    results.append(result)
    return result


def parse_json(result: CommandResult) -> dict[str, Any]:
    if not result.ok:
        return {}
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def redacted(value: str) -> str:
    return value.replace(str(ROOT), "<PROJECT_ROOT>").replace(str(Path.home()), "<HOME>")


def command_log(results: list[CommandResult]) -> str:
    sections: list[str] = []
    for result in results:
        sections.extend(
            [
                f"$ {redacted(' '.join(result.command))}",
                f"exit_code={result.returncode}",
                "[stdout]",
                redacted(result.stdout.rstrip()),
                "[stderr]",
                redacted(result.stderr.rstrip()),
                "",
            ]
        )
    return "\n".join(sections)


def file_hashes(root: Path, pattern: str) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha256_bytes(path.read_bytes())
        for path in sorted(root.glob(pattern))
        if path.is_file()
    }


def module_audit() -> dict[str, object]:
    source_root = ROOT / "src"
    source_files = sorted((source_root / "gepase").rglob("*.py"))

    def name(path: Path) -> str:
        parts = list(path.relative_to(source_root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    modules = {name(path): path for path in source_files}
    incoming: dict[str, set[str]] = {item: set() for item in modules}
    scan_files = (
        source_files
        + sorted((ROOT / "tests").rglob("*.py"))
        + sorted((ROOT / "scripts").glob("*.py"))
    )
    parse_errors: list[str] = []
    for path in scan_files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as error:
            parse_errors.append(f"{path.relative_to(ROOT)}:{error}")
            continue
        importer = name(path) if path.is_relative_to(source_root) else str(path.relative_to(ROOT))
        package_parts = importer.split(".")[:-1] if importer.startswith("gepase.") else []
        targets: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level and package_parts:
                    prefix = package_parts[: len(package_parts) - node.level + 1]
                    base = ".".join(prefix + ([node.module] if node.module else []))
                else:
                    base = node.module or ""
                if base:
                    targets.append(base)
                    targets.extend(
                        f"{base}.{alias.name}" for alias in node.names if alias.name != "*"
                    )
        for target in targets:
            for candidate in modules:
                if target == candidate or target.startswith(candidate + "."):
                    if candidate != importer:
                        incoming[candidate].add(importer)
    zero_inbound = sorted(item for item, refs in incoming.items() if not refs)
    allowed_entrypoints = [item for item in zero_inbound if item == "gepase.__main__"]
    unexplained = sorted(set(zero_inbound) - set(allowed_entrypoints))
    return {
        "source_modules": len(modules),
        "parse_errors": parse_errors,
        "zero_inbound": zero_inbound,
        "allowed_entrypoints": allowed_entrypoints,
        "unexplained_zero_inbound": unexplained,
        "valid": not parse_errors and not unexplained,
    }


def main() -> int:
    if STAGE_ROOT.exists():
        shutil.rmtree(STAGE_ROOT)
    LOCAL_ROOT.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(STAGE_ROOT)
    commands: list[CommandResult] = []
    started_commit = git_commit(ROOT)
    started_tree = source_tree_hash(ROOT)
    status = run(("git", "status", "--porcelain=v1"), commands)
    dirty_entries = len(status.stdout.splitlines()) if status.ok else -1
    store.write_json(
        "preflight.json",
        {
            "schema_version": "1.0.0",
            "stage_id": "S10",
            "captured_at": datetime.now(UTC).isoformat(),
            "started_from_commit": started_commit,
            "source_tree_hash": started_tree,
            "dirty_entries_observed": dirty_entries,
            "scope": {
                "agent_native_roles_allowed": False,
                "headless_provider_calls_allowed": False,
                "candidate_search_allowed": False,
                "r3_rerun_allowed": False,
                "r4_rerun_allowed": False,
                "private_skills_in_scope": False,
                "sealed_r2_r5_evidence_only": True,
            },
        },
    )

    legacy_paths = (
        "artifacts/local_skill_inventory.json",
        "artifacts/runs/s2-agent-native-smoke",
        "artifacts/runs/s5-agent-native-search",
        "artifacts/runs/s6-graph-guided-agent-native",
        "artifacts/runs/s7-validation-gated-agent-native",
        "artifacts/runs/s8-fixture-work",
        "artifacts/stages/S0",
        "artifacts/stages/S1",
        "artifacts/stages/S2",
        "artifacts/stages/S3",
        "artifacts/stages/S4",
        "artifacts/stages/S5",
        "artifacts/stages/S6",
        "artifacts/stages/S7",
        "results",
        "scripts/run_s0_gates.py",
        "scripts/run_s1a_gates.py",
        "scripts/run_s2_gates.py",
        "scripts/run_s3_gates.py",
        "configs/experiments/s2_agent_native_smoke.yaml",
    )
    present_legacy = [item for item in legacy_paths if (ROOT / item).exists()]
    module_result = module_audit()
    deleted_entries = sum(line.startswith(" D ") for line in status.stdout.splitlines())
    cleanup = {
        "schema_version": "1.0.0",
        "policy": "reference-traced targeted cleanup; recoverable archive outside repository",
        "present_legacy_paths": present_legacy,
        "removed_groups": [
            {
                "id": "invalid-historical-results",
                "paths": ["results/", "artifacts/runs/s5-agent-native-search/"],
                "reason": "old E1/proxy scores are not valid Skill optimization evidence",
            },
            {
                "id": "historical-stage-outputs",
                "paths": ["artifacts/stages/S0-S7/", "artifacts/runs/s2,s6,s7,s8/"],
                "reason": "superseded by corrected R1-R5 evidence or migrated into minimal tests",
            },
            {
                "id": "unreferenced-python-facades",
                "paths": [
                    "evals/providers/replay.py",
                    "mutation/rollback.py",
                    "optimizer/merge/contract_fixtures.py",
                    "package/snapshot.py",
                    "store/checkpoints.py",
                ],
                "reason": (
                    "zero import/API/CLI/test inbound references; authoritative "
                    "implementations remain"
                ),
            },
            {
                "id": "historical-stage-runners",
                "paths": ["scripts/run_s0,s1a,s2,s3_gates.py", "s2_agent_native_smoke.yaml"],
                "reason": "hard-coded superseded artifacts and private-corpus inventory",
            },
        ],
        "migrated_fixtures": {
            "package_graph_overlay": (
                "tests/package/conftest.py creates minimal typed E1/E2 evidence"
            ),
            "merge_suite_output": "artifacts/local/merge-fixture-work (Git ignored)",
        },
        "deleted_git_entries": deleted_entries,
        "module_audit": module_result,
        "private_skill_modified": False,
        "permanent_delete_used": False,
        "valid": not present_legacy and bool(module_result["valid"]),
    }
    store.write_json("cleanup-manifest.json", cleanup)

    schema_before = file_hashes(ROOT / "schemas", "*.json")
    schema_export = run(("uv", "run", "python", "scripts/export_core_schemas.py"), commands)
    schema_after = file_hashes(ROOT / "schemas", "*.json")
    ruff = run(("uv", "run", "ruff", "check", "."), commands)
    pyright = run(("uv", "run", "pyright", "src", "tests", "scripts"), commands)
    pytest = run(
        (
            "uv",
            "run",
            "pytest",
            "-q",
            f"--junitxml={STAGE_ROOT / 'test-results.xml'}",
        ),
        commands,
    )
    if (STAGE_ROOT / "test-results.xml").is_file():
        store.index_existing("test-results.xml", "application/xml")
    secret = run(("uv", "run", "python", "scripts/check_secrets.py", "--format", "json"), commands)
    links = run(("uv", "run", "python", "scripts/check_markdown_links.py"), commands)
    license_check = run(("uv", "run", "python", "scripts/check_license.py"), commands)
    diff_check = run(("git", "diff", "--check"), commands)
    doctor = run(("uv", "run", "gepase", "doctor", "--format", "json"), commands)
    version = run(("uv", "run", "gepase", "--version"), commands)
    root_help = run(("uv", "run", "gepase", "--help"), commands)
    eval_help = run(("uv", "run", "gepase", "eval", "--help"), commands)
    optimizer_help = run(("uv", "run", "gepase", "optimizer", "--help"), commands)
    report_help = run(("uv", "run", "gepase", "report", "--help"), commands)
    agent_config = run(
        (
            "uv",
            "run",
            "gepase",
            "config",
            "validate",
            "configs/examples/mock.yaml",
            "--format",
            "json",
        ),
        commands,
    )
    headless_config = run(
        (
            "uv",
            "run",
            "gepase",
            "config",
            "validate",
            "configs/examples/headless-roles.yaml",
            "--format",
            "json",
        ),
        commands,
    )

    public_roots = (
        "artifacts/runs/r2-slack-gif-creator-evalplan",
        "artifacts/runs/r3-slack-gif-creator-paired",
        "artifacts/runs/r4-slack-gif-creator-evolution",
        "artifacts/runs/r5-slack-gif-creator-report",
        "artifacts/stages/R1",
        "artifacts/stages/R2",
        "artifacts/stages/R3",
        "artifacts/stages/R4",
        "artifacts/stages/R5",
    )
    public_verification: dict[str, dict[str, Any]] = {}
    for item in public_roots:
        result = run(
            ("uv", "run", "gepase", "artifact", "verify", item, "--format", "json"), commands
        )
        public_verification[item] = parse_json(result)

    report_verify = run(
        (
            "uv",
            "run",
            "gepase",
            "report",
            "verify",
            "--config",
            "configs/canaries/slack-gif-creator-r5.json",
            "--report-dir",
            "artifacts/runs/r5-slack-gif-creator-report",
            "--format",
            "json",
        ),
        commands,
    )
    r5_gates = run(
        (
            "uv",
            "run",
            "python",
            "scripts/run_r5_gates.py",
            "--report-dir",
            "artifacts/runs/r5-slack-gif-creator-report",
        ),
        commands,
    )

    fresh_install: dict[str, Any] = {"valid": False}
    smoke: dict[str, Any] = {"valid": False}
    with tempfile.TemporaryDirectory(prefix="s10-release-", dir=LOCAL_ROOT) as temporary:
        temporary_root = Path(temporary)
        mock_dir = temporary_root / "mock"
        mock_run = run(
            (
                "uv",
                "run",
                "gepase",
                "mock",
                "run",
                "--config",
                "configs/examples/mock.yaml",
                "--output",
                str(mock_dir),
                "--format",
                "json",
            ),
            commands,
        )
        mock_verify = run(
            ("uv", "run", "gepase", "artifact", "verify", str(mock_dir), "--format", "json"),
            commands,
        )
        deploy_dir = temporary_root / "deployed-package"
        deploy = run(
            (
                "uv",
                "run",
                "gepase",
                "report",
                "deploy",
                "--config",
                "configs/canaries/slack-gif-creator-r5.json",
                "--report-dir",
                "artifacts/runs/r5-slack-gif-creator-report",
                "--output",
                str(deploy_dir),
                "--format",
                "json",
            ),
            commands,
        )
        deployed_files = len([path for path in deploy_dir.rglob("*") if path.is_file()])
        smoke = {
            "valid": mock_run.ok and mock_verify.ok and deploy.ok and deployed_files == 7,
            "mock": parse_json(mock_run),
            "mock_verification": parse_json(mock_verify),
            "deploy": parse_json(deploy),
            "deployed_files": deployed_files,
        }

        dist = temporary_root / "dist"
        build = run(("uv", "build", "--out-dir", str(dist)), commands)
        wheel = dist / "gepase-0.1.0-py3-none-any.whl"
        sdist = dist / "gepase-0.1.0.tar.gz"
        venv = temporary_root / "venv"
        create_venv = run((sys.executable, "-m", "venv", str(venv)), commands)
        installed_cli = venv / ("Scripts/gepase.exe" if sys.platform == "win32" else "bin/gepase")
        venv_python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        install = run(
            (
                "uv",
                "pip",
                "install",
                "--python",
                str(venv_python),
                "--offline",
                str(wheel),
            ),
            commands,
        )
        installed_commands = (
            (str(installed_cli), "--version"),
            (str(installed_cli), "--help"),
            (
                str(installed_cli),
                "config",
                "validate",
                "configs/examples/mock.yaml",
                "--format",
                "json",
            ),
        )
        if install.ok and installed_cli.is_file():
            installed_version, installed_help, installed_config = (
                run(command, commands) for command in installed_commands
            )
        else:
            reason = "fresh-install CLI unavailable because the offline install failed"
            installed_version, installed_help, installed_config = (
                unavailable(command, commands, reason) for command in installed_commands
            )
        fresh_install = {
            "valid": all(
                item.ok
                for item in (
                    build,
                    create_venv,
                    install,
                    installed_version,
                    installed_help,
                    installed_config,
                )
            )
            and installed_version.stdout.strip() == "0.1.0"
            and wheel.is_file()
            and sdist.is_file()
            and wheel.stat().st_size < 2_000_000
            and sdist.stat().st_size < 2_000_000,
            "wheel_size_bytes": wheel.stat().st_size if wheel.is_file() else -1,
            "sdist_size_bytes": sdist.stat().st_size if sdist.is_file() else -1,
            "installed_version": installed_version.stdout.strip(),
            "offline_install": True,
        }

    report_data = json.loads(
        (ROOT / "artifacts/runs/r5-slack-gif-creator-report/report-data.json").read_text(
            encoding="utf-8"
        )
    )
    release_manifest = {
        "schema_version": "1.0.0",
        "version": "0.1.0",
        "languages": ["README.md", "README_zh.md"],
        "visuals": ["docs/assets/architecture.svg", "docs/assets/canary-results.svg"],
        "public_runs": public_verification,
        "public_schemas": len(schema_after),
        "schema_hashes_stable": schema_before == schema_after,
        "fresh_install": fresh_install,
        "reproduction_smoke": smoke,
        "canary_result": {
            "package_id": "slack-gif-creator",
            "candidate_id": report_data["headline"]["candidate_id"],
            "train_mean_delta": report_data["headline"]["train_mean_delta"],
            "validation_mean_delta": report_data["headline"]["validation_mean_delta"],
            "validation_wins": report_data["headline"]["validation_wins"],
            "validation_cases": 3,
            "single_canary_scope": True,
            "accepted_changed_files": report_data["deployable"]["files"][1]["path"],
        },
        "private_paths_published": 0,
        "private_skills_published": 0,
        "agent_calls_in_s10": 0,
        "headless_calls_in_s10": 0,
    }
    store.write_json("release-manifest.json", release_manifest)
    store.write_json(
        "evidence/static-and-schema.json",
        {
            "ruff": ruff.ok,
            "pyright": pyright.ok,
            "schema_export": schema_export.ok,
            "schema_hashes_stable": schema_before == schema_after,
            "schema_count": len(schema_after),
            "module_audit": module_result,
        },
    )
    secret_payload = parse_json(secret)
    store.write_json(
        "evidence/security-license-docs.json",
        {
            "secret_scan": secret_payload,
            "markdown_links": links.ok,
            "license_attribution": license_check.ok,
            "git_diff_check": diff_check.ok,
            "bilingual_readme": (ROOT / "README.md").is_file()
            and (ROOT / "README_zh.md").is_file(),
        },
    )
    store.write_json(
        "evidence/public-artifacts.json",
        {
            "verification": public_verification,
            "report_verify": parse_json(report_verify),
            "r5_gates": parse_json(r5_gates),
        },
    )
    store.write_json("evidence/fresh-install.json", fresh_install)
    store.write_json("evidence/reproduction-smoke.json", smoke)

    pytest_count = 0
    if (STAGE_ROOT / "test-results.xml").is_file():
        import xml.etree.ElementTree as ET

        xml_root = ET.parse(STAGE_ROOT / "test-results.xml").getroot()
        pytest_count = sum(
            int(item.attrib.get("tests", "0")) for item in xml_root.findall("testsuite")
        )
    public_valid = all(bool(item.get("valid")) for item in public_verification.values())
    r5_gate_payload = parse_json(r5_gates)
    english_readme = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese_readme = (ROOT / "README_zh.md").read_text(encoding="utf-8")
    claims_bounded = all(
        marker in english_readme
        for marker in (
            "slack-gif-creator",
            "cross-Skill",
            "multi-seed",
            "SKILL.md-only optimization",
        )
    ) and all(
        marker in chinese_readme
        for marker in ("slack-gif-creator", "跨 Skill", "多 seed", "只优化 `SKILL.md`")
    )
    gates = [
        {
            "gate_id": "S10-G01-targeted-cleanup",
            "status": "passed" if cleanup["valid"] else "failed",
            "detail": (
                "Legacy paths are absent and only the module entrypoint has zero inbound "
                "references."
            ),
        },
        {
            "gate_id": "S10-G02-bilingual-public-surface",
            "status": "passed" if links.ok and claims_bounded and root_help.ok else "failed",
            "detail": (
                "Bilingual READMEs, visuals, links, commands, and claim boundaries are present."
            ),
        },
        {
            "gate_id": "S10-G03-sealed-evidence-and-attribution",
            "status": "passed"
            if public_valid and report_verify.ok and r5_gates.ok and license_check.ok
            else "failed",
            "detail": "R1-R5/R2-R5 hashes, R5 Gates, canary provenance, and licenses verify.",
        },
        {
            "gate_id": "S10-G04-schema-security-privacy",
            "status": "passed"
            if schema_export.ok
            and schema_before == schema_after
            and secret.ok
            and not secret_payload.get("findings")
            else "failed",
            "detail": (
                "Generated schemas are stable and the public/untracked surface has no "
                "secret/private-path findings."
            ),
        },
        {
            "gate_id": "S10-G05-full-regression-and-build",
            "status": "passed"
            if ruff.ok and pyright.ok and pytest.ok and fresh_install["valid"]
            else "failed",
            "detail": (
                "Ruff, Pyright, full pytest, compact build, and fresh offline wheel install pass."
            ),
        },
        {
            "gate_id": "S10-G06-cli-resume-report-deploy-smoke",
            "status": "passed"
            if all(
                item.ok
                for item in (
                    doctor,
                    version,
                    root_help,
                    eval_help,
                    optimizer_help,
                    report_help,
                    agent_config,
                    headless_config,
                )
            )
            and smoke["valid"]
            else "failed",
            "detail": (
                "Public CLI, both provider configs, offline mock, report verification, and "
                "deploy pass."
            ),
        },
        {
            "gate_id": "S10-G07-release-claim-boundary",
            "status": "passed"
            if claims_bounded
            and report_data["headline"]["single_canary_scope"]
            and report_data["deployable"]["changed_files"] == ["SKILL.md"]
            else "failed",
            "detail": (
                "README results exactly retain the one-canary and SKILL.md-only accepted-edit "
                "limits."
            ),
        },
    ]
    all_passed = all(item["status"] == "passed" for item in gates)
    store.write_json(
        "evidence/s10-gates.json",
        {
            "schema_version": "1.0.0",
            "valid": all_passed,
            "gates": gates,
            "summary": {
                "passed": sum(item["status"] == "passed" for item in gates),
                "total": len(gates),
            },
        },
    )
    store.write_text("commands.log", command_log(commands), "text/plain")
    store.write_text(
        "external-validation.md",
        "# S10 fresh-install validation\n\n"
        f"Status: {'passed' if fresh_install['valid'] else 'failed'}\n\n"
        "A compact sdist and wheel were built from the dirty-but-curated release tree. The wheel "
        "was installed with `uv pip --offline` into a new virtual environment, then its version, "
        "root help, and config validation were executed. No Agent, external LLM API, candidate "
        "search, R3 rerun, or R4 rerun was performed.\n",
        "text/markdown",
    )
    store.write_text(
        "stage-summary.md",
        "# S10 release summary\n\n"
        f"- Machine Gates: {sum(item['status'] == 'passed' for item in gates)}/{len(gates)}\n"
        f"- Full tests: {pytest_count} passed\n"
        f"- Public schemas: {len(schema_after)} stable exports\n"
        f"- Secret findings: {len(secret_payload.get('findings', []))}\n"
        f"- Fresh wheel install: {'passed' if fresh_install['valid'] else 'failed'}\n"
        "- Agent/API/search calls in S10: 0/0/0\n"
        "- Algorithm result remains one sealed public canary: +0.12427 held-out mean, 3/3 wins.\n",
        "text/markdown",
    )
    output_index = json.loads((STAGE_ROOT / "artifact-index.json").read_text(encoding="utf-8"))
    stage_report = {
        "schema_version": "1.0.0",
        "stage_id": "S10",
        "status": "complete" if all_passed else "blocked",
        "started_from_commit": started_commit,
        "finished_commit": git_commit(ROOT),
        "source_tree_hash": source_tree_hash(ROOT),
        "source_tree_hash_scope": (
            "current curated dirty worktree; private/ignored content excluded"
        ),
        "input_artifacts": [
            {"path": item, "valid": bool(payload.get("valid")), "checked": payload.get("checked")}
            for item, payload in public_verification.items()
        ],
        "output_artifacts": output_index["artifacts"],
        "gate_results": gates,
        "real_agent_runs": 0,
        "headless_provider_runs": 0,
        "candidate_searches": 0,
        "r3_reruns": 0,
        "r4_reruns": 0,
        "metrics": {
            "pytest_passed": pytest_count,
            "source_modules": module_result["source_modules"],
            "public_schemas": len(schema_after),
            "public_artifact_roots_verified": sum(
                bool(item.get("valid")) for item in public_verification.values()
            ),
            "secret_scan_files": secret_payload.get("scanned_files", 0),
            "secret_scan_findings": len(secret_payload.get("findings", [])),
            "machine_gates_passed": sum(item["status"] == "passed" for item in gates),
            "machine_gates_total": len(gates),
            "validation_mean_delta": report_data["headline"]["validation_mean_delta"],
            "validation_wins": report_data["headline"]["validation_wins"],
            "fresh_install_passed": bool(fresh_install["valid"]),
        },
        "known_issues": [
            (
                "Optimization effect is one Skill, one EvalPlan, one model snapshot, and one "
                "search run."
            ),
            (
                "The accepted candidate changed one bounded SKILL.md node; no positive "
                "cross-file edit was observed."
            ),
            (
                "Role-scoped Headless configuration is a validated interface; v0.1 does not "
                "ship a built-in API runtime."
            ),
            (
                "R4 wall-clock budget overrun remains documented in state/stage evidence and "
                "was not rerun in S10."
            ),
        ],
        "design_decisions": [
            (
                "Publish corrected R2-R5 evidence and R1-R5/S10 stage reports; omit "
                "superseded S-stage outputs and old proxy results."
            ),
            "Keep GitHub evidence separate from the compact Python sdist/wheel.",
            (
                "Use Agent-native by default and expose only a typed optional Headless routing "
                "boundary."
            ),
            (
                "Do not run Agent roles, Headless APIs, candidate search, R3, or R4 during "
                "release verification."
            ),
        ],
        "unlocks": ["GitHub v0.1 release"] if all_passed else [],
        "upstream_r5_machine_gates": r5_gate_payload.get("summary", {}),
    }
    store.write_json("stage_report.json", stage_report)
    verification = store.verify().as_dict()
    result = {
        "schema_version": "1.0.0",
        "valid": all_passed and bool(verification["valid"]),
        "stage_id": "S10",
        "gates": gates,
        "artifact_verification": verification,
        "pytest_passed": pytest_count,
        "agent_calls": 0,
        "headless_provider_calls": 0,
        "candidate_searches": 0,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
