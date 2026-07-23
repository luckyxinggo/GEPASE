"""Same-package merge contract fixture commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from gepase.cli.app_support import emit
from gepase.optimizer.merge.fixture_suite import run_merge_fixture_suite
from gepase.store.artifacts import atomic_write, canonical_json_bytes

merge_app = typer.Typer(
    no_args_is_help=True,
    help="Validate same-package merge contracts, closure, and conflict handling.",
)


@merge_app.command("fixture-suite")
def fixture_suite(
    fixture_dir: Annotated[Path, typer.Argument()],
    output: Annotated[Path | None, typer.Option("--output")] = None,
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    result = run_merge_fixture_suite(Path.cwd(), fixture_dir)
    if output is not None:
        atomic_write(output, canonical_json_bytes(result))
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)
