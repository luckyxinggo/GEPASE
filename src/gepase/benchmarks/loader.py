"""Load a benchmark manifest and its JSONL TaskCase files."""

from __future__ import annotations

from pathlib import Path

from gepase.evals.schema import BenchmarkManifest, TaskCase


def load_manifest(path: Path) -> BenchmarkManifest:
    return BenchmarkManifest.model_validate_json(path.read_text(encoding="utf-8"))


def load_cases(root: Path, manifest: BenchmarkManifest) -> list[TaskCase]:
    cases: list[TaskCase] = []
    for package in manifest.packages:
        dataset = root / package.dataset_path
        for line in dataset.read_text(encoding="utf-8").splitlines():
            if line.strip():
                cases.append(TaskCase.model_validate_json(line))
    return cases
