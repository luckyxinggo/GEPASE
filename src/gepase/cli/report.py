"""Build and verify sealed-evidence canary reports."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated

import typer

from gepase.cli.app_support import emit
from gepase.reporting.canary import CanaryReportBuilder, ReportEvidenceError
from gepase.store.artifacts import sha256_bytes

report_app = typer.Typer(
    no_args_is_help=True,
    help="Build reproducible static reports from sealed evaluation/evolution evidence.",
)


@report_app.command("build")
def build_report(
    config: Annotated[Path, typer.Option("--config")],
    output: Annotated[Path, typer.Option("--output")],
    output_format: Annotated[str, typer.Option("--format")] = "text",
) -> None:
    """Build a new report directory without rerunning evaluation or search."""

    try:
        result = CanaryReportBuilder.from_config(Path.cwd(), config).build(output)
    except (OSError, ValueError, ReportEvidenceError) as error:
        emit({"valid": False, "error": str(error)}, output_format)
        raise typer.Exit(2) from error
    emit({"valid": True, **result}, output_format)


@report_app.command("verify")
def verify_report(
    config: Annotated[Path, typer.Option("--config")],
    report_dir: Annotated[Path, typer.Option("--report-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "text",
) -> None:
    """Recompute the report view from sealed inputs and compare every copied output."""

    try:
        result = CanaryReportBuilder.from_config(Path.cwd(), config).verify(report_dir)
    except (OSError, ValueError, ReportEvidenceError) as error:
        emit({"valid": False, "error": str(error)}, output_format)
        raise typer.Exit(2) from error
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@report_app.command("deploy")
def deploy_report_package(
    config: Annotated[Path, typer.Option("--config")],
    report_dir: Annotated[Path, typer.Option("--report-dir")],
    output: Annotated[Path, typer.Option("--output")],
    output_format: Annotated[str, typer.Option("--format")] = "text",
) -> None:
    """Verify a sealed report and copy its deployable Package to a new directory."""

    try:
        report_root = report_dir.resolve(strict=True)
        verification = CanaryReportBuilder.from_config(Path.cwd(), config).verify(report_root)
        if not verification["valid"]:
            raise ReportEvidenceError("report verification failed")
        data = json.loads((report_root / "report-data.json").read_text(encoding="utf-8"))
        deployable = data["deployable"]
        package_ref = Path(str(deployable["package_path"]))
        if package_ref.is_absolute() or ".." in package_ref.parts:
            raise ReportEvidenceError("deployable package path is not report-relative")
        source = (report_root / package_ref).resolve(strict=True)
        if not source.is_relative_to(report_root) or not source.is_dir():
            raise ReportEvidenceError("deployable package escapes the sealed report")
        destination = output.resolve()
        if destination.exists():
            raise FileExistsError(f"deploy output already exists: {destination}")
        shutil.copytree(source, destination, copy_function=shutil.copy2)
        expected = {str(item["path"]): str(item["sha256"]) for item in deployable["files"]}
        actual = {
            path.relative_to(destination).as_posix(): sha256_bytes(path.read_bytes())
            for path in sorted(destination.rglob("*"))
            if path.is_file()
        }
        if actual != expected:
            shutil.rmtree(destination)
            raise ReportEvidenceError("deployed Package does not match sealed file hashes")
    except (KeyError, OSError, ValueError, ReportEvidenceError) as error:
        emit({"valid": False, "error": str(error)}, output_format)
        raise typer.Exit(2) from error
    emit(
        {
            "valid": True,
            "candidate_id": deployable["candidate_id"],
            "output": output.as_posix(),
            "files": len(expected),
        },
        output_format,
    )
