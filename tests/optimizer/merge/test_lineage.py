from __future__ import annotations

from pathlib import Path

from gepase.optimizer.candidate import PackageCandidate
from gepase.optimizer.merge.fixture_suite import run_merge_fixture_suite

ROOT = Path(__file__).resolve().parents[3]


def test_multi_parent_lineage_is_materialized() -> None:
    result = run_merge_fixture_suite(ROOT, ROOT / "tests/fixtures/merge")
    assert result["valid"]
    candidate = PackageCandidate.model_validate(result["merged_candidate"])
    assert len(candidate.parent_ids) == 2
    assert candidate.operator == "package_aware_pareto_merge"
