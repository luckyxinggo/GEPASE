"""Check local Markdown links without depending on network availability."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


def markdown_files(root: Path) -> list[Path]:
    process = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "*.md",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode == 0:
        return [
            root / line
            for line in process.stdout.splitlines()
            if line and (root / line).is_file()
        ]
    return list(root.glob("*.md")) + list((root / "docs").rglob("*.md"))


def main() -> int:
    root = Path.cwd().resolve()
    broken: list[str] = []
    for source in markdown_files(root):
        text = source.read_text(encoding="utf-8")
        for raw in LINK.findall(text):
            target = raw.strip().split(maxsplit=1)[0].strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path_text = unquote(target.split("#", 1)[0])
            if not path_text:
                continue
            target_path = Path(path_text)
            resolved = target_path if target_path.is_absolute() else source.parent / target_path
            if not resolved.exists():
                broken.append(f"{source.relative_to(root)} -> {target}")
    if broken:
        print("\n".join(broken))
        return 1
    print("markdown links: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
