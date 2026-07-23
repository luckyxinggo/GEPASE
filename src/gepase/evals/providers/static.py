"""E0 static package evidence."""

from __future__ import annotations

from pathlib import Path

from gepase.evals.evidence import (
    EvaluationRecord,
    EvidenceProvenance,
    TraceCompleteness,
    UsageRecord,
)
from gepase.evals.providers.base import ProviderCapabilities
from gepase.evals.schema import EvidenceTier
from gepase.evals.work_items import EvalWorkItem, WorkSubmission, canonical_hash


class StaticProvider:
    capabilities = ProviderCapabilities(
        provider_id="static-v1",
        evidence_tiers=(EvidenceTier.E0_STATIC,),
        capabilities=("package_structure",),
    )

    def validate_submission(self, item: EvalWorkItem, submission: WorkSubmission) -> None:
        raise ValueError("StaticProvider does not ingest delegated submissions")

    def normalize_evidence(
        self, item: EvalWorkItem, submission: WorkSubmission
    ) -> EvaluationRecord:
        raise ValueError("StaticProvider does not normalize delegated submissions")

    def evaluate(self, item: EvalWorkItem, project_root: Path) -> EvaluationRecord:
        skill_present = item.skill_ref is not None and (
            project_root / item.skill_ref / "SKILL.md"
        ).is_file()
        payload = {"work": item.work_id, "skill_present": skill_present}
        return EvaluationRecord(
            record_id=f"record-{canonical_hash(payload)[:24]}",
            work_id=item.work_id,
            pair_id=item.pair_id,
            task_id=item.task_id,
            skill_id=item.skill_id,
            variant=item.variant,
            evidence_tier=EvidenceTier.E0_STATIC,
            candidate_snapshot_hash=item.candidate_snapshot_hash,
            prompt_hash=item.pairing.prompt_hash,
            fixture_hash=item.pairing.fixture_hash,
            policy_hash=item.pairing.policy_hash,
            provider_snapshot=item.pairing.provider_snapshot,
            host_model_snapshot=item.pairing.host_model_snapshot,
            seed=item.pairing.seed,
            trace_completeness=TraceCompleteness.NONE,
            score=1.0 if skill_present else 0.0,
            usage=UsageRecord(),
            provenance=EvidenceProvenance(
                origin="static",
                provider_id=self.capabilities.provider_id,
                generated_by="StaticProvider.evaluate",
            ),
        )
