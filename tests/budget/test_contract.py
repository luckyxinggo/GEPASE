from __future__ import annotations

import pytest

from gepase.schemas.budget import BudgetContract, BudgetExceeded, BudgetLedger


def contract(**updates: object) -> BudgetContract:
    values: dict[str, object] = {
        "max_proposals": 1,
        "max_metric_calls": 2,
        "max_e1_calls": 2,
        "max_e2_e3_calls": 0,
        "max_reflection_calls": 1,
        "max_tokens": 100,
        "timeout_seconds": 30,
    }
    values.update(updates)
    return BudgetContract.model_validate(values)


def test_boundary_is_inclusive_and_next_reservation_is_atomic() -> None:
    ledger = BudgetLedger(contract())
    ledger.charge(proposals=1, reflection_calls=1, metric_calls=2, e1_calls=2, tokens=100)
    assert ledger.within_contract()
    before = ledger.snapshot()
    with pytest.raises(BudgetExceeded, match="proposals"):
        ledger.charge(proposals=1, tokens=1)
    assert ledger.snapshot().proposals == before.proposals
    assert ledger.snapshot().tokens == before.tokens


@pytest.mark.parametrize(
    "axis", ["proposals", "metric_calls", "e1_calls", "reflection_calls", "tokens"]
)
def test_each_axis_forces_a_typed_stop(axis: str) -> None:
    ledger = BudgetLedger(contract(**{f"max_{axis}": 0}))
    with pytest.raises(BudgetExceeded) as raised:
        ledger.charge(**{axis: 1})
    assert raised.value.axis == axis
