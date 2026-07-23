"""Strongly typed S8 package-aware merge contracts."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum

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
