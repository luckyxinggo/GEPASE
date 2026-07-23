"""Replayable assertion families used before any LLM judge."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gepase.evals.schema import AssertionSpec


@dataclass(frozen=True)
class AssertionContext:
    artifact_root: Path


def _artifact(context: AssertionContext, parameters: dict[str, Any]) -> Path:
    relative = Path(str(parameters["path"]))
    path = (context.artifact_root / relative).resolve()
    if not path.is_relative_to(context.artifact_root.resolve()):
        raise ValueError("assertion path escapes artifact root")
    return path


def _json_pointer(value: Any, pointer: str) -> Any:
    current = value
    for token in pointer.strip("/").split("/") if pointer.strip("/") else ():
        token = token.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    return current


def evaluate_assertion(spec: AssertionSpec, context: AssertionContext) -> bool:
    path = _artifact(context, spec.parameters)
    if spec.family == "file_exists":
        return path.is_file() and path.stat().st_size >= int(spec.parameters.get("min_bytes", 1))
    if not path.is_file():
        return False
    try:
        if spec.family == "file_contains":
            text = path.read_text(encoding="utf-8")
            return all(str(value) in text for value in spec.parameters.get("values", []))
        if spec.family == "forbidden_text":
            text = path.read_text(encoding="utf-8")
            return not any(
                str(value).lower() in text.lower() for value in spec.parameters["values"]
            )
        if spec.family in {"json_equals", "json_range"}:
            value = _json_pointer(
                json.loads(path.read_text(encoding="utf-8")),
                spec.parameters["pointer"],
            )
            if spec.family == "json_equals":
                return value == spec.parameters["expected"]
            return float(spec.parameters["minimum"]) <= float(value) <= float(
                spec.parameters["maximum"]
            )
        if spec.family == "html_contract":
            text = path.read_text(encoding="utf-8")
            required = all(
                re.search(pattern, text, re.IGNORECASE) for pattern in spec.parameters["regex"]
            )
            no_remote = not re.search(r"(?:src|href)=[\"']https?://", text, re.IGNORECASE)
            return bool(
                required
                and (no_remote or not spec.parameters.get("no_remote_assets", False))
            )
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        KeyError,
        IndexError,
        TypeError,
        ValueError,
    ):
        # A malformed or structurally wrong candidate artifact is an assertion failure, not an
        # evaluator crash. Invalid benchmark paths are still rejected above by `_artifact`.
        return False
    raise ValueError(f"unsupported assertion family: {spec.family}")
