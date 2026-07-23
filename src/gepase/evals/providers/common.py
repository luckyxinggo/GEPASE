"""Shared provider normalization helpers."""

from __future__ import annotations

from gepase.evals.evidence import (
    EvaluationRecord,
    EvidenceProvenance,
    TraceCompleteness,
)
from gepase.evals.schema import EvidenceTier
from gepase.evals.work_items import EvalWorkItem, WorkSubmission, canonical_hash


def delegated_record(item: EvalWorkItem, submission: WorkSubmission) -> EvaluationRecord:
    payload = {
        "work_id": item.work_id,
        "submission_id": submission.submission_id,
        "tier": item.evidence_tier.value,
    }
    return EvaluationRecord(
        record_id=f"record-{canonical_hash(payload)[:24]}",
        work_id=item.work_id,
        pair_id=item.pair_id,
        task_id=item.task_id,
        skill_id=item.skill_id,
        variant=item.variant,
        evidence_tier=item.evidence_tier,
        candidate_snapshot_hash=item.candidate_snapshot_hash,
        prompt_hash=item.pairing.prompt_hash,
        fixture_hash=item.pairing.fixture_hash,
        policy_hash=item.pairing.policy_hash,
        provider_snapshot=item.pairing.provider_snapshot,
        host_model_snapshot=item.pairing.host_model_snapshot,
        seed=item.pairing.seed,
        planned_trace=submission.planned_trace,
        observed_trace=submission.observed_trace,
        trace_completeness=(
            TraceCompleteness.PLANNED_ONLY
            if not submission.observed_trace and submission.planned_trace
            else TraceCompleteness.COMPLETE
            if submission.observed_trace
            else TraceCompleteness.NONE
        ),
        artifact_root=submission.artifact_root,
        artifacts=submission.artifacts,
        score=(
            submission.proxy_score
            if item.evidence_tier is EvidenceTier.E1_SIMULATED
            else None
        ),
        usage=submission.usage,
        uncertainty=submission.uncertainty,
        provenance=EvidenceProvenance(
            origin=("simulation" if item.evidence_tier.value == "E1" else "agent-native"),
            provider_id=item.provider_id,
            host=submission.host,
            model=submission.model,
            host_task_id=submission.host_task_id,
            context_id=submission.context_id,
            submission_id=submission.submission_id,
            generated_by="gepase.eval.ingest",
        ),
        failure_kind=submission.failure_kind,
        failure_detail=submission.failure_detail,
    )
