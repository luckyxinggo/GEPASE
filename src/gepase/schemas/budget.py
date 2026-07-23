"""Multi-axis work, token, and time budget contracts."""

from __future__ import annotations

import threading
import time
from enum import StrEnum
from typing import ClassVar

from pydantic import Field

from gepase.schemas.common import FrozenModel


class StopReason(StrEnum):
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    HUMAN_CHECKPOINT_REQUIRED = "human_checkpoint_required"
    FAILED = "failed"


class BudgetContract(FrozenModel):
    max_proposals: int = Field(ge=0)
    max_metric_calls: int = Field(ge=0)
    max_e1_calls: int = Field(ge=0)
    max_e2_e3_calls: int = Field(ge=0)
    max_reflection_calls: int = Field(ge=0)
    max_tokens: int = Field(ge=0)
    timeout_seconds: float = Field(gt=0)


class BudgetUsage(FrozenModel):
    proposals: int = 0
    metric_calls: int = 0
    e1_calls: int = 0
    e2_e3_calls: int = 0
    reflection_calls: int = 0
    tokens: int = 0
    elapsed_seconds: float = 0.0


class BudgetExceeded(RuntimeError):
    def __init__(self, axis: str) -> None:
        super().__init__(f"budget exhausted: {axis}")
        self.axis = axis


class BudgetLedger:
    """Thread-safe atomic reservation; equality with a limit is allowed."""

    _AXES: ClassVar[dict[str, str]] = {
        "proposals": "max_proposals",
        "metric_calls": "max_metric_calls",
        "e1_calls": "max_e1_calls",
        "e2_e3_calls": "max_e2_e3_calls",
        "reflection_calls": "max_reflection_calls",
        "tokens": "max_tokens",
    }

    def __init__(self, contract: BudgetContract, usage: BudgetUsage | None = None) -> None:
        self.contract = contract
        self._values = (usage or BudgetUsage()).model_dump()
        self._started = time.monotonic() - float(self._values.pop("elapsed_seconds", 0))
        self._lock = threading.Lock()

    def charge(self, **increments: int | float) -> BudgetUsage:
        with self._lock:
            elapsed = time.monotonic() - self._started
            if elapsed > self.contract.timeout_seconds:
                raise BudgetExceeded("timeout_seconds")
            for axis, value in increments.items():
                if axis not in self._AXES or value < 0:
                    raise ValueError(f"invalid budget increment: {axis}={value}")
                limit = float(getattr(self.contract, self._AXES[axis]))
                if float(self._values[axis]) + float(value) > limit:
                    raise BudgetExceeded(axis)
            for axis, value in increments.items():
                self._values[axis] += value
            return self.snapshot()

    def snapshot(self) -> BudgetUsage:
        return BudgetUsage(**self._values, elapsed_seconds=time.monotonic() - self._started)

    def within_contract(self) -> bool:
        usage = self.snapshot()
        return (
            all(
                float(getattr(usage, axis)) <= float(getattr(self.contract, limit))
                for axis, limit in self._AXES.items()
            )
            and usage.elapsed_seconds <= self.contract.timeout_seconds
        )
