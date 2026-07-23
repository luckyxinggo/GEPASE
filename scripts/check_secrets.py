"""Scan tracked and generated evidence for secrets and private local paths."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

SECRET_PATTERNS = {
    # Require a token boundary so ordinary identifiers such as
    # ``task-first-...`` cannot be misread from their embedded ``sk-``.
    "api_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "assigned_secret": re.compile(
        r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*(['\"])([^'\"]{12,})\2"
    ),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
SAFE_ASSIGNED_VALUES = {
    "requires_secret",
    "required_secret",
    "secret_reference",
}
PRIVATE_PATH = re.compile(r"/(?:Users|home)/[^/\s]+/")
SKIP_PARTS = {".git", ".venv", "skills_test", "__pycache__", ".pytest_cache", ".ruff_cache"}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str


def tracked_files(root: Path) -> list[Path]:
    process = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=False,
        capture_output=True,
    )
    if process.returncode != 0:
        return []
    return [root / item.decode() for item in process.stdout.split(b"\0") if item]


def generated_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory in (root / "artifacts", root / "results"):
        if directory.exists():
            files.extend(
                path
                for path in directory.rglob("*")
                if path.is_file() and not path.is_relative_to(root / "artifacts/local")
            )
    return files


def scan_files(root: Path, files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(set(files)):
        if not path.exists() or any(part in SKIP_PARTS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for kind, pattern in SECRET_PATTERNS.items():
                match = pattern.search(line)
                if match and not (
                    kind == "assigned_secret" and match.group(3).casefold() in SAFE_ASSIGNED_VALUES
                ):
                    findings.append(Finding(path.relative_to(root).as_posix(), number, kind))
            if PRIVATE_PATH.search(line):
                findings.append(Finding(path.relative_to(root).as_posix(), number, "private_path"))
    return findings


def audit(root: Path, *, include_generated: bool = True) -> dict[str, object]:
    tracked = tracked_files(root)
    tracked_private = [
        path.relative_to(root).as_posix()
        for path in tracked
        if path.relative_to(root).parts[:1] == ("skills_test",)
    ]
    generated = generated_files(root) if include_generated else []
    findings = scan_files(root, tracked + generated)
    return {
        "valid": not findings and not tracked_private,
        "findings": [asdict(item) for item in findings],
        "tracked_skills_test_files": len(tracked_private),
        "tracked_skills_test_paths": tracked_private,
        "scanned_files": len(set(tracked + generated)),
        "include_generated": include_generated,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument(
        "--tracked-only",
        action="store_true",
        help="Scan the public Git-tracked surface and exclude ignored private run artifacts.",
    )
    arguments = parser.parse_args()
    result = audit(
        arguments.root.resolve(),
        include_generated=not arguments.tracked_only,
    )
    if arguments.format == "json":
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("secret scan: ok" if result["valid"] else json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
