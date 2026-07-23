"""Consumer-side validation for S7.6 same-lineage merge parent sets."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from gepase.optimizer.evolution.lineage import CandidateAncestryIndex
from gepase.optimizer.evolution.models import (
    MergeCompatibilityReason,
    MergeCompatibilityReport,
    MergeEligibility,
    MergeParentCandidate,
    MergeParentSetSnapshot,
)


def _same(values: Iterable[object]) -> bool:
    materialized = tuple(values)
    return bool(materialized) and len(set(materialized)) == 1


class MergeParentContractValidator:
    """Recompute every merge-parent fact from explicit identities and DAG edges."""

    def __init__(self, lineage: CandidateAncestryIndex) -> None:
        self.lineage = lineage

    def validate(
        self,
        parents: Sequence[MergeParentCandidate],
    ) -> MergeCompatibilityReport:
        parent_ids = tuple(parent.identity.candidate_id for parent in parents)
        reasons: list[MergeCompatibilityReason] = []

        if len(parents) < 2:
            reasons.append(MergeCompatibilityReason.INSUFFICIENT_PARENTS)
        if any(parent.merge_eligibility is MergeEligibility.UNKNOWN_LEGACY for parent in parents):
            reasons.append(MergeCompatibilityReason.LEGACY_IDENTITY_UNKNOWN)
        elif any(parent.merge_eligibility is not MergeEligibility.ELIGIBLE for parent in parents):
            reasons.append(MergeCompatibilityReason.MERGE_ELIGIBILITY_NOT_READY)
        duplicate_candidate = len(parent_ids) != len(set(parent_ids))
        if duplicate_candidate:
            reasons.append(MergeCompatibilityReason.DUPLICATE_CANDIDATE)

        same_package = _same(parent.identity.package_id for parent in parents)
        same_source_package = _same(parent.identity.source_package_ref for parent in parents)
        same_snapshot = _same(parent.identity.source_snapshot_hash for parent in parents)
        same_root = _same(parent.identity.lineage_root_candidate_id for parent in parents)
        if parents and not same_package:
            reasons.append(MergeCompatibilityReason.CROSS_PACKAGE)
        if parents and not same_source_package:
            reasons.append(MergeCompatibilityReason.DIFFERENT_SOURCE_PACKAGE)
        if parents and not same_snapshot:
            reasons.append(MergeCompatibilityReason.DIFFERENT_SNAPSHOT)
        if parents and not same_root:
            reasons.append(MergeCompatibilityReason.DIFFERENT_LINEAGE_ROOT)

        indexed = set(self.lineage.candidate_ids)
        missing_lineage = any(candidate_id not in indexed for candidate_id in parent_ids)
        if missing_lineage:
            reasons.append(MergeCompatibilityReason.MISSING_LINEAGE_NODE)

        lca_candidate_id: str | None = None
        if parent_ids and not missing_lineage and not duplicate_candidate:
            lca_candidate_id = self.lineage.lowest_common_ancestor(parent_ids)
        lca_exists = lca_candidate_id is not None
        if len(parents) >= 2 and not lca_exists:
            reasons.append(MergeCompatibilityReason.LCA_MISSING)

        ancestor_relation = False
        if not missing_lineage:
            ancestor_relation = any(
                self.lineage.is_ancestor(left, right) or self.lineage.is_ancestor(right, left)
                for index, left in enumerate(parent_ids)
                for right in parent_ids[index + 1 :]
            )
        if ancestor_relation:
            reasons.append(MergeCompatibilityReason.ANCESTOR_DESCENDANT)

        first_children: tuple[str, ...] = ()
        if lca_candidate_id is not None:
            first_children = tuple(
                child
                for candidate_id in parent_ids
                if (child := self.lineage.first_divergent_child(lca_candidate_id, candidate_id))
                is not None
            )
        explicit_branches = tuple(parent.identity.branch_id for parent in parents)
        explicit_branch_roots = tuple(
            parent.identity.branch_root_candidate_id for parent in parents
        )
        different_branches = (
            len(first_children) == len(parents)
            and len(first_children) == len(set(first_children))
            and None not in explicit_branches
            and len(explicit_branches) == len(set(explicit_branches))
            and None not in explicit_branch_roots
            and len(explicit_branch_roots) == len(set(explicit_branch_roots))
        )
        if len(parents) >= 2 and not different_branches:
            reasons.append(MergeCompatibilityReason.SAME_BRANCH)

        content_hashes = tuple(parent.identity.content_hash for parent in parents)
        distinct_content = len(content_hashes) == len(set(content_hashes))
        if len(parents) >= 2 and not distinct_content:
            reasons.append(MergeCompatibilityReason.DUPLICATE_CONTENT)

        contributions = tuple(parent.contribution for parent in parents)
        contribution_nonempty = bool(contributions) and all(
            not contribution.is_empty for contribution in contributions
        )
        if not contribution_nonempty:
            reasons.append(MergeCompatibilityReason.EMPTY_CONTRIBUTION)
        contribution_signatures = tuple(contribution.signature for contribution in contributions)
        contribution_distinct = len(contribution_signatures) == len(set(contribution_signatures))
        if len(parents) >= 2 and not contribution_distinct:
            reasons.append(MergeCompatibilityReason.DUPLICATE_CONTRIBUTION)

        train_only = all(
            not any(
                marker in ref.casefold() for marker in ("validation", "/test", "test-", "held-out")
            )
            for parent in parents
            for ref in parent.train_evidence_refs
        )
        if not train_only:
            reasons.append(MergeCompatibilityReason.HELD_OUT_EVIDENCE)

        structural_pass = bool(parents) and all(parent.gate_0_1_passed for parent in parents)
        if not structural_pass:
            reasons.append(MergeCompatibilityReason.STRUCTURAL_GATE_FAILED)
        train_floor = bool(parents) and all(parent.train_floor_satisfied for parent in parents)
        if not train_floor:
            reasons.append(MergeCompatibilityReason.TRAIN_FLOOR_FAILED)

        claimed_ancestry_matches = not missing_lineage and all(
            self.lineage.root_to_candidate_chain(parent.identity.candidate_id)
            == parent.ancestor_chain
            for parent in parents
        )
        if parents and not claimed_ancestry_matches:
            reasons.append(MergeCompatibilityReason.CLAIMED_ANCESTRY_MISMATCH)

        ordered_reasons = tuple(dict.fromkeys(reasons))
        compatible = not ordered_reasons
        return MergeCompatibilityReport(
            parent_candidate_ids=parent_ids,
            merge_input_compatible=compatible,
            reason_codes=(
                (MergeCompatibilityReason.COMPATIBLE,) if compatible else ordered_reasons
            ),
            lca_candidate_id=lca_candidate_id,
            first_divergent_child_ids=first_children,
            same_package=same_package,
            same_source_package=same_source_package,
            same_snapshot=same_snapshot,
            same_lineage_root=same_root,
            lca_exists=lca_exists,
            ancestor_relation=ancestor_relation,
            different_branches=different_branches,
            distinct_content=distinct_content,
            exclusive_contribution_nonempty=contribution_nonempty,
            contribution_distinct=contribution_distinct,
            train_only_evidence=train_only,
            structural_gates_passed=structural_pass,
            train_floor_satisfied=train_floor,
        )

    def validate_snapshot(
        self,
        snapshot: MergeParentSetSnapshot,
    ) -> MergeCompatibilityReport:
        return self.validate(snapshot.parents)


def validate_merge_parent_set(
    snapshot: MergeParentSetSnapshot,
) -> MergeCompatibilityReport:
    """Validate one serialized parent-set snapshot as the future S8 consumer would."""

    lineage = CandidateAncestryIndex(snapshot.lineage)
    return MergeParentContractValidator(lineage).validate_snapshot(snapshot)
