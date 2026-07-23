"""Leakage-group-aware split helpers."""

from __future__ import annotations

import hashlib
import json

from gepase.evals.schema import SplitManifest, TaskCase


def validate_group_isolation(cases: list[TaskCase]) -> dict[str, set[str]]:
    groups: dict[str, set[str]] = {"train": set(), "validation": set(), "test": set()}
    for case in cases:
        groups[case.split].add(case.leakage_group)
    return {
        "train_validation": groups["train"] & groups["validation"],
        "train_test": groups["train"] & groups["test"],
        "validation_test": groups["validation"] & groups["test"],
    }


def build_split_manifest(cases: list[TaskCase], version: str) -> SplitManifest:
    values = {
        split: tuple(sorted(case.id for case in cases if case.split == split))
        for split in ("train", "validation", "test")
    }
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return SplitManifest(
        version=version,
        train=values["train"],
        validation=values["validation"],
        test=values["test"],
        split_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )
