"""Reject credentials and user-home absolute paths before evidence persistence."""

from __future__ import annotations

import json
import re
from typing import Any

SENSITIVE = {
    "api_key": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    "private_home_path": re.compile(r"/(?:Users|home)/[^/\s]+/"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def sensitive_kinds(value: Any) -> tuple[str, ...]:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return tuple(name for name, pattern in SENSITIVE.items() if pattern.search(text))


def ensure_redacted(value: Any, *, field: str) -> None:
    findings = sensitive_kinds(value)
    if findings:
        raise ValueError(f"{field} contains prohibited sensitive data: {', '.join(findings)}")
