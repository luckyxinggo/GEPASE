from __future__ import annotations

from pathlib import Path

from gepase.optimizer.diagnostics import adapter_contract_diagnostic, asi_audit_diagnostic


def test_adapter_contract_preserves_failed_rows_and_matches_frontiers() -> None:
    result = adapter_contract_diagnostic(Path.cwd())

    assert result["valid"] is True
    assert result["work_items"] == 3
    assert result["evaluation_rows"] == 3
    assert result["failed_rows"] == 1
    assert result["criteria"]["sync_step_frontier_equal"] is True


def test_asi_audit_has_complete_tiered_evidence_without_trace_confusion() -> None:
    result = asi_audit_diagnostic(Path.cwd())

    assert result["valid"] is True
    assert result["required_evidence_coverage"] == 1.0
    assert result["planned_observed_confusion"] == 0
    assert result["token_estimate"] <= result["token_budget"]
