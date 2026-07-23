"""Strongly typed S7.6 evolution-search and merge-parent contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import Field, model_validator

from gepase.schemas.common import FrozenModel

EVOLUTION_SCHEMA_VERSION = "2.0.0"
MERGE_CONSUMER_CONTRACT_VERSION = "1.0.0"

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonEmpty = Annotated[str, Field(min_length=1)]


def _require_unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not contain duplicates")


def _looks_held_out(ref: str) -> bool:
    text = ref.casefold()
    return any(marker in text for marker in ("validation", "/test", "test-", "held-out"))


class MergeEligibility(StrEnum):
    """Whether one pool row has sufficient explicit identity for merge selection."""

    UNKNOWN_LEGACY = "unknown_legacy"
    PENDING_CONTRACT = "pending_contract"
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"


class MergeCompatibilityReason(StrEnum):
    COMPATIBLE = "compatible"
    INSUFFICIENT_PARENTS = "insufficient_parents"
    LEGACY_IDENTITY_UNKNOWN = "legacy_identity_unknown"
    MERGE_ELIGIBILITY_NOT_READY = "merge_eligibility_not_ready"
    CROSS_PACKAGE = "cross_package"
    DIFFERENT_SOURCE_PACKAGE = "different_source_package"
    DIFFERENT_SNAPSHOT = "different_snapshot"
    DIFFERENT_LINEAGE_ROOT = "different_lineage_root"
    MISSING_LINEAGE_NODE = "missing_lineage_node"
    LCA_MISSING = "lca_missing"
    ANCESTOR_DESCENDANT = "ancestor_descendant"
    SAME_BRANCH = "same_branch"
    DUPLICATE_CANDIDATE = "duplicate_candidate"
    DUPLICATE_CONTENT = "duplicate_content"
    EMPTY_CONTRIBUTION = "empty_contribution"
    DUPLICATE_CONTRIBUTION = "duplicate_contribution"
    HELD_OUT_EVIDENCE = "held_out_evidence"
    STRUCTURAL_GATE_FAILED = "structural_gate_failed"
    TRAIN_FLOOR_FAILED = "train_floor_failed"
    CLAIMED_ANCESTRY_MISMATCH = "claimed_ancestry_mismatch"


class ExclusiveContribution(FrozenModel):
    """Train-only local wins; diversity does not itself imply merge compatibility."""

    task_keys: tuple[str, ...] = ()
    objective_keys: tuple[str, ...] = ()
    component_ids: tuple[str, ...] = ()
    closure_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def unique_keys(self) -> ExclusiveContribution:
        _require_unique(self.task_keys, "task_keys")
        _require_unique(self.objective_keys, "objective_keys")
        _require_unique(self.component_ids, "component_ids")
        _require_unique(self.closure_ids, "closure_ids")
        return self

    @property
    def is_empty(self) -> bool:
        return not any((self.task_keys, self.objective_keys, self.component_ids, self.closure_ids))

    @property
    def signature(self) -> tuple[tuple[str, ...], ...]:
        return (
            tuple(sorted(self.task_keys)),
            tuple(sorted(self.objective_keys)),
            tuple(sorted(self.component_ids)),
            tuple(sorted(self.closure_ids)),
        )


class FailureCluster(FrozenModel):
    schema_version: str = EVOLUTION_SCHEMA_VERSION
    cluster_id: NonEmpty
    package_id: NonEmpty
    source_snapshot_hash: Sha256
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    representative_task_ids: tuple[str, ...] = Field(min_length=1)
    oracle_refs: tuple[str, ...] = Field(min_length=1)
    target_metric: NonEmpty
    causal_node_ids: tuple[str, ...] = Field(min_length=1)
    allowed_operations: tuple[str, ...] = Field(min_length=1)
    support_count: int = Field(ge=1)
    severity: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    expected_benefit: NonEmpty
    blast_radius: int = Field(ge=0)

    @model_validator(mode="after")
    def cluster_invariants(self) -> FailureCluster:
        for values, label in (
            (self.evidence_refs, "evidence_refs"),
            (self.representative_task_ids, "representative_task_ids"),
            (self.oracle_refs, "oracle_refs"),
            (self.causal_node_ids, "causal_node_ids"),
            (self.allowed_operations, "allowed_operations"),
        ):
            _require_unique(values, label)
        if self.support_count < len(self.evidence_refs):
            raise ValueError("support_count cannot be smaller than evidence_refs")
        return self


class EvolutionCandidateIdentity(FrozenModel):
    """Explicit candidate family and branch identity; never inferred from scores."""

    schema_version: str = EVOLUTION_SCHEMA_VERSION
    candidate_id: NonEmpty
    package_id: NonEmpty
    source_package_ref: NonEmpty
    source_snapshot_hash: Sha256
    lineage_root_candidate_id: NonEmpty
    parent_ids: tuple[str, ...] = ()
    branch_id: str | None = None
    branch_root_candidate_id: str | None = None
    generation: int = Field(ge=0)
    operator: NonEmpty
    content_hash: Sha256
    failure_cluster_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def identity_invariants(self) -> EvolutionCandidateIdentity:
        source = Path(self.source_package_ref)
        if source.is_absolute() or ".." in source.parts:
            raise ValueError("source_package_ref must be repository-relative")
        _require_unique(self.parent_ids, "parent_ids")
        _require_unique(self.failure_cluster_ids, "failure_cluster_ids")
        if self.candidate_id in self.parent_ids:
            raise ValueError("candidate cannot be its own parent")
        if self.generation == 0:
            if self.parent_ids:
                raise ValueError("lineage root cannot have parents")
            if self.lineage_root_candidate_id != self.candidate_id:
                raise ValueError("generation-zero candidate must be the lineage root")
            if self.branch_id is not None or self.branch_root_candidate_id is not None:
                raise ValueError("lineage root cannot claim a mutation branch")
        else:
            if not self.parent_ids:
                raise ValueError("derived candidate requires explicit parent_ids")
            if not self.branch_id or not self.branch_root_candidate_id:
                raise ValueError("derived candidate requires explicit branch identity")
        return self


class MutationBranch(FrozenModel):
    schema_version: str = EVOLUTION_SCHEMA_VERSION
    branch_id: NonEmpty
    package_id: NonEmpty
    source_snapshot_hash: Sha256
    lineage_root_candidate_id: NonEmpty
    branch_root_candidate_id: NonEmpty
    failure_cluster_id: NonEmpty
    candidate_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def branch_invariants(self) -> MutationBranch:
        _require_unique(self.candidate_ids, "candidate_ids")
        if self.candidate_ids[0] != self.branch_root_candidate_id:
            raise ValueError("first branch candidate must be branch_root_candidate_id")
        return self


class MergeParentCandidate(FrozenModel):
    """Pool candidate plus the train-only facts consumed by the S8 input validator."""

    identity: EvolutionCandidateIdentity
    patch_id: NonEmpty
    ancestor_chain: tuple[str, ...] = Field(min_length=1)
    contribution: ExclusiveContribution
    train_evidence_refs: tuple[str, ...] = Field(min_length=1)
    gate_0_1_passed: bool
    train_floor_satisfied: bool
    not_deployable: bool = True
    merge_eligibility: MergeEligibility = MergeEligibility.ELIGIBLE

    @model_validator(mode="after")
    def parent_invariants(self) -> MergeParentCandidate:
        _require_unique(self.ancestor_chain, "ancestor_chain")
        _require_unique(self.train_evidence_refs, "train_evidence_refs")
        if self.ancestor_chain[0] != self.identity.lineage_root_candidate_id:
            raise ValueError("ancestor_chain must start at lineage_root_candidate_id")
        if self.ancestor_chain[-1] != self.identity.candidate_id:
            raise ValueError("ancestor_chain must end at candidate_id")
        if not self.not_deployable:
            raise ValueError("breeding parent must retain train-only not_deployable status")
        return self


class MergeCompatibilityReport(FrozenModel):
    schema_version: str = EVOLUTION_SCHEMA_VERSION
    consumer_contract_version: str = MERGE_CONSUMER_CONTRACT_VERSION
    parent_candidate_ids: tuple[str, ...]
    merge_input_compatible: bool
    reason_codes: tuple[MergeCompatibilityReason, ...]
    lca_candidate_id: str | None = None
    first_divergent_child_ids: tuple[str, ...] = ()
    same_package: bool
    same_source_package: bool
    same_snapshot: bool
    same_lineage_root: bool
    lca_exists: bool
    ancestor_relation: bool
    different_branches: bool
    distinct_content: bool
    exclusive_contribution_nonempty: bool
    contribution_distinct: bool
    train_only_evidence: bool
    structural_gates_passed: bool
    train_floor_satisfied: bool

    @model_validator(mode="after")
    def report_consistency(self) -> MergeCompatibilityReport:
        _require_unique(self.reason_codes, "reason_codes")
        if self.merge_input_compatible:
            if self.reason_codes != (MergeCompatibilityReason.COMPATIBLE,):
                raise ValueError("compatible report must contain only compatible reason")
        elif MergeCompatibilityReason.COMPATIBLE in self.reason_codes:
            raise ValueError("rejected report cannot contain compatible reason")
        return self


class MergeParentSetSnapshot(FrozenModel):
    schema_version: str = EVOLUTION_SCHEMA_VERSION
    parent_set_id: NonEmpty
    parents: tuple[MergeParentCandidate, ...]
    lineage: tuple[EvolutionCandidateIdentity, ...] = Field(min_length=1)
    selection_config_hash: Sha256
    train_selection_evidence_refs: tuple[str, ...] = Field(min_length=1)
    consumer_contract_version: str = MERGE_CONSUMER_CONTRACT_VERSION
    compatibility_report: MergeCompatibilityReport | None = None

    @model_validator(mode="after")
    def snapshot_invariants(self) -> MergeParentSetSnapshot:
        candidate_ids = tuple(item.candidate_id for item in self.lineage)
        _require_unique(candidate_ids, "lineage candidate_ids")
        _require_unique(self.train_selection_evidence_refs, "train_selection_evidence_refs")
        if any(_looks_held_out(ref) for ref in self.train_selection_evidence_refs):
            raise ValueError("breeding snapshot cannot contain held-out evidence")
        parent_ids = {item.identity.candidate_id for item in self.parents}
        if not parent_ids <= set(candidate_ids):
            raise ValueError("all parents must be present in lineage")
        return self


class BreedingSnapshot(FrozenModel):
    schema_version: str = EVOLUTION_SCHEMA_VERSION
    snapshot_id: NonEmpty
    candidates: tuple[MergeParentCandidate, ...] = Field(min_length=1)
    lineage: tuple[EvolutionCandidateIdentity, ...] = Field(min_length=1)
    selection_config_hash: Sha256
    train_evidence_refs: tuple[str, ...] = Field(min_length=1)
    held_out_fields_redacted: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def train_only(self) -> BreedingSnapshot:
        _require_unique(
            tuple(item.identity.candidate_id for item in self.candidates),
            "candidate_ids",
        )
        _require_unique(
            tuple(item.candidate_id for item in self.lineage),
            "lineage candidate_ids",
        )
        _require_unique(self.train_evidence_refs, "train_evidence_refs")
        if not self.held_out_fields_redacted:
            raise ValueError("breeding snapshot must redact held-out fields")
        if any(_looks_held_out(ref) for ref in self.train_evidence_refs):
            raise ValueError("breeding snapshot cannot contain held-out evidence")
        return self
