from __future__ import annotations

from pathlib import Path

from gepase.optimizer.diagnostics import checkpoint_resume_diagnostic
from gepase.optimizer.gepa_compat import assert_compatible_gepa


def test_official_gepa_version_and_state_contract_are_pinned() -> None:
    result = assert_compatible_gepa()

    assert result == {
        "version": "0.1.4",
        "state_schema_version": 5,
        "forked_upstream_files": 0,
    }


def test_interrupted_candidate_store_matches_uninterrupted_frontier() -> None:
    result = checkpoint_resume_diagnostic(Path.cwd())

    assert result["valid"] is True
    assert result["resumed"]["candidate_ids"] == result["uninterrupted"]["candidate_ids"]
    assert result["resumed"]["budget"]["proposals"] == 4
    assert result["resumed"]["counts"]["candidates"] == 5
