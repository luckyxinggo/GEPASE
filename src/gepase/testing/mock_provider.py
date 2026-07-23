"""Deterministic provider used by S0 and offline contract tests."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class MockTask:
    task_id: str
    prompt: str
    expected: str


@dataclass(frozen=True)
class MockResult:
    task_id: str
    output: str
    passed: bool
    evidence_tier: str = "E0"


class MockEvidenceProvider:
    name = "deterministic-mock"

    def evaluate(self, task: MockTask) -> MockResult:
        digest = hashlib.sha256(task.prompt.encode()).hexdigest()[:12]
        output = f"{task.expected}:{digest}"
        return MockResult(task_id=task.task_id, output=output, passed=True)


def default_tasks() -> tuple[MockTask, ...]:
    return (
        MockTask(task_id="mock-uppercase", prompt="normalize alpha", expected="ALPHA"),
        MockTask(task_id="mock-table", prompt="render two deterministic rows", expected="ROWS=2"),
    )

