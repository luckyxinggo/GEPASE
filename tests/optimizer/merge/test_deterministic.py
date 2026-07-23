from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from gepase.optimizer.merge.fixture_suite import run_merge_fixture_suite

ROOT = Path(__file__).resolve().parents[3]


def test_fixture_merge_is_deterministic_and_retains_both_parents() -> None:
    result = run_merge_fixture_suite(ROOT, ROOT / "tests/fixtures/merge")
    result = cast(dict[str, Any], result)
    assert result["valid"]
    assert result["determinism"]["patch_hash_equal"]
    assert result["determinism"]["candidate_hash_equal"]
    assert result["determinism"]["contribution_map_equal"]
    assert result["complement_retention"]["a_only_assertion"]
    assert result["complement_retention"]["b_only_assertion"]
    assert result["complement_retention"]["s7_verdict"] == "accepted"
