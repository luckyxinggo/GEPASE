"""Strict paired comparability and aggregation."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from gepase.evals.errors import PairNotComparable
from gepase.evals.evidence import EvaluationRecord

COMPARABLE_FIELDS = (
    "pair_id",
    "task_id",
    "skill_id",
    "evidence_tier",
    "prompt_hash",
    "fixture_hash",
    "policy_hash",
    "provider_snapshot",
    "host_model_snapshot",
    "seed",
)


def compare_pair(first: EvaluationRecord, second: EvaluationRecord) -> dict[str, Any]:
    differences = {
        field: [
            getattr(first, field).value
            if hasattr(getattr(first, field), "value")
            else getattr(first, field),
            getattr(second, field).value
            if hasattr(getattr(second, field), "value")
            else getattr(second, field),
        ]
        for field in COMPARABLE_FIELDS
        if getattr(first, field) != getattr(second, field)
    }
    if first.variant == second.variant:
        differences["variant"] = [first.variant, second.variant]
    result = {
        "pair_comparable": not differences,
        "config_diff": differences,
        "allowed_differences": ["variant", "candidate_snapshot_hash"],
        "variants": sorted((first.variant, second.variant)),
    }
    return result


def require_comparable(first: EvaluationRecord, second: EvaluationRecord) -> None:
    result = compare_pair(first, second)
    if not result["pair_comparable"]:
        raise PairNotComparable(str(result["config_diff"]))


def aggregate_pairs(records: list[EvaluationRecord]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[EvaluationRecord]] = defaultdict(list)
    for record in records:
        grouped[(record.pair_id, record.evidence_tier.value)].append(record)
    pairs: list[dict[str, Any]] = []
    rejected = 0
    for (pair_id, tier), values in sorted(grouped.items()):
        by_variant = {record.variant: record for record in values}
        if "no-skill" not in by_variant or "original" not in by_variant:
            continue
        comparison = compare_pair(by_variant["no-skill"], by_variant["original"])
        if not comparison["pair_comparable"]:
            rejected += 1
            continue
        baseline = by_variant["no-skill"].score
        original = by_variant["original"].score
        pairs.append(
            {
                "pair_id": pair_id,
                "tier": tier,
                "task_id": by_variant["original"].task_id,
                "skill_id": by_variant["original"].skill_id,
                "no_skill_score": baseline,
                "original_score": original,
                "delta": (
                    original - baseline
                    if original is not None and baseline is not None
                    else None
                ),
            }
        )
    return {"pairs": pairs, "pair_count": len(pairs), "rejected_incomparable": rejected}
