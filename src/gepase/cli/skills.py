"""Local Skill corpus commands."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from gepase.cli.app_support import emit
from gepase.skills.inventory import write_audit

skills_app = typer.Typer(
    no_args_is_help=True,
    help="Audit Skill package sources without publishing them.",
)


@skills_app.command("audit-corpus")
def audit_corpus_command(
    corpus: Path,
    output: Annotated[Path, typer.Option("--output")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    payload = write_audit(Path.cwd(), corpus, output)
    emit(payload, output_format)
    valid = all(
        payload[field] == 0
        for field in (
            "source_hash_missing",
            "capability_manifest_missing",
            "tracked_private_files",
            "source_mutation",
        )
    )
    if payload["local_sources"] < 5 or not valid:
        raise typer.Exit(2)
