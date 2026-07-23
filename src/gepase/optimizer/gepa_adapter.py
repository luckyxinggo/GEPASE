"""S2 evaluation adapter for package candidates and official GEPA-compatible scores."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from pydantic import Field, model_validator

from gepase.benchmarks.calibration import deterministic_plan_quality
from gepase.evals.engine import MultiFidelityEvalEngine
from gepase.evals.evidence import EvaluationRecord, TraceStep
from gepase.evals.schema import EvidenceTier, TaskCase
from gepase.evals.scores import TaskScoreVector
from gepase.evals.work_items import EvalWorkItem
from gepase.optimizer.candidate import PackageCandidate
from gepase.schemas.common import ArtifactRef, FrozenModel

_TIER_RANK = {
    EvidenceTier.E0_STATIC: 0,
    EvidenceTier.E1_SIMULATED: 1,
    EvidenceTier.E2_DELEGATED: 2,
    EvidenceTier.E3_EXECUTABLE: 3,
}


class CandidateEvaluationRow(FrozenModel):
    task_id: str
    record_id: str
    record_ref: str
    evidence_tier: EvidenceTier
    score: float = Field(ge=0, le=1)
    objective_scores: dict[str, float]
    task_score_vector: TaskScoreVector | None = None
    output: dict[str, Any]
    planned_trace: tuple[TraceStep, ...] = ()
    observed_trace: tuple[TraceStep, ...] = ()
    uncertainty: float = Field(ge=0, le=1)
    artifacts: tuple[ArtifactRef, ...] = ()
    assertion_feedback: tuple[dict[str, Any], ...] = ()
    provenance: dict[str, Any]
    failure_kind: str | None = None

    @model_validator(mode="after")
    def fidelity_boundary(self) -> CandidateEvaluationRow:
        if self.evidence_tier in {EvidenceTier.E0_STATIC, EvidenceTier.E1_SIMULATED}:
            if self.observed_trace:
                raise ValueError("E0/E1 evaluation row cannot contain observed trace")
        if self.evidence_tier in {EvidenceTier.E2_DELEGATED, EvidenceTier.E3_EXECUTABLE}:
            required = ("host", "model", "host_task_id", "submission_id")
            if any(not self.provenance.get(key) for key in required):
                raise ValueError("E2/E3 evaluation row requires Agent provenance")
        return self


class CandidateEvaluation(FrozenModel):
    schema_version: str = "1.0.0"
    evaluation_id: str
    candidate_id: str
    candidate_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    split: str
    requested_tier: EvidenceTier
    rows: tuple[CandidateEvaluationRow, ...]
    mean_score: float = Field(ge=0, le=1)
    objective_means: dict[str, float]
    failed_tasks: tuple[str, ...] = ()


class GEPASEAdapter:
    """Plan externally executable work and normalize returned S2 evidence.

    This adapter never invokes an Agent Runtime or provider. It only talks to the S2 ledger.
    """

    def __init__(self, project_root: Path, manifest: Path, eval_run_dir: Path) -> None:
        self.project_root = project_root.resolve()
        self.manifest = manifest
        self.eval_run_dir = eval_run_dir

    def plan_evaluations(
        self,
        batch: list[TaskCase],
        candidate: PackageCandidate,
        *,
        split: str,
        tier: EvidenceTier,
        candidate_ref: str,
        host: str,
        model: str,
        seed: int,
    ) -> tuple[EvalWorkItem, ...]:
        if tier is EvidenceTier.E3_EXECUTABLE:
            raise ValueError("E3 is derived from E2 assertions and cannot be directly planned")
        ids = {case.id for case in batch}
        if not ids:
            raise ValueError("evaluation batch cannot be empty")
        with MultiFidelityEvalEngine(self.project_root, self.eval_run_dir) as engine:
            engine.plan_cases(
                self.manifest,
                splits=(split,),
                tiers=(tier,),
                variants=("candidate",),
                host=host,
                model=model,
                case_ids=ids,
                seed=seed,
                candidate_ref=candidate_ref,
                candidate_snapshot_hash=candidate.content_hash,
            )
            items = tuple(
                item
                for item in engine.ledger.work_items()
                if item.task_id in ids
                and item.candidate_snapshot_hash == candidate.content_hash
                and item.evidence_tier is tier
            )
        if len(items) != len(ids):
            raise RuntimeError("evaluation planning did not create one work item per task")
        return tuple(sorted(items, key=lambda item: item.task_id))

    def ingest_evidence(
        self,
        candidate: PackageCandidate,
        batch: list[TaskCase],
        *,
        split: str,
        requested_tier: EvidenceTier,
    ) -> CandidateEvaluation:
        task_ids = {case.id for case in batch}
        with MultiFidelityEvalEngine(self.project_root, self.eval_run_dir) as engine:
            records = [
                record
                for record in engine.ledger.records()
                if record.task_id in task_ids
                and record.candidate_snapshot_hash == candidate.content_hash
            ]
        return self.score_candidate(
            candidate,
            batch,
            records,
            split=split,
            requested_tier=requested_tier,
        )

    def score_candidate(
        self,
        candidate: PackageCandidate,
        batch: list[TaskCase],
        records: list[EvaluationRecord],
        *,
        split: str,
        requested_tier: EvidenceTier,
    ) -> CandidateEvaluation:
        by_task: defaultdict[str, list[EvaluationRecord]] = defaultdict(list)
        for record in records:
            by_task[record.task_id].append(record)
        rows: list[CandidateEvaluationRow] = []
        failures: list[str] = []
        for case in batch:
            available = by_task.get(case.id, [])
            if not available:
                raise RuntimeError(f"evaluation evidence missing task: {case.id}")
            record = max(available, key=lambda item: _TIER_RANK[item.evidence_tier])
            if (
                record.evidence_tier in {EvidenceTier.E2_DELEGATED, EvidenceTier.E3_EXECUTABLE}
                and record.task_score_vector is None
            ):
                raise RuntimeError(
                    f"functional evidence lacks TaskScoreVector: {case.id}; "
                    "assertion pass rate is not an overall Skill score"
                )
            score = (
                float(
                    deterministic_plan_quality(
                        record,
                        requested_output=case.input["requested_output"],
                        fixture_ref=case.fixture_ref,
                    )["score"]
                )
                if record.evidence_tier is EvidenceTier.E1_SIMULATED
                and record.failure_kind is None
                else float(record.score or 0)
            )
            if record.failure_kind is not None:
                failures.append(case.id)
            tokens = record.usage.input_tokens + record.usage.output_tokens
            objectives = (
                record.task_score_vector.objectives
                if record.task_score_vector is not None
                else {
                    "proxy_plan_quality": score,
                    "proxy_reliability": max(0.0, 1.0 - record.uncertainty),
                    "proxy_evidence_strength": _TIER_RANK[record.evidence_tier] / 3,
                    "proxy_token_efficiency": 1.0 / (1.0 + tokens / 1000),
                }
            )
            assertions = tuple(
                {
                    "assertion_id": item.assertion_id,
                    "family": item.family,
                    "passed": item.passed,
                    "detail": item.detail,
                }
                for item in record.assertion_results
            )
            rows.append(
                CandidateEvaluationRow(
                    task_id=case.id,
                    record_id=record.record_id,
                    record_ref=f"evals/records/{record.record_id}.json",
                    evidence_tier=record.evidence_tier,
                    score=score,
                    objective_scores=objectives,
                    task_score_vector=record.task_score_vector,
                    output={
                        "artifact_root": record.artifact_root,
                        "failure_detail": record.failure_detail,
                    },
                    planned_trace=record.planned_trace,
                    observed_trace=record.observed_trace,
                    uncertainty=record.uncertainty,
                    artifacts=record.artifacts,
                    assertion_feedback=assertions,
                    provenance=record.provenance.model_dump(mode="json"),
                    failure_kind=(record.failure_kind.value if record.failure_kind else None),
                )
            )
        objective_names = sorted({key for row in rows for key in row.objective_scores})
        objective_means = {
            key: mean(row.objective_scores[key] for row in rows) for key in objective_names
        }
        identity = f"{candidate.candidate_id}:{split}:{requested_tier.value}"
        import hashlib

        evaluation_id = f"evaluation-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
        return CandidateEvaluation(
            evaluation_id=evaluation_id,
            candidate_id=candidate.candidate_id,
            candidate_content_hash=candidate.content_hash,
            split=split,
            requested_tier=requested_tier,
            rows=tuple(rows),
            mean_score=mean(row.score for row in rows),
            objective_means=objective_means,
            failed_tasks=tuple(sorted(failures)),
        )
