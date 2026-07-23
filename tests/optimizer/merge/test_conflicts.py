from __future__ import annotations

from pathlib import Path

from gepase.optimizer.merge.conflicts import detect_conflicts
from gepase.optimizer.merge.fixture_suite import _contribution, _operation
from gepase.optimizer.merge.models import MergeConflictKind


def test_all_required_conflict_kinds_are_detected() -> None:
    for kind in MergeConflictKind:
        conflicts = detect_conflicts(
            (
                _contribution("candidate-a", _operation(kind, "a")),
                _contribution("candidate-b", _operation(kind, "b")),
            )
        )
        assert [item.kind for item in conflicts] == [kind]


def test_complement_fixture_has_expected_contract_file() -> None:
    fixture = Path(__file__).resolve().parents[2] / "fixtures/merge/expected_conflicts.json"
    assert fixture.is_file()
