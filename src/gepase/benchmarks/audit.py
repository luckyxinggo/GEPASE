"""S1-A benchmark contract gates."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from gepase.benchmarks.loader import load_cases, load_manifest
from gepase.evals.schema import SplitManifest, TaskCase
from gepase.evals.split import validate_group_isolation

ALLOWED_LICENSES = {"Apache-2.0", "MIT", "BSD-3-Clause", "CC-BY-4.0"}


def validate_benchmark(root: Path, manifest_path: Path) -> dict[str, Any]:
    schema_errors: list[str] = []
    missing_fixtures: list[str] = []
    try:
        manifest = load_manifest(manifest_path)
        cases = load_cases(root, manifest)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        return {
            "valid": False,
            "packages": 0,
            "cases": 0,
            "schema_errors": 1,
            "errors": [str(error)],
            "missing_fixture": 0,
        }
    counts = Counter(case.skill_id for case in cases)
    package_ids = {package.skill_id for package in manifest.packages}
    duplicate_ids = [
        identifier
        for identifier, count in Counter(case.id for case in cases).items()
        if count > 1
    ]
    schema_errors.extend(f"duplicate case id: {item}" for item in duplicate_ids)
    for package in manifest.packages:
        required_paths = (
            Path(package.skill_path) / "SKILL.md",
            Path(package.capability_manifest_ref),
            Path(package.benchmark_card_ref),
            Path(package.provenance_ref),
            Path(package.dataset_path),
        )
        for required in required_paths:
            if not (root / required).is_file():
                schema_errors.append(f"{package.skill_id}: missing {required.as_posix()}")
    for rubric in manifest.rubric_refs:
        if not (root / rubric).is_file():
            schema_errors.append(f"missing rubric: {rubric}")
    for case in cases:
        fixture_path = root / case.fixture_ref
        if not fixture_path.is_file():
            missing_fixtures.append(case.id)
        elif hashlib.sha256(fixture_path.read_bytes()).hexdigest() != case.fixture_sha256:
            schema_errors.append(f"{case.id}: fixture hash mismatch")
        if case.skill_id not in package_ids:
            schema_errors.append(f"{case.id}: unknown skill_id")
    declared_mismatch = [
        package.skill_id
        for package in manifest.packages
        if counts[package.skill_id] != package.case_count
    ]
    schema_errors.extend(f"{item}: declared case_count mismatch" for item in declared_mismatch)
    try:
        split_manifest = SplitManifest.model_validate_json(
            (root / manifest.split_manifest_ref).read_text(encoding="utf-8")
        )
        expected_by_split = {
            split: {case.id for case in cases if case.split == split}
            for split in ("train", "validation", "test")
        }
        for split, expected in expected_by_split.items():
            actual = set(getattr(split_manifest, split))
            if actual != expected:
                schema_errors.append(f"split manifest mismatch: {split}")
    except (OSError, ValidationError) as error:
        schema_errors.append(f"invalid split manifest: {error}")
    valid = (
        len(manifest.packages) >= 3
        and len(cases) >= 150
        and all(count >= 40 for count in counts.values())
        and not schema_errors
        and not missing_fixtures
    )
    return {
        "valid": valid,
        "packages": len(manifest.packages),
        "cases": len(cases),
        "cases_by_package": dict(sorted(counts.items())),
        "schema_errors": len(schema_errors),
        "schema_error_details": schema_errors,
        "missing_fixture": len(missing_fixtures),
        "missing_fixture_cases": missing_fixtures,
    }


def audit_licenses(root: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    cases = load_cases(root, manifest)
    invalid_packages = [
        package.skill_id
        for package in manifest.packages
        if package.license not in ALLOWED_LICENSES
        or not (root / package.provenance_ref).is_file()
        or not (root / package.skill_path / "LICENSE").is_file()
    ]
    invalid_cases = [
        case.id
        for case in cases
        if case.provenance.license not in ALLOWED_LICENSES or not case.provenance.reference
    ]
    unknown = len(invalid_packages) + len(invalid_cases)
    return {
        "valid": unknown == 0,
        "packages_checked": len(manifest.packages),
        "cases_checked": len(cases),
        "unknown_or_incompatible_license": unknown,
        "invalid_packages": invalid_packages,
        "invalid_cases": invalid_cases,
        "provenance_complete": unknown == 0,
    }


def _case_signature(case: TaskCase) -> str:
    canonical = json.dumps(
        {"prompt": " ".join(case.prompt.lower().split()), "input": case.input},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def audit_leakage(root: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    cases = load_cases(root, manifest)
    intersections = validate_group_isolation(cases)
    signature_splits: dict[str, set[str]] = defaultdict(set)
    signature_cases: dict[str, list[str]] = defaultdict(list)
    for case in cases:
        signature = _case_signature(case)
        signature_splits[signature].add(case.split)
        signature_cases[signature].append(case.id)
    duplicates = [
        signature_cases[key] for key, splits in signature_splits.items() if len(splits) > 1
    ]
    intersection_counts = {key: len(value) for key, value in intersections.items()}
    leakage_count = sum(intersection_counts.values())
    return {
        "valid": leakage_count == 0 and not duplicates,
        "cases_checked": len(cases),
        "leakage_group_intersections": intersection_counts,
        "leakage_group_intersection_total": leakage_count,
        "near_duplicate_cross_split_warnings": len(duplicates),
        "near_duplicate_case_groups": duplicates,
    }


def score_composition(root: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    cases = load_cases(root, manifest)
    deterministic_minimum = min(case.deterministic_weight for case in cases)
    judge_maximum = max(case.judge_weight for case in cases)
    return {
        "valid": deterministic_minimum >= 0.7 and judge_maximum <= 0.3,
        "cases_checked": len(cases),
        "deterministic_weight_min": deterministic_minimum,
        "judge_weight_max": judge_maximum,
        "exceptions": [],
    }
