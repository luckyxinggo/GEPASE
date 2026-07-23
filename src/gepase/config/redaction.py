"""Secret-safe serialization helpers."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

SECRET_FIELD = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|access[_-]?token|auth[_-]?token|bearer[_-]?token|"
    r"secret|password|credential)(?:$|[_-])"
)
SECRET_VALUE = re.compile(r"(?i)(sk-[A-Za-z0-9_-]{16,}|bearer\s+[A-Za-z0-9._-]{16,})")


def redact(value: Any) -> Any:
    """Recursively redact secret fields and common secret-looking values."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            result[text_key] = "***REDACTED***" if SECRET_FIELD.search(text_key) else redact(item)
        return result
    if isinstance(value, str):
        return SECRET_VALUE.sub("***REDACTED***", value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [redact(item) for item in value]
    return value
