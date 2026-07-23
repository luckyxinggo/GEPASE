"""Benchmark contract and audit commands."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer

from gepase.benchmarks.audit import (
    audit_leakage,
    audit_licenses,
    score_composition,
    validate_benchmark,
)
from gepase.benchmarks.evolution import verify_evolution_track
from gepase.benchmarks.lifecycle import freeze_benchmark, mutation_test
from gepase.cli.app_support import emit

benchmark_app = typer.Typer(no_args_is_help=True, help="Validate and audit benchmark contracts.")


def _run_audit(
    operation: Callable[[Path, Path], dict[str, Any]],
    manifest: Path,
    output_format: str,
) -> None:
    payload = operation(Path.cwd(), manifest)
    emit(payload, output_format)
    if not payload.get("valid", False):
        raise typer.Exit(2)


@benchmark_app.command("validate")
def validate(
    manifest: Path,
    output_format: Annotated[str, typer.Option("--format")] = "text",
) -> None:
    _run_audit(validate_benchmark, manifest, output_format)


@benchmark_app.command("audit-license")
def license_audit(
    manifest: Path,
    output_format: Annotated[str, typer.Option("--format")] = "text",
) -> None:
    _run_audit(audit_licenses, manifest, output_format)


@benchmark_app.command("audit-leakage")
def leakage_audit(
    manifest: Path,
    output_format: Annotated[str, typer.Option("--format")] = "text",
) -> None:
    _run_audit(audit_leakage, manifest, output_format)


@benchmark_app.command("mutation-test")
def mutation_test_command(
    manifest: Path,
    mutants: Annotated[Path, typer.Option("--mutants")],
    output_format: Annotated[str, typer.Option("--format")] = "text",
) -> None:
    payload = mutation_test(Path.cwd(), manifest, mutants)
    emit(payload, output_format)
    if not payload["valid"]:
        raise typer.Exit(2)


@benchmark_app.command("score-composition")
def score_composition_command(
    manifest: Path,
    output_format: Annotated[str, typer.Option("--format")] = "text",
) -> None:
    _run_audit(score_composition, manifest, output_format)


@benchmark_app.command("freeze")
def freeze_command(
    manifest: Annotated[Path, typer.Option("--manifest")],
    version: Annotated[str, typer.Option("--version")],
    output: Annotated[Path, typer.Option("--output")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    payload = freeze_benchmark(Path.cwd(), manifest, version=version, output=output)
    emit(payload, output_format)
    if not payload["valid"]:
        raise typer.Exit(2)


@benchmark_app.command("verify")
def verify_command(
    freeze_lock: Path,
    evolution_track: Path,
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    payload = verify_evolution_track(Path.cwd(), freeze_lock, evolution_track)
    emit(payload, output_format)
    if not payload["valid"]:
        raise typer.Exit(2)
