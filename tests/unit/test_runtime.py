from __future__ import annotations

import subprocess
from pathlib import Path

from gepase.runtime import source_tree_hash


def _git(root: Path, *args: str) -> None:
    subprocess.run(("git", "-C", str(root), *args), check=True, capture_output=True)


def test_source_tree_hash_includes_untracked_nonignored_sources(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("ignored.txt\nartifacts/\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore", "tracked.txt")
    baseline = source_tree_hash(tmp_path)

    (tmp_path / "new_source.py").write_text("VALUE = 1\n", encoding="utf-8")
    with_untracked = source_tree_hash(tmp_path)
    assert with_untracked != baseline

    (tmp_path / "ignored.txt").write_text("secret local input\n", encoding="utf-8")
    assert source_tree_hash(tmp_path) == with_untracked

    artifact = tmp_path / "artifacts" / "run.json"
    artifact.parent.mkdir()
    artifact.write_text("{}\n", encoding="utf-8")
    assert source_tree_hash(tmp_path) == with_untracked
