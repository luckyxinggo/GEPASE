"""Strongly typed S8 package-aware merge contracts."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator

from gepase.mutation.schema import PatchOperation
from gepase.optimizer.candidate import PackageCandidate
from gepase.optimizer.evolution.models import MergeParentSetSnapshot
from gepase.schemas.common import FrozenModel

MERGE_SCHEMA_VERSION = "1.0.0"


class MergeConflictKind(StrEnum):
    SAME_NODE_CONTENT = "same_node_different_content"
    PATH_COLLISION = "path_collision"
    INTERFACE_SIGNATURE = "interface_signature_conflict"
    DELETE_MODIFY = "delete_modify_conflict"
    FRONTMATTER = "frontmatter_conflict"
    INCOMPATIBLE_DEPENDENCY = "incompatible_dependency"


class ParentSelectionScore(FrozenModel):
    parent_set_id: str
    package_id: str
    parent_candidate_ids: tuple[str, ...] = Field(min_length=2)
    exclusive_task_wins: int = Field(ge=0)
    exclusive_objective_wins: int = Field(ge=0)
    exclusive_component_count: int = Field(ge=0)
    closure_union_size: int = Field(ge=0)
    closure_overlap_size: int = Field(ge=0)
    closure_overlap_ratio: float = Field(ge=0.0, le=1.0)
    structural_risk: float = Field(ge=0.0)
    lca_distance: int = Field(ge=0)
    score: float
    held_out_features_read: int = Field(default=0, ge=0, le=0)
    contract_revalidated: bool


class SelectedMergeParentSet(FrozenModel):
    snapshot: MergeParentSetSnapshot
    score: ParentSelectionScore

    @model_validator(mode="after")
    def identities_match(self) -> SelectedMergeParentSet:
        ids = tuple(parent.identity.candidate_id for parent in self.snapshot.parents)
        if ids != self.score.parent_candidate_ids:
            raise ValueError("selection score parent identities do not match snapshot")
        return self


class ParentContribution(FrozenModel):
    parent_candidate_id: str
    patch_ids: tuple[str, ...] = Field(min_length=1)
    operations: tuple[PatchOperation, ...] = Field(min_length=1)
    mutation_node_ids: tuple[str, ...] = Field(min_length=1)
    dependency_node_ids: tuple[str, ...] = ()
    closure_node_ids: tuple[str, ...] = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)


class MergeConflict(FrozenModel):
    conflict_id: str
    kind: MergeConflictKind
    path: str
    node_ids: tuple[str, ...]
    parent_candidate_ids: tuple[str, ...] = Field(min_length=2)
    operation_ids: tuple[str, ...] = Field(min_length=2)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    detail: str = Field(min_length=1)


class ContributionSource(FrozenModel):
    operation_id: str
    path: str
    target_node_id: str | None = None
    source_parent_candidate_ids: tuple[str, ...] = Field(min_length=1)
    resolution: str = "deterministic_parent_union"


class MergeContributionMap(FrozenModel):
    lca_candidate_id: str
    sources: tuple[ContributionSource, ...] = Field(min_length=1)

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class MergeResolutionWorkItem(FrozenModel):
    schema_version: str = MERGE_SCHEMA_VERSION
    work_id: str
    parent_set_id: str
    lca_candidate_id: str
    conflicts: tuple[MergeConflict, ...] = Field(min_length=1)
    allowed_paths: tuple[str, ...] = Field(min_length=1)
    allowed_node_ids: tuple[str, ...] = Field(min_length=1)
    allowed_preconditions: dict[str, str] = Field(min_length=1)
    base_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    assertions_included: bool = False
    sibling_outputs_included: bool = False
    production_external_calls_allowed: bool = False

    @model_validator(mode="after")
    def preconditions_cover_scope(self) -> MergeResolutionWorkItem:
        if set(self.allowed_preconditions) != set(self.allowed_node_ids):
            raise ValueError("resolution preconditions must cover the exact conflict node set")
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.allowed_preconditions.values()
        ):
            raise ValueError("resolution preconditions must be lowercase SHA-256 hashes")
        return self


class MergeResolutionSubmission(FrozenModel):
    schema_version: str = MERGE_SCHEMA_VERSION
    work_id: str
    operations: tuple[PatchOperation, ...] = Field(min_length=1)
    resolved_conflict_ids: tuple[str, ...] = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=1_200)
    assertions_seen: bool = False
    sibling_outputs_seen: bool = False
    production_external_calls: int = Field(default=0, ge=0, le=0)


class MergeBuildRecord(FrozenModel):
    schema_version: str = MERGE_SCHEMA_VERSION
    parent_set_id: str
    package_id: str
    benchmark_skill_id: str
    parent_candidate_ids: tuple[str, ...] = Field(min_length=2)
    lca_candidate_id: str
    patch_id: str | None = None
    application_id: str | None = None
    candidate: PackageCandidate | None = None
    contribution_map: MergeContributionMap | None = None
    conflicts: tuple[MergeConflict, ...] = ()
    contract_revalidated: bool
    held_out_features_read: int = Field(default=0, ge=0, le=0)
    status: str
    reason: str | None = None


class MergeOutcomeStatus(StrEnum):
    MATERIALIZED_PENDING_EVALUATION = "materialized_pending_evaluation"
    MATERIALIZED_AND_EVALUATED = "materialized_and_evaluated"
    NO_ELIGIBLE_PARENT_SET = "no_eligible_parent_set"
    NOT_REACHED_BUDGET_INCOMPLETE = "not_reached_budget_incomplete"


class MergeOutcome(FrozenModel):
    """Typed terminal/pending result of exhaustive train-only parent enumeration."""

    schema_version: str = MERGE_SCHEMA_VERSION
    status: MergeOutcomeStatus
    considered_parent_candidate_ids: tuple[str, ...]
    considered_parent_set_count: int = Field(ge=0)
    eligible_parent_set_count: int = Field(ge=0)
    rejected_parent_set_count: int = Field(ge=0)
    rejection_reason_counts: dict[str, int]
    cross_package_pair_count: int = Field(ge=0)
    selected_parent_set_id: str | None = None
    merge_candidate_id: str | None = None
    evaluation_complete: bool = False
    enumeration_ref: str | None = None
    build_record_ref: str | None = None

    @model_validator(mode="after")
    def outcome_consistency(self) -> MergeOutcome:
        if len(self.considered_parent_candidate_ids) != len(
            set(self.considered_parent_candidate_ids)
        ):
            raise ValueError("considered Merge parents must be unique")
        if self.considered_parent_set_count != (
            self.eligible_parent_set_count + self.rejected_parent_set_count
        ):
            raise ValueError("Merge considered set count is incomplete")
        for value in (self.enumeration_ref, self.build_record_ref):
            if value is None:
                continue
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Merge evidence refs must be repository-relative")
        if self.status is MergeOutcomeStatus.NOT_REACHED_BUDGET_INCOMPLETE:
            if any(
                (
                    self.eligible_parent_set_count,
                    self.selected_parent_set_id is not None,
                    self.merge_candidate_id is not None,
                    self.evaluation_complete,
                    self.build_record_ref is not None,
                )
            ):
                raise ValueError("budget-incomplete Merge cannot claim enumeration/child")
        elif self.status is MergeOutcomeStatus.NO_ELIGIBLE_PARENT_SET:
            if self.eligible_parent_set_count != 0:
                raise ValueError("no_eligible_parent_set cannot contain an eligible set")
            if self.selected_parent_set_id or self.merge_candidate_id or self.evaluation_complete:
                raise ValueError("ineligible Merge outcome cannot claim a child")
            if not self.enumeration_ref:
                raise ValueError("ineligible Merge outcome requires enumeration evidence")
        else:
            if self.eligible_parent_set_count < 1 or not self.selected_parent_set_id:
                raise ValueError("materialized Merge outcome requires an eligible selected set")
            if not self.merge_candidate_id or not self.build_record_ref:
                raise ValueError("materialized Merge outcome requires a child/build record")
            if not self.enumeration_ref:
                raise ValueError("materialized Merge outcome requires enumeration evidence")
            if (
                self.status is MergeOutcomeStatus.MATERIALIZED_AND_EVALUATED
            ) != self.evaluation_complete:
                raise ValueError("Merge evaluation status is inconsistent")
        return self
