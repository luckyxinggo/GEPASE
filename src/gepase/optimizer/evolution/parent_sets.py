"""Enumerate train-only same-lineage parent sets from a frozen breeding snapshot."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections import Counter, defaultdict

from pydantic import Field

from gepase.optimizer.evolution.lineage import CandidateAncestryIndex
from gepase.optimizer.evolution.models import (
    BreedingSnapshot,
    MergeParentCandidate,
    MergeParentSetSnapshot,
)
from gepase.optimizer.merge.parent_contract import MergeParentContractValidator
from gepase.schemas.common import FrozenModel


def _canonical_hash(value: object) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _contribution_keys(candidate: MergeParentCandidate) -> frozenset[str]:
    contribution = candidate.contribution
    return frozenset(
        (
            *(f"task:{key}" for key in contribution.task_keys),
            *(f"objective:{key}" for key in contribution.objective_keys),
            *(f"component:{key}" for key in contribution.component_ids),
            *(f"closure:{key}" for key in contribution.closure_ids),
        )
    )


class RankedParentSet(FrozenModel):
    parent_set: MergeParentSetSnapshot
    pareto_rank: int = Field(ge=1)
    contribution_union_size: int = Field(ge=0)
    exclusive_contribution_size: int = Field(ge=0)
    contribution_overlap_size: int = Field(ge=0)
    lineage_distance: int = Field(ge=0)


class ParentSetEnumerationReport(FrozenModel):
    schema_version: str = "1.0.0"
    breeding_snapshot_id: str
    ranked_parent_sets: tuple[RankedParentSet, ...]
    merge_compatible_parent_set_count: int = Field(ge=0)
    merge_compatible_package_count: int = Field(ge=0)
    cross_package_pairs_observed: int = Field(ge=0)
    cross_package_pairs_counted_as_merge: int = Field(default=0, ge=0)
    rejected_pair_count: int = Field(ge=0)
    rejection_reasons: dict[str, int]
    consumer_validated: bool


def _pareto_layers(
    rows: list[tuple[MergeParentSetSnapshot, int, int, int, int]],
) -> list[tuple[MergeParentSetSnapshot, int, int, int, int, int]]:
    """Rank by complement breadth/exclusivity and shorter shared lineage distance."""

    remaining = list(rows)
    ranked: list[tuple[MergeParentSetSnapshot, int, int, int, int, int]] = []
    rank = 1
    while remaining:
        layer: list[tuple[MergeParentSetSnapshot, int, int, int, int]] = []
        for row in remaining:
            _, union_size, exclusive_size, overlap_size, distance = row
            dominated = any(
                (
                    other_union >= union_size
                    and other_exclusive >= exclusive_size
                    and other_overlap <= overlap_size
                    and other_distance <= distance
                    and (
                        other_union > union_size
                        or other_exclusive > exclusive_size
                        or other_overlap < overlap_size
                        or other_distance < distance
                    )
                )
                for (
                    _,
                    other_union,
                    other_exclusive,
                    other_overlap,
                    other_distance,
                ) in remaining
                if _ is not row[0]
            )
            if not dominated:
                layer.append(row)
        for row in sorted(layer, key=lambda item: item[0].parent_set_id):
            ranked.append((*row, rank))
            remaining.remove(row)
        rank += 1
    return ranked


def enumerate_parent_sets(
    snapshot: BreedingSnapshot,
    *,
    parent_count: int = 2,
) -> ParentSetEnumerationReport:
    if parent_count < 2:
        raise ValueError("merge-parent enumeration requires at least two parents")
    lineage = CandidateAncestryIndex(snapshot.lineage)
    validator = MergeParentContractValidator(lineage)
    by_package: defaultdict[str, list[MergeParentCandidate]] = defaultdict(list)
    for candidate in snapshot.candidates:
        by_package[candidate.identity.package_id].append(candidate)

    cross_package_pairs = sum(
        1
        for left, right in itertools.combinations(snapshot.candidates, 2)
        if left.identity.package_id != right.identity.package_id
    )
    rejected = 0
    reasons: Counter[str] = Counter()
    compatible_rows: list[tuple[MergeParentSetSnapshot, int, int, int, int]] = []
    for package_id, candidates in sorted(by_package.items()):
        del package_id  # Identity is recomputed by the consumer validator.
        for parents in itertools.combinations(
            sorted(candidates, key=lambda item: item.identity.candidate_id),
            parent_count,
        ):
            compatibility = validator.validate(parents)
            if not compatibility.merge_input_compatible:
                rejected += 1
                reasons.update(reason.value for reason in compatibility.reason_codes)
                continue
            evidence_refs = tuple(
                sorted({ref for parent in parents for ref in parent.train_evidence_refs})
            )
            identity = {
                "breeding_snapshot_id": snapshot.snapshot_id,
                "selection_config_hash": snapshot.selection_config_hash,
                "parents": [parent.identity.candidate_id for parent in parents],
                "content": [parent.identity.content_hash for parent in parents],
                "evidence": evidence_refs,
            }
            parent_set_id = f"merge-parent-set-{_canonical_hash(identity)[:24]}"
            parent_set = MergeParentSetSnapshot(
                parent_set_id=parent_set_id,
                parents=tuple(parents),
                lineage=snapshot.lineage,
                selection_config_hash=snapshot.selection_config_hash,
                train_selection_evidence_refs=evidence_refs,
                compatibility_report=compatibility,
            )
            keys = [_contribution_keys(parent) for parent in parents]
            union = set().union(*keys)
            overlap = set.intersection(*(set(item) for item in keys))
            exclusive = sum(
                len(set(item) - set().union(*(set(other) for other in keys if other is not item)))
                for item in keys
            )
            lca = compatibility.lca_candidate_id
            distance = (
                sum(
                    len(lineage.path_from_ancestor(lca, parent.identity.candidate_id) or ()) - 1
                    for parent in parents
                )
                if lca is not None
                else 10**9
            )
            compatible_rows.append((parent_set, len(union), exclusive, len(overlap), distance))
    ranked = tuple(
        RankedParentSet(
            parent_set=row[0],
            pareto_rank=row[5],
            contribution_union_size=row[1],
            exclusive_contribution_size=row[2],
            contribution_overlap_size=row[3],
            lineage_distance=row[4],
        )
        for row in _pareto_layers(compatible_rows)
    )
    packages = {item.parent_set.parents[0].identity.package_id for item in ranked}
    return ParentSetEnumerationReport(
        breeding_snapshot_id=snapshot.snapshot_id,
        ranked_parent_sets=ranked,
        merge_compatible_parent_set_count=len(ranked),
        merge_compatible_package_count=len(packages),
        cross_package_pairs_observed=cross_package_pairs,
        cross_package_pairs_counted_as_merge=0,
        rejected_pair_count=rejected,
        rejection_reasons=dict(sorted(reasons.items())),
        consumer_validated=all(
            item.parent_set.compatibility_report is not None
            and item.parent_set.compatibility_report.merge_input_compatible
            for item in ranked
        ),
    )


def parent_set_contract_audit(
    report: ParentSetEnumerationReport,
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "valid": (
            report.merge_compatible_parent_set_count >= 1
            and report.merge_compatible_package_count >= 1
            and report.cross_package_pairs_counted_as_merge == 0
            and report.consumer_validated
        ),
        "all_compatible_sets_pass_contract": report.consumer_validated,
        **report.model_dump(mode="json", exclude={"ranked_parent_sets"}),
        "parent_sets": [
            {
                "parent_set_id": item.parent_set.parent_set_id,
                "package_id": item.parent_set.parents[0].identity.package_id,
                "parent_candidate_ids": [
                    parent.identity.candidate_id for parent in item.parent_set.parents
                ],
                "pareto_rank": item.pareto_rank,
                "lca_candidate_id": (
                    item.parent_set.compatibility_report.lca_candidate_id
                    if item.parent_set.compatibility_report
                    else None
                ),
            }
            for item in report.ranked_parent_sets
        ],
    }
