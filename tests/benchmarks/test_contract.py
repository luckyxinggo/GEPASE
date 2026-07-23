import json
from collections import Counter
from pathlib import Path

from gepase.benchmarks.audit import audit_leakage, audit_licenses, validate_benchmark
from gepase.benchmarks.loader import load_cases, load_manifest

MANIFEST = Path("benchmarks/manifest-draft.json")


def test_benchmark_has_three_balanced_non_toy_packages() -> None:
    result = validate_benchmark(Path.cwd(), MANIFEST)
    assert result["valid"] is True
    assert result["cases"] == 150
    assert set(result["cases_by_package"].values()) == {50}


def test_group_aware_split_and_license_provenance() -> None:
    leakage = audit_leakage(Path.cwd(), MANIFEST)
    licenses = audit_licenses(Path.cwd(), MANIFEST)
    assert leakage["valid"] is True
    assert leakage["near_duplicate_cross_split_warnings"] == 0
    assert licenses["valid"] is True
    manifest = load_manifest(MANIFEST)
    cases = load_cases(Path.cwd(), manifest)
    assert Counter(case.split for case in cases) == {
        "train": 90,
        "validation": 30,
        "test": 30,
    }


def test_blind_rubric_excludes_method_identity() -> None:
    rubric = json.loads(
        Path("benchmarks/rubrics/blind-quality-v1.json").read_text(encoding="utf-8")
    )
    assert set(rubric["blind_fields"]) >= {
        "candidate_id",
        "variant",
        "parents",
        "optimizer",
        "expected_answer",
    }
    assert sum(item["weight"] for item in rubric["dimensions"]) == 1.0
