"""Top-level Typer application."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from gepase import __version__
from gepase.cli.analyze import (
    analyze_faults,
    analyze_packages,
    graph_app,
    localize_faults,
)
from gepase.cli.app_support import emit
from gepase.cli.benchmark import benchmark_app
from gepase.cli.eval import eval_app
from gepase.cli.gate import gate_app
from gepase.cli.merge import merge_app
from gepase.cli.mutation import mutation_app, patch_app, selector_app
from gepase.cli.optimize import asi_app, candidate_app, optimizer_app
from gepase.cli.report import report_app
from gepase.cli.skills import skills_app
from gepase.config.loader import load_project_config
from gepase.services.mock_run import run_mock
from gepase.store.artifacts import ArtifactStore

app = typer.Typer(
    no_args_is_help=False,
    invoke_without_command=True,
    help="GEPASE Skill Package evolution core.",
)
config_app = typer.Typer(no_args_is_help=True, help="Validate and inspect project configuration.")
artifact_app = typer.Typer(no_args_is_help=True, help="Verify content-addressed artifacts.")
mock_app = typer.Typer(no_args_is_help=True, help="Run deterministic offline vertical slices.")
app.add_typer(config_app, name="config")
app.add_typer(artifact_app, name="artifact")
app.add_typer(mock_app, name="mock")
app.add_typer(benchmark_app, name="benchmark")
app.add_typer(skills_app, name="skills")
app.add_typer(eval_app, name="eval")
app.add_typer(graph_app, name="graph")
app.add_typer(candidate_app, name="candidate")
app.add_typer(optimizer_app, name="optimizer")
app.add_typer(asi_app, name="asi")
app.add_typer(mutation_app, name="mutation")
app.add_typer(patch_app, name="patch")
app.add_typer(selector_app, name="selector")
app.add_typer(gate_app, name="gate")
app.add_typer(merge_app, name="merge")
app.add_typer(report_app, name="report")
app.command("analyze")(analyze_packages)
app.command("analyze-fault-corpus")(analyze_faults)
app.command("localize")(localize_faults)


@app.callback()
def main(
    context: typer.Context,
    version: Annotated[
        bool,
        typer.Option("--version", help="Show the package version.", is_eager=True),
    ] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()
    if context.invoked_subcommand is None:
        typer.echo(context.get_help())


@app.command("doctor")
def doctor(
    output_format: Annotated[str, typer.Option("--format")] = "text",
) -> None:
    root = Path.cwd()
    checks = {
        "python": {"ok": sys.version_info >= (3, 11), "version": sys.version.split()[0]},
        "git": {"ok": (root / ".git").exists()},
        "uv": {"ok": shutil.which("uv") is not None},
        "private_corpus_ignored": {
            "ok": "/skills_test/" in (root / ".gitignore").read_text(encoding="utf-8")
            if (root / ".gitignore").exists()
            else False
        },
        "provider_credentials": {"ok": True, "status": "optional_missing_or_configured"},
    }
    status = "ok" if all(bool(item["ok"]) for item in checks.values()) else "error"
    emit({"status": status, "checks": checks}, output_format)
    if status != "ok":
        raise typer.Exit(2)


@config_app.command("validate")
def validate_config(
    path: Path,
    output_format: Annotated[str, typer.Option("--format")] = "text",
    override: Annotated[list[str] | None, typer.Option("--set")] = None,
) -> None:
    try:
        loaded = load_project_config(path, tuple(override or ()))
    except (OSError, ValueError, ValidationError) as error:
        emit({"valid": False, "error": str(error)}, output_format)
        raise typer.Exit(2) from error
    emit(
        {"valid": True, "config_hash": loaded.config_hash, "resolved": loaded.redacted},
        output_format,
    )


@artifact_app.command("verify")
def verify_artifacts(
    path: Path,
    output_format: Annotated[str, typer.Option("--format")] = "text",
) -> None:
    result = ArtifactStore(path).verify()
    emit(result.as_dict(), output_format)
    if not result.valid:
        raise typer.Exit(2)


@mock_app.command("run")
def mock_run(
    config: Annotated[Path, typer.Option("--config")],
    output: Annotated[Path, typer.Option("--output")],
    output_format: Annotated[str, typer.Option("--format")] = "text",
) -> None:
    payload = run_mock(config, output, Path.cwd())
    emit(payload, output_format)
