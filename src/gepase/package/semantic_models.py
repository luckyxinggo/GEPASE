"""Typed contracts for bounded Analyzer-proposed semantic graph hypotheses."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator

from gepase.schemas.common import SCHEMA_VERSION, ArtifactRef, FrozenModel


class SemanticRelationType(StrEnum):
    IMPLEMENTS = "implements"
    EXPLAINS = "explains"
    CONSTRAINS = "constrains"
    CONSUMES = "consumes"
    PRODUCES = "produces"
    VALIDATES = "validates"
    CONFLICTS_WITH = "conflicts_with"


class SemanticConsumer(StrEnum):
    FAILURE_LOCALIZATION = "failure_localization"
    ASI_EXPLANATION = "asi_explanation"
    SELECTOR_TOP_K = "selector_top_k"
    EXPLORATION_ORDERING = "exploration_ordering"
    PATCH_AUTHORIZATION = "patch_authorization"
    TARGET_SET_EXPANSION = "target_set_expansion"
    TRUSTED_DEPENDENCY_CLOSURE = "trusted_dependency_closure"
    SAFETY_CLOSURE = "safety_closure"
    MERGE_ELIGIBILITY = "merge_eligibility"
    MERGE_CONFLICT_RESOLUTION = "merge_conflict_resolution"
    VALIDATION_GATE = "validation_gate"


ALLOWED_SEMANTIC_CONSUMERS = (
    SemanticConsumer.FAILURE_LOCALIZATION,
    SemanticConsumer.ASI_EXPLANATION,
    SemanticConsumer.SELECTOR_TOP_K,
    SemanticConsumer.EXPLORATION_ORDERING,
)


class SemanticHypothesisConfig(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    config_id: str = "gh-p1-bounded-semantic-v1"
    allowed_relation_types: tuple[SemanticRelationType, ...] = tuple(SemanticRelationType)
    allowed_consumers: tuple[SemanticConsumer, ...] = ALLOWED_SEMANTIC_CONSUMERS
    min_confidence: float = Field(default=0.45, ge=0, le=1)
    selector_weight_cap: float = Field(default=0.35, ge=0, le=0.5)
    max_proposals_per_work: int = Field(default=6, ge=1, le=12)
    max_relations_per_node_pair: int = Field(default=2, ge=1, le=3)
    max_relations_per_node: int = Field(default=3, ge=1, le=6)
    max_allowed_nodes: int = Field(default=12, ge=2, le=40)
    max_agent_calls: int = Field(default=1, ge=0, le=1)
    analyzer_timeout_ms: int = Field(default=180_000, ge=1, le=600_000)
    analyzer_token_budget: int = Field(default=12_000, ge=1, le=64_000)

    @model_validator(mode="after")
    def bounded_and_allowlisted(self) -> SemanticHypothesisConfig:
        if len(self.allowed_relation_types) != len(set(self.allowed_relation_types)):
            raise ValueError("semantic relation types must be unique")
        if len(self.allowed_consumers) != len(set(self.allowed_consumers)):
            raise ValueError("semantic consumers must be unique")
        if not set(self.allowed_consumers) <= set(ALLOWED_SEMANTIC_CONSUMERS):
            raise ValueError("semantic config cannot enable a high-impact consumer")
        return self

    @property
    def config_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude={"schema_version"})
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()


class SemanticNodeContext(FrozenModel):
    node_id: str
    path: str
    locator: str
    kind: str
    label: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    excerpt: str = Field(min_length=1, max_length=1_200)


class SemanticNodeAnchor(FrozenModel):
    node_id: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    excerpt: str | None = Field(default=None, min_length=1, max_length=600)
    span_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def has_minimal_content_evidence(self) -> SemanticNodeAnchor:
        if self.excerpt is None and self.span_hash is None:
            raise ValueError("semantic node anchor requires excerpt or span_hash")
        return self


class SemanticRelationProposal(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    proposal_id: str = Field(min_length=1)
    source: SemanticNodeAnchor
    target: SemanticNodeAnchor
    relation_type: SemanticRelationType
    task_id: str = Field(min_length=1)
    failure_cluster_id: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    rationale_zh: str = Field(min_length=1, max_length=1_200)
    confidence: float = Field(ge=0, le=1)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def distinct_nodes_and_evidence(self) -> SemanticRelationProposal:
        if self.source.node_id == self.target.node_id:
            raise ValueError("semantic relation cannot be a self-loop")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("semantic evidence refs must be unique")
        return self


class SemanticEnrichmentScope(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    package_id: str
    package_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    failure_cluster_id: str
    failure_summary_zh: str = Field(min_length=1, max_length=4_000)
    evidence_artifacts: tuple[ArtifactRef, ...] = Field(min_length=1)
    allowed_nodes: tuple[SemanticNodeContext, ...] = Field(min_length=2)
    prompt_ref: str
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_ref: str
    schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    config: SemanticHypothesisConfig

    @model_validator(mode="after")
    def bounded_relative_scope(self) -> SemanticEnrichmentScope:
        if len(self.allowed_nodes) > self.config.max_allowed_nodes:
            raise ValueError("semantic scope exceeds bounded node budget")
        node_ids = [item.node_id for item in self.allowed_nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("semantic scope node ids must be unique")
        refs = [item.path for item in self.evidence_artifacts]
        if len(refs) != len(set(refs)):
            raise ValueError("semantic evidence artifacts must be unique")
        for value in (*refs, self.prompt_ref, self.schema_ref):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("semantic scope refs must be repository-relative")
        return self


class SemanticDecisionStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class SemanticRelationDecision(FrozenModel):
    proposal_id: str
    status: SemanticDecisionStatus
    relation_type: SemanticRelationType
    source_node_id: str
    target_node_id: str
    confidence: float = Field(ge=0, le=1)
    reason_codes: tuple[str, ...] = Field(min_length=1)
    edge_id: str | None = None


class SemanticOverlayResult(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    analyzer_work_id: str
    submission_id: str
    package_id: str
    package_snapshot_hash: str
    failure_cluster_id: str
    accepted: tuple[SemanticRelationDecision, ...]
    rejected: tuple[SemanticRelationDecision, ...]
    semantic_edge_ids: tuple[str, ...]
    layer_counts_before: dict[str, int]
    layer_counts_after: dict[str, int]
    trusted_graph_unchanged: bool
    source_node_count_unchanged: bool
    source_graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    layered_graph_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class SemanticCacheKey(FrozenModel):
    key_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_id: str
    package_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    failure_cluster_id: str
    node_content_hashes: tuple[tuple[str, str], ...]
    prompt_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SemanticCacheEntry(FrozenModel):
    key: SemanticCacheKey
    result: SemanticOverlayResult
    touched_node_ids: tuple[str, ...]


class SemanticCacheState(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    entries: tuple[SemanticCacheEntry, ...] = ()


class SemanticCacheLookup(FrozenModel):
    key_id: str
    status: str
    result: SemanticOverlayResult | None = None
    reason: str


class SemanticCacheInvalidation(FrozenModel):
    touched_node_ids: tuple[str, ...]
    invalidated_key_ids: tuple[str, ...]
    retained_key_ids: tuple[str, ...]


class SemanticConsumerDecision(FrozenModel):
    consumer: SemanticConsumer
    allowed: bool
    semantic_edge_ids: tuple[str, ...]
    reason: str
