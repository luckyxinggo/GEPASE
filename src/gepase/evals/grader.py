"""Blind grader payload contract; optimizer and variant identity are excluded."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from gepase.evals.evidence import EvaluationRecord
from gepase.schemas.common import ArtifactRef, FrozenModel


class BlindGraderWorkItem(FrozenModel):
    grader_work_id: str
    task_id: str
    task_prompt: str
    artifacts: tuple[ArtifactRef, ...]
    rubric: dict[str, Any]


class GraderResult(FrozenModel):
    grader_work_id: str
    score: float = Field(ge=0, le=1)
    dimension_scores: dict[str, float]
    feedback: str


def blind_grader_work(
    record: EvaluationRecord,
    *,
    task_prompt: str,
    rubric: dict[str, Any],
) -> BlindGraderWorkItem:
    return BlindGraderWorkItem(
        grader_work_id=f"grader-{record.record_id}",
        task_id=record.task_id,
        task_prompt=task_prompt,
        artifacts=record.artifacts,
        rubric=rubric,
    )
