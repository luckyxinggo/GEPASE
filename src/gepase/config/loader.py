"""Configuration loading, environment expansion, overrides, and hashing."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from gepase.config.models import ProjectConfig
from gepase.config.redaction import redact

ENV_PATTERN = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")


@dataclass(frozen=True)
class LoadedConfig:
    config: ProjectConfig
    config_hash: str
    redacted: dict[str, Any]


def _expand_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, str) and (match := ENV_PATTERN.match(value)):
        return os.environ.get(match.group(1), "")
    return value


def _apply_override(data: dict[str, Any], expression: str) -> None:
    key, separator, raw = expression.partition("=")
    if not separator or not key:
        raise ValueError(f"invalid override: {expression!r}")
    cursor: dict[str, Any] = data
    parts = key.split(".")
    for part in parts[:-1]:
        child = cursor.setdefault(part, {})
        if not isinstance(child, dict):
            raise ValueError(f"override traverses non-mapping field: {key!r}")
        cursor = child
    cursor[parts[-1]] = yaml.safe_load(raw)


def load_project_config(path: Path, overrides: tuple[str, ...] = ()) -> LoadedConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("project config root must be a mapping")
    data: dict[str, Any] = _expand_env(raw)
    for expression in overrides:
        _apply_override(data, expression)
    config = ProjectConfig.model_validate(data)
    redacted_data = redact(config.model_dump(mode="json", exclude_none=True))
    canonical = json.dumps(redacted_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return LoadedConfig(
        config=config,
        config_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        redacted=redacted_data,
    )

