from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gepase.cli.app import app
from gepase.reporting.canary import CanaryReportBuilder, CanaryReportConfig
from gepase.store.artifacts import ArtifactStore, sha256_bytes

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs/canaries/slack-gif-creator-r5.json"


def test_report_config_rejects_paths_outside_repository() -> None:
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    payload["r2_run_ref"] = "/tmp/not-sealed"
    with pytest.raises(ValueError, match="repository-relative"):
        CanaryReportConfig.model_validate(payload)


def test_collect_is_read_only_and_recomputes_r5_facts() -> None:
    builder = CanaryReportBuilder.from_config(ROOT, CONFIG)
    upstream = [
        ROOT / "artifacts/runs/r2-slack-gif-creator-evalplan/artifact-index.json",
        ROOT / "artifacts/runs/r3-slack-gif-creator-paired/artifact-index.json",
        ROOT / "artifacts/runs/r4-slack-gif-creator-evolution/artifact-index.json",
    ]
    before = {path: sha256_bytes(path.read_bytes()) for path in upstream}
    bundle = builder.collect()
    after = {path: sha256_bytes(path.read_bytes()) for path in upstream}

    assert before == after
    assert not list(
        (ROOT / "artifacts/runs/r4-slack-gif-creator-evolution").glob(
            "rejected.sqlite3-*"
        )
    )
    assert bundle.data["status"] == "release_candidate_ready"
    assert all(bundle.data["success_gates"].values())
    assert bundle.data["headline"]["validation_mean_delta"] == pytest.approx(
        0.1242666666666667
    )
    assert len(bundle.data["validation_cases"]) == 3
    assert len([item for item in bundle.outputs if item.media_type == "image/gif"]) == 9


def test_build_and_verify_portable_report(tmp_path: Path) -> None:
    output = tmp_path / "r5-report"
    builder = CanaryReportBuilder.from_config(ROOT, CONFIG)
    result = builder.build(output)

    assert result["validation_mean_delta"] == pytest.approx(0.1242666666666667)
    assert ArtifactStore(output).verify().as_dict() == {
        "valid": True,
        "missing": 0,
        "hash_mismatch": 0,
        "schema_errors": 0,
        "checked": 20,
        "unindexed_files": 0,
    }
    verification = builder.verify(output)
    assert verification["valid"], verification

    data = json.loads((output / "report-data.json").read_text(encoding="utf-8"))
    html = (output / "index.html").read_text(encoding="utf-8")
    assert data["gates"]["funnel"] == {
        "proposed": 4,
        "gate_0_passed": 4,
        "gate_1_passed": 4,
        "gate_2_passed": 3,
        "gate_3_passed": 1,
        "accepted": 1,
    }
    assert data["runtime"]["budget_compliant"] is False
    assert data["runtime"]["exhausted_axes"] == ["wall_clock"]
    assert "只消费已经封存的 R2\u2013R4 evidence" in html
    assert "预算超限" not in html
    assert "冻结预算" not in html
    assert '<script src="http' not in html and '<link href="http' not in html

    archive = output / data["deployable"]["archive_path"]
    with zipfile.ZipFile(archive) as package:
        assert package.namelist() == sorted(item["path"] for item in data["deployable"]["files"])
        for item in data["deployable"]["files"]:
            assert sha256_bytes(package.read(item["path"])) == item["sha256"]

    with pytest.raises(FileExistsError):
        builder.build(output)


def test_report_inline_javascript_has_valid_syntax(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is optional; browser validation covers this in release checks")
    output = tmp_path / "syntax-report"
    CanaryReportBuilder.from_config(ROOT, CONFIG).build(output)
    script = (
        (output / "index.html")
        .read_text(encoding="utf-8")
        .split("<script>\n", maxsplit=1)[1]
        .split("\n</script>", maxsplit=1)[0]
    )
    result = subprocess.run(
        [node, "--check"],
        input=script,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_report_cli_build_verify_and_detect_tamper(tmp_path: Path) -> None:
    runner = CliRunner()
    output = tmp_path / "cli-report"
    build = runner.invoke(
        app,
        [
            "report",
            "build",
            "--config",
            str(CONFIG),
            "--output",
            str(output),
            "--format",
            "json",
        ],
    )
    assert build.exit_code == 0, build.output
    verify = runner.invoke(
        app,
        [
            "report",
            "verify",
            "--config",
            str(CONFIG),
            "--report-dir",
            str(output),
            "--format",
            "json",
        ],
    )
    assert verify.exit_code == 0, verify.output

    gif = next((output / "assets/gifs").rglob("*.gif"))
    gif.write_bytes(gif.read_bytes() + b"tamper")
    verify = runner.invoke(
        app,
        [
            "report",
            "verify",
            "--config",
            str(CONFIG),
            "--report-dir",
            str(output),
            "--format",
            "json",
        ],
    )
    assert verify.exit_code == 2
    assert '"valid": false' in verify.output


def test_report_cli_deploys_only_after_verification(tmp_path: Path) -> None:
    runner = CliRunner()
    report = tmp_path / "report"
    destination = tmp_path / "deployed-package"
    CanaryReportBuilder.from_config(ROOT, CONFIG).build(report)

    deployed = runner.invoke(
        app,
        [
            "report",
            "deploy",
            "--config",
            str(CONFIG),
            "--report-dir",
            str(report),
            "--output",
            str(destination),
            "--format",
            "json",
        ],
    )
    assert deployed.exit_code == 0, deployed.output
    payload = json.loads(deployed.output)
    assert payload["valid"] is True
    assert payload["files"] == 7
    assert (destination / "SKILL.md").is_file()

    repeated = runner.invoke(
        app,
        [
            "report",
            "deploy",
            "--config",
            str(CONFIG),
            "--report-dir",
            str(report),
            "--output",
            str(destination),
            "--format",
            "json",
        ],
    )
    assert repeated.exit_code == 2
    assert '"valid": false' in repeated.output
