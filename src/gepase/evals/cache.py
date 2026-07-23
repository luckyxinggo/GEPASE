"""Content-addressed evaluation cache identity."""

from __future__ import annotations

from gepase.evals.work_items import EvalWorkItem, canonical_hash


def cache_key_for(item: EvalWorkItem) -> str:
    return canonical_hash(
        {
            "task_id": item.task_id,
            "skill_id": item.skill_id,
            "variant": item.variant,
            "evidence_tier": item.evidence_tier.value,
            "candidate_snapshot_hash": item.candidate_snapshot_hash,
            "prompt_hash": item.pairing.prompt_hash,
            "fixture_hash": item.pairing.fixture_hash,
            "policy_hash": item.pairing.policy_hash,
            "provider_snapshot": item.pairing.provider_snapshot,
            "host_model_snapshot": item.pairing.host_model_snapshot,
            "seed": item.pairing.seed,
        }
    )
