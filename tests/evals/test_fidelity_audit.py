from pathlib import Path

from gepase.evals.engine import audit_fidelity


def test_valid_evidence_fixture_directory_has_no_tier_violations() -> None:
    root = Path("tests/fixtures/evidence")
    result = audit_fidelity(root.glob("*.json"))
    assert result == {
        "valid": True,
        "records": 4,
        "observed_field_violations": 0,
        "provenance_missing": 0,
        "tier_upgrade_without_evidence": 0,
        "invalid_records": 0,
    }
