"""E3 deterministic assertion provider."""

from __future__ import annotations

from pathlib import Path

from gepase.evals.assertions import AssertionContext, evaluate_assertion
from gepase.evals.evidence import (
    AssertionResult,
    EvaluationRecord,
    EvidenceProvenance,
    TraceCompleteness,
    record_score,
)
from gepase.evals.providers.artifact import ArtifactProvider
from gepase.evals.schema import EvidenceTier, TaskCase
from gepase.evals.work_items import canonical_hash


class AssertionProvider:
    def __init__(self) -> None:
        self.artifacts = ArtifactProvider()

    def evaluate(
        self,
        project_root: Path,
        case: TaskCase,
        source: EvaluationRecord,
    ) -> EvaluationRecord:
        if source.evidence_tier is not EvidenceTier.E2_DELEGATED:
            raise ValueError("E3 assertion requires an E2 source record")
        if not source.artifacts:
            raise ValueError("E2 source has no artifacts")
        submission_id = source.provenance.submission_id
        if submission_id is None:
            raise ValueError("E2 source has no submission provenance")
        if source.artifact_root is None:
            raise ValueError("E2 source has no artifact_root")
        artifact_root = (project_root / source.artifact_root).resolve()
        if not artifact_root.is_relative_to(project_root.resolve()) or not artifact_root.is_dir():
            raise ValueError("E2 artifact_root is unavailable")
        context = AssertionContext(artifact_root)
        results = tuple(
            AssertionResult(
                assertion_id=spec.assertion_id,
                family=spec.family,
                passed=evaluate_assertion(spec, context),
                weight=spec.weight,
            )
            for spec in case.assertions
        )
        payload = {"source": source.record_id, "assertions": [item.passed for item in results]}
        provenance = source.provenance
        return EvaluationRecord(
            record_id=f"record-{canonical_hash(payload)[:24]}",
            work_id=f"{source.work_id}-assertions",
            pair_id=source.pair_id,
            task_id=source.task_id,
            skill_id=source.skill_id,
            variant=source.variant,
            evidence_tier=EvidenceTier.E3_EXECUTABLE,
            candidate_snapshot_hash=source.candidate_snapshot_hash,
            prompt_hash=source.prompt_hash,
            fixture_hash=source.fixture_hash,
            policy_hash=source.policy_hash,
            provider_snapshot="assertion-v1",
            host_model_snapshot=source.host_model_snapshot,
            seed=source.seed,
            planned_trace=source.planned_trace,
            observed_trace=source.observed_trace,
            trace_completeness=TraceCompleteness.COMPLETE,
            artifact_root=source.artifact_root,
            artifacts=source.artifacts,
            assertion_results=results,
            score=record_score(results),
            usage=source.usage,
            uncertainty=0,
            provenance=EvidenceProvenance(
                origin="assertion",
                provider_id="assertion-v1",
                host=provenance.host,
                model=provenance.model,
                host_task_id=provenance.host_task_id,
                submission_id=submission_id,
                generated_by="AssertionProvider.evaluate",
            ),
            source_record_refs=(source.record_id,),
        )
