"""Environment and source identity helpers; not an Agent Runtime."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path


def command_output(*args: str) -> str:
    process = subprocess.run(args, check=False, capture_output=True, text=True)
    return process.stdout.strip() if process.returncode == 0 else ""


def git_commit(root: Path) -> str:
    return command_output("git", "-C", str(root), "rev-parse", "HEAD") or "WORKTREE"


def source_tree_hash(root: Path) -> str:
    process = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        check=False,
        capture_output=True,
    )
    selected = []
    for value in process.stdout.split(b"\0"):
        if not value or value.startswith((b"artifacts/", b"results/")):
            continue
        path = root / value.decode()
        if path.is_file():
            selected.append(path)
    if not selected:
        for relative in ("src", "configs", "scripts", "pyproject.toml", "uv.lock"):
            path = root / relative
            if path.is_file():
                selected.append(path)
            elif path.is_dir():
                selected.extend(item for item in path.rglob("*") if item.is_file())
    payload = []
    for path in sorted(selected):
        payload.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def environment_summary() -> dict[str, str]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "implementation": platform.python_implementation(),
    }
