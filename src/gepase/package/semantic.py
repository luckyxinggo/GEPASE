"""Core validation, overlay, cache, and consumer policy for semantic hypotheses."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from gepase.package.ir import EdgeKind, GraphEdge, PackageGraph, make_edge
from gepase.package.semantic_models import (
    SemanticCacheEntry,
    SemanticCacheInvalidation,
    SemanticCacheKey,
    SemanticCacheLookup,
    SemanticCacheState,
    SemanticConsumer,
    SemanticConsumerDecision,
    SemanticDecisionStatus,
    SemanticHypothesisConfig,
    SemanticOverlayResult,
    SemanticRelationDecision,
    SemanticRelationProposal,
    SemanticRelationType,
)
from gepase.store.artifacts import sha256_bytes

if TYPE_CHECKING:
    from gepase.evals.functional import AnalyzerSubmission, AnalyzerWorkItem


SEMANTIC_EDGE_KIND_BY_RELATION = {
    SemanticRelationType.IMPLEMENTS: EdgeKind.IMPLEMENTS,
    SemanticRelationType.EXPLAINS: EdgeKind.EXPLAINS,
    SemanticRelationType.CONSTRAINS: EdgeKind.CONSTRAINS,
    SemanticRelationType.CONSUMES: EdgeKind.CONSUMES,
    SemanticRelationType.PRODUCES: EdgeKind.PRODUCES,
    SemanticRelationType.VALIDATES: EdgeKind.VALIDATES,
    SemanticRelationType.CONFLICTS_WITH: EdgeKind.CONFLICTS_WITH,
}
SEMANTIC_EDGE_KINDS = frozenset(SEMANTIC_EDGE_KIND_BY_RELATION.values())
SEMANTIC_ONLY_EDGE_KINDS = SEMANTIC_EDGE_KINDS - {EdgeKind.PRODUCES}
TRUSTED_LAYERS = frozenset({"static", "observed"})


def graph_fingerprint(graph: PackageGraph) -> str:
    payload = graph.model_dump(mode="json")
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def graph_layer_counts(graph: PackageGraph) -> dict[str, int]:
    return dict(sorted(Counter(edge.layer for edge in graph.edges).items()))


def trusted_graph_view(graph: PackageGraph) -> PackageGraph:
    """Return the static+observed graph used by every high-impact consumer."""

    return graph.model_copy(
        update={"edges": tuple(edge for edge in graph.edges if edge.layer in TRUSTED_LAYERS)}
    )


def semantic_consumer_decision(
    graph: PackageGraph,
    consumer: SemanticConsumer,
    config: SemanticHypothesisConfig,
) -> SemanticConsumerDecision:
    allowed = consumer in config.allowed_consumers
    edges = tuple(
        edge.edge_id
        for edge in graph.edges
        if allowed and edge.layer == "semantic_hypothesis"
    )
    return SemanticConsumerDecision(
        consumer=consumer,
        allowed=allowed,
        semantic_edge_ids=edges,
        reason=(
            "bounded_semantic_consumer_allowlist"
            if allowed
            else "semantic_only_edges_forbidden_for_high_impact_consumer"
        ),
    )


def semantic_edges_for_consumer(
    graph: PackageGraph,
    consumer: SemanticConsumer,
    config: SemanticHypothesisConfig,
) -> tuple[GraphEdge, ...]:
    decision = semantic_consumer_decision(graph, consumer, config)
    if not decision.allowed:
        return ()
    allowed_ids = set(decision.semantic_edge_ids)
    return tuple(edge for edge in graph.edges if edge.edge_id in allowed_ids)


def _artifact_hash(project_root: Path, reference: str) -> tuple[str, int]:
    path = (project_root / reference).resolve(strict=True)
    if not path.is_relative_to(project_root):
        raise ValueError("semantic evidence reference escapes the repository")
    payload = path.read_bytes()
    return sha256_bytes(payload), len(payload)


def verify_semantic_scope_artifacts(work: AnalyzerWorkItem, project_root: Path) -> None:
    scope = work.semantic_enrichment
    if scope is None:
        raise ValueError("Analyzer work item has no semantic enrichment scope")
    root = project_root.resolve()
    for artifact in scope.evidence_artifacts:
        observed_hash, observed_size = _artifact_hash(root, artifact.path)
        if observed_hash != artifact.sha256 or observed_size != artifact.size_bytes:
            raise ValueError(f"semantic evidence artifact mismatch: {artifact.path}")
    for reference, expected_hash in (
        (scope.prompt_ref, scope.prompt_hash),
        (scope.schema_ref, scope.schema_hash),
    ):
        observed_hash, _ = _artifact_hash(root, reference)
        if observed_hash != expected_hash:
            raise ValueError(f"semantic protocol artifact mismatch: {reference}")


def semantic_cache_key(
    work: AnalyzerWorkItem,
    *,
    model: str,
) -> SemanticCacheKey:
    scope = work.semantic_enrichment
    if scope is None:
        raise ValueError("Analyzer work item has no semantic enrichment scope")
    node_hashes = tuple(sorted((item.node_id, item.content_hash) for item in scope.allowed_nodes))
    payload = {
        "package_id": scope.package_id,
        "package_snapshot_hash": scope.package_snapshot_hash,
        "failure_cluster_id": scope.failure_cluster_id,
        "node_content_hashes": node_hashes,
        "prompt_hash": scope.prompt_hash,
        "schema_hash": scope.schema_hash,
        "model": model,
        "config_hash": scope.config.config_hash,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return SemanticCacheKey(key_id=hashlib.sha256(raw.encode()).hexdigest(), **payload)


class SemanticHypothesisCache:
    """Serializable Core-owned cache; persistence remains an ArtifactStore concern."""

    def __init__(self, state: SemanticCacheState | None = None) -> None:
        self._entries = {item.key.key_id: item for item in (state or SemanticCacheState()).entries}

    def lookup(self, key: SemanticCacheKey) -> SemanticCacheLookup:
        entry = self._entries.get(key.key_id)
        if entry is None:
            return SemanticCacheLookup(
                key_id=key.key_id,
                status="miss",
                reason="cache_key_absent_or_content_changed",
            )
        if entry.key != key:
            return SemanticCacheLookup(
                key_id=key.key_id,
                status="miss",
                reason="cache_key_collision_rejected",
            )
        return SemanticCacheLookup(
            key_id=key.key_id,
            status="hit",
            result=entry.result,
            reason="exact_snapshot_content_failure_prompt_schema_model_config_match",
        )

    def put(self, key: SemanticCacheKey, result: SemanticOverlayResult) -> None:
        touched = tuple(
            sorted(
                {
                    node_id
                    for item in (*result.accepted, *result.rejected)
                    for node_id in (item.source_node_id, item.target_node_id)
                }
            )
        )
        self._entries[key.key_id] = SemanticCacheEntry(
            key=key,
            result=result,
            touched_node_ids=touched,
        )

    def invalidate_touched(
        self,
        touched_node_ids: set[str],
    ) -> SemanticCacheInvalidation:
        invalidated = sorted(
            key
            for key, entry in self._entries.items()
            if set(entry.touched_node_ids) & touched_node_ids
        )
        for key in invalidated:
            del self._entries[key]
        return SemanticCacheInvalidation(
            touched_node_ids=tuple(sorted(touched_node_ids)),
            invalidated_key_ids=tuple(invalidated),
            retained_key_ids=tuple(sorted(self._entries)),
        )

    def state(self) -> SemanticCacheState:
        return SemanticCacheState(
            entries=tuple(self._entries[key] for key in sorted(self._entries))
        )


class SemanticHypothesisEngine:
    """Validate Analyzer proposals and return a layered PackageGraph copy."""

    def evaluate(
        self,
        work: AnalyzerWorkItem,
        submission: AnalyzerSubmission,
        graph: PackageGraph,
        *,
        project_root: Path,
    ) -> tuple[PackageGraph, SemanticOverlayResult]:
        scope = work.semantic_enrichment
        if scope is None:
            if submission.semantic_relation_proposals:
                raise ValueError("semantic proposals require a bounded enrichment scope")
            raise ValueError("Analyzer work item has no semantic enrichment scope")
        if submission.analyzer_work_id != work.analyzer_work_id:
            raise ValueError("semantic submission/work id mismatch")
        if graph.package_id != scope.package_id:
            raise ValueError("semantic graph package mismatch")
        if graph.snapshot_hash != scope.package_snapshot_hash:
            raise ValueError("semantic graph snapshot mismatch")
        verify_semantic_scope_artifacts(work, project_root)
        by_id = {node.node_id: node for node in graph.nodes}
        contexts = {item.node_id: item for item in scope.allowed_nodes}
        allowed_refs = {item.path for item in scope.evidence_artifacts}
        config = scope.config
        proposals = submission.semantic_relation_proposals
        decisions: list[SemanticRelationDecision] = []
        accepted_edges: list[GraphEdge] = []
        pair_counts: defaultdict[tuple[str, str], int] = defaultdict(int)
        node_counts: Counter[str] = Counter()
        existing_trusted = {
            (edge.source, edge.target, edge.kind)
            for edge in graph.edges
            if edge.layer in TRUSTED_LAYERS
        }
        seen_proposals: set[tuple[str, str, SemanticRelationType]] = set()
        work_over_budget = len(proposals) > config.max_proposals_per_work
        for proposal in proposals:
            reasons = self._rejection_reasons(
                proposal,
                work=work,
                graph_nodes=by_id,
                contexts=contexts,
                allowed_refs=allowed_refs,
                pair_counts=pair_counts,
                node_counts=node_counts,
                existing_trusted=existing_trusted,
                seen_proposals=seen_proposals,
                work_over_budget=work_over_budget,
            )
            if reasons:
                decisions.append(
                    _decision(proposal, SemanticDecisionStatus.REJECTED, tuple(reasons))
                )
                continue
            edge = make_edge(
                proposal.source.node_id,
                proposal.target.node_id,
                SEMANTIC_EDGE_KIND_BY_RELATION[proposal.relation_type],
                layer="semantic_hypothesis",
                identity={
                    "work_id": work.analyzer_work_id,
                    "proposal_id": proposal.proposal_id,
                    "failure_cluster_id": scope.failure_cluster_id,
                },
                evidence_tier="semantic_hypothesis",
                evaluation_id=work.analyzer_work_id,
                task_id=proposal.task_id,
                provider=f"{submission.role_run.host}:{submission.role_run.model}",
                # Preserve the Analyzer's declared confidence for audit.  The
                # selector independently caps its score contribution.
                confidence=proposal.confidence,
                trace_completeness="analyzer_hypothesis_only",
                metadata={
                    "trust_label": "Agent 假设",
                    "relation_type": proposal.relation_type.value,
                    "failure_cluster_id": proposal.failure_cluster_id,
                    "evidence_refs": list(proposal.evidence_refs),
                    "rationale_zh": proposal.rationale_zh,
                    "declared_confidence": proposal.confidence,
                    "source_content_hash": proposal.source.content_hash,
                    "target_content_hash": proposal.target.content_hash,
                    "analyzer_work_id": work.analyzer_work_id,
                    "context_id": submission.role_run.context_id,
                    "host_task_id": submission.role_run.host_task_id,
                    "host": submission.role_run.host,
                    "model": submission.role_run.model,
                    "prompt_hash": scope.prompt_hash,
                    "schema_hash": scope.schema_hash,
                    "config_hash": config.config_hash,
                    "allowed_consumers": [item.value for item in config.allowed_consumers],
                    "generated_at": proposal.generated_at.isoformat(),
                },
            )
            accepted_edges.append(edge)
            pair_counts[(proposal.source.node_id, proposal.target.node_id)] += 1
            node_counts.update((proposal.source.node_id, proposal.target.node_id))
            seen_proposals.add(
                (proposal.source.node_id, proposal.target.node_id, proposal.relation_type)
            )
            decisions.append(
                _decision(
                    proposal,
                    SemanticDecisionStatus.ACCEPTED,
                    ("bounded_hypothesis_accepted",),
                    edge.edge_id,
                )
            )
        before_fingerprint = graph_fingerprint(graph)
        existing_edge_ids = {edge.edge_id for edge in graph.edges}
        layered = graph.model_copy(
            update={
                "edges": (
                    *graph.edges,
                    *tuple(
                        sorted(
                            (
                                edge
                                for edge in accepted_edges
                                if edge.edge_id not in existing_edge_ids
                            ),
                            key=lambda item: item.edge_id,
                        )
                    ),
                )
            }
        )
        trusted_before = trusted_graph_view(graph)
        trusted_after = trusted_graph_view(layered)
        accepted = tuple(
            item for item in decisions if item.status is SemanticDecisionStatus.ACCEPTED
        )
        rejected = tuple(
            item for item in decisions if item.status is SemanticDecisionStatus.REJECTED
        )
        result = SemanticOverlayResult(
            analyzer_work_id=work.analyzer_work_id,
            submission_id=submission.submission_id,
            package_id=graph.package_id,
            package_snapshot_hash=graph.snapshot_hash,
            failure_cluster_id=scope.failure_cluster_id,
            accepted=accepted,
            rejected=rejected,
            semantic_edge_ids=tuple(sorted(edge.edge_id for edge in accepted_edges)),
            layer_counts_before=graph_layer_counts(graph),
            layer_counts_after=graph_layer_counts(layered),
            trusted_graph_unchanged=trusted_before == trusted_after,
            source_node_count_unchanged=len(graph.nodes) == len(layered.nodes),
            source_graph_fingerprint=before_fingerprint,
            layered_graph_fingerprint=graph_fingerprint(layered),
        )
        return layered, result

    @staticmethod
    def _rejection_reasons(
        proposal: SemanticRelationProposal,
        *,
        work: AnalyzerWorkItem,
        graph_nodes: Mapping[str, object],
        contexts: Mapping[str, object],
        allowed_refs: set[str],
        pair_counts: defaultdict[tuple[str, str], int],
        node_counts: Counter[str],
        existing_trusted: set[tuple[str, str, EdgeKind]],
        seen_proposals: set[tuple[str, str, SemanticRelationType]],
        work_over_budget: bool,
    ) -> list[str]:
        scope = work.semantic_enrichment
        assert scope is not None
        config = scope.config
        reasons: list[str] = []
        source_id = proposal.source.node_id
        target_id = proposal.target.node_id
        if work_over_budget:
            reasons.append("work_proposal_budget_exceeded")
        if proposal.relation_type not in config.allowed_relation_types:
            reasons.append("relation_disabled_by_config")
        if proposal.task_id != work.task_id:
            reasons.append("task_mismatch")
        if proposal.failure_cluster_id != scope.failure_cluster_id:
            reasons.append("failure_cluster_mismatch")
        if not set(proposal.evidence_refs) <= allowed_refs:
            reasons.append("evidence_outside_bounded_work_item")
        if source_id not in graph_nodes or target_id not in graph_nodes:
            reasons.append("unknown_graph_node")
        if source_id not in contexts or target_id not in contexts:
            reasons.append("node_outside_bounded_scope")
        for anchor, node_id in ((proposal.source, source_id), (proposal.target, target_id)):
            node = graph_nodes.get(node_id)
            context = contexts.get(node_id)
            if node is not None and getattr(node, "content_hash", None) != anchor.content_hash:
                reasons.append("stale_node_content_hash")
            if context is not None and anchor.excerpt is not None:
                context_excerpt = str(getattr(context, "excerpt", ""))
                if anchor.excerpt not in context_excerpt:
                    reasons.append("excerpt_outside_bounded_context")
            if context is not None and anchor.span_hash is not None:
                context_excerpt = str(getattr(context, "excerpt", ""))
                expected_span_hash = hashlib.sha256(context_excerpt.encode()).hexdigest()
                if anchor.span_hash != expected_span_hash:
                    reasons.append("stale_or_unverifiable_span_hash")
        if proposal.confidence < config.min_confidence:
            reasons.append("below_minimum_confidence")
        pair = (source_id, target_id)
        if pair_counts[pair] >= config.max_relations_per_node_pair:
            reasons.append("node_pair_budget_exceeded")
        if any(
            node_counts[node_id] >= config.max_relations_per_node
            for node_id in (source_id, target_id)
        ):
            reasons.append("per_node_budget_exceeded")
        edge_kind = SEMANTIC_EDGE_KIND_BY_RELATION[proposal.relation_type]
        if (source_id, target_id, edge_kind) in existing_trusted:
            reasons.append("duplicates_trusted_fact")
        identity = (source_id, target_id, proposal.relation_type)
        if identity in seen_proposals:
            reasons.append("duplicate_semantic_hypothesis")
        return list(dict.fromkeys(reasons))


def _decision(
    proposal: SemanticRelationProposal,
    status: SemanticDecisionStatus,
    reasons: tuple[str, ...],
    edge_id: str | None = None,
) -> SemanticRelationDecision:
    return SemanticRelationDecision(
        proposal_id=proposal.proposal_id,
        status=status,
        relation_type=proposal.relation_type,
        source_node_id=proposal.source.node_id,
        target_node_id=proposal.target.node_id,
        confidence=proposal.confidence,
        reason_codes=reasons,
        edge_id=edge_id,
    )
