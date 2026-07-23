"""Shared CLI rendering helpers."""

from __future__ import annotations

import json

import typer


def emit(payload: object, output_format: str) -> None:
    model_dump = getattr(payload, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(mode="json")
    if output_format == "json":
        typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        typer.echo(payload)
