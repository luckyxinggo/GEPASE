from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from gepase.evals.eval_plan import RoleRunProvenance
from gepase.evals.evidence import UsageRecord
from gepase.evals.functional import (
    AnalyzerEvidenceSummary,
    AnalyzerSubmission,
    AnalyzerWorkItem,
)
from gepase.mutation.target_set import choose_bounded_target_set
from gepase.optimizer.graph_selector import GraphGuidedComponentSelector
from gepase.optimizer.merge.closure import dependency_closure
from gepase.optimizer.selectors import (
    FeatureContribution,
    RankedSelection,
    SelectionContext,
    SelectionTarget,
)
from gepase.package.ir import (
    EdgeKind,
    FailureSlice,
    FailureSliceNode,
    IRNode,
    NodeKind,
    PackageGraph,
    make_edge,
    make_node,
)
from gepase.package.semantic import (
    SemanticHypothesisCache,
    SemanticHypothesisEngine,
    graph_fingerprint,
    semantic_cache_key,
    semantic_consumer_decision,
    trusted_graph_view,
)
from gepase.package.semantic_models import (
    SemanticConsumer,
    SemanticEnrichmentScope,
    SemanticHypothesisConfig,
    SemanticNodeAnchor,
    SemanticNodeContext,
    SemanticRelationProposal,
    SemanticRelationType,
)
from gepase.package.slicing import reverse_slice
from gepase.reporting.graph_report import render_graph_report
from gepase.schemas.common import ArtifactRef


def _write_artifact(root: Path, relative: str, text: str) -> ArtifactRef:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    payload = path.read_bytes()
    return ArtifactRef(
        path=relative,
        sha256=hashlib.sha256(payload).hexdigest(),
        media_type="application/json" if relative.endswith(".json") else "text/plain",
        size_bytes=len(payload),
    )


def _fixture(tmp_path: Path) -> tuple[PackageGraph, AnalyzerWorkItem, AnalyzerSubmission]:
    package = make_node("fixture", NodeKind.PACKAGE, ".", "package", "fixture", "package")
    skill = make_node(
        "fixture",
        NodeKind.INSTRUCTION,
        "SKILL.md",
        "instruction/validate",
        "Validate the generated animation before delivery.",
        "validate instruction",
    )
    script = make_node(
        "fixture",
        NodeKind.FUNCTION,
        "core/validators.py",
        "symbol/validate_gif",
        "validate_gif",
        "def validate_gif(path): return True",
    )
    peer = make_node(
        "fixture",
        NodeKind.FUNCTION,
        "core/validators.py",
        "symbol/is_slack_ready",
        "is_slack_ready",
        "def is_slack_ready(path): return True",
    )
    other = make_node(
        "fixture",
        NodeKind.FUNCTION,
        "core/easing.py",
        "symbol/ease",
        "ease",
        "def ease(value): return value",
    )
    graph = PackageGraph(
        package_id="fixture",
        snapshot_hash="1" * 64,
        nodes=(package, skill, script, peer, other),
        edges=(
            make_edge(package.node_id, skill.node_id, EdgeKind.CONTAINS),
            make_edge(package.node_id, script.node_id, EdgeKind.CONTAINS),
            make_edge(package.node_id, peer.node_id, EdgeKind.CONTAINS),
            make_edge(package.node_id, other.node_id, EdgeKind.CONTAINS),
        ),
    )
    evidence = _write_artifact(tmp_path, "evidence/failure.json", '{"loop":false}\n')
    prompt = _write_artifact(tmp_path, "protocol/prompt.md", "bounded semantic prompt\n")
    schema = _write_artifact(tmp_path, "protocol/schema.json", "{}\n")
    contexts = tuple(
        SemanticNodeContext(
            node_id=node.node_id,
            path=node.path,
            locator=node.locator,
            kind=node.kind.value,
            label=node.label,
            content_hash=node.content_hash,
            excerpt=f"bounded excerpt for {node.label}",
        )
        for node in (skill, script, other)
    )
    scope = SemanticEnrichmentScope(
        package_id=graph.package_id,
        package_snapshot_hash=graph.snapshot_hash,
        failure_cluster_id="loop-seam",
        failure_summary_zh="循环接缝校验未发现错误。",
        evidence_artifacts=(evidence,),
        allowed_nodes=contexts,
        prompt_ref=prompt.path,
        prompt_hash=prompt.sha256,
        schema_ref=schema.path,
        schema_hash=schema.sha256,
        config=SemanticHypothesisConfig(),
    )
    summary = AnalyzerEvidenceSummary(
        variant="original",
        execution_record_ref=evidence.path,
        deterministic_bundle_ref=evidence.path,
        independent_grade_ref=evidence.path,
        task_correctness=0.5,
        output_quality=0.5,
        failed_expectation_ids=("loop",),
        grader_feedback_zh="循环接缝不连续。",
    )
    work = AnalyzerWorkItem(
        analyzer_work_id="semantic-work-fixture",
        task_id="task-loop",
        pair_id="pair-loop",
        task_prompt="生成循环动画",
        baseline=summary.model_copy(update={"variant": "no-skill"}),
        original=summary,
        package_graph_ref="graph.json",
        node_hints=(),
        submission_schema_ref="schema.json",
        semantic_enrichment=scope,
    )
    now = datetime(2026, 7, 28, tzinfo=UTC)
    proposal = SemanticRelationProposal(
        proposal_id="proposal-validates",
        source=SemanticNodeAnchor(
            node_id=script.node_id,
            content_hash=script.content_hash,
            excerpt="bounded excerpt for validate_gif",
        ),
        target=SemanticNodeAnchor(
            node_id=skill.node_id,
            content_hash=skill.content_hash,
            excerpt="bounded excerpt for Validate",
        ),
        relation_type=SemanticRelationType.IMPLEMENTS,
        task_id=work.task_id,
        failure_cluster_id=scope.failure_cluster_id,
        evidence_refs=(evidence.path,),
        rationale_zh="校验函数实现了交付前验证指令。",
        confidence=0.78,
        generated_at=now,
    )
    submission = AnalyzerSubmission(
        submission_id="semantic-submission-fixture",
        analyzer_work_id=work.analyzer_work_id,
        role_run=RoleRunProvenance(
            host="fixture-agent-host",
            model="fixture-analyzer",
            context_id="isolated-semantic-fixture",
            host_task_id="fixture-task",
            usage=UsageRecord(
                output_tokens=100,
                duration_ms=500,
                token_count_kind="estimated",
            ),
            started_at=now,
            finished_at=now + timedelta(milliseconds=500),
        ),
        analyses=(),
        summary_zh="提出一条有证据锚点的实现关系假设。",
        semantic_relation_proposals=(proposal,),
    )
    return graph, work, submission


def _target(node: IRNode) -> SelectionTarget:
    return SelectionTarget(
        node_id=node.node_id,
        path=node.path,
        locator=node.locator,
        node_kind=node.kind.value,
        content_hash=node.content_hash,
        token_estimate=10,
    )


def _rank(node: IRNode, rank: int) -> RankedSelection:
    return RankedSelection(
        rank=rank,
        node_id=node.node_id,
        path=node.path,
        locator=node.locator,
        score=1.0,
        contributions=(
            FeatureContribution(
                feature="fixture",
                raw_value=1.0,
                weight=1.0,
                contribution=1.0,
            ),
        ),
        evidence_refs=("fixture:evidence",),
        reason_code="fixture",
    )


def test_core_accepts_only_audited_semantic_overlay(tmp_path: Path) -> None:
    graph, work, submission = _fixture(tmp_path)
    layered, result = SemanticHypothesisEngine().evaluate(
        work, submission, graph, project_root=tmp_path
    )
    assert len(result.accepted) == 1 and not result.rejected
    assert result.trusted_graph_unchanged and result.source_node_count_unchanged
    assert trusted_graph_view(layered) == trusted_graph_view(graph)
    semantic_edge = next(edge for edge in layered.edges if edge.layer == "semantic_hypothesis")
    assert semantic_edge.metadata["trust_label"] == "Agent 假设"
    assert semantic_edge.confidence == 0.78
    assert graph_fingerprint(graph) == result.source_graph_fingerprint


def test_invalid_relation_and_adversarial_proposals_are_rejected(tmp_path: Path) -> None:
    graph, work, submission = _fixture(tmp_path)
    raw = submission.model_dump(mode="json")
    raw["semantic_relation_proposals"][0]["relation_type"] = "similar_to"
    with pytest.raises(ValidationError):
        AnalyzerSubmission.model_validate(raw)

    valid = submission.semantic_relation_proposals[0]
    outside = valid.model_copy(
        update={
            "proposal_id": "outside-evidence",
            "evidence_refs": ("unlisted.json",),
        }
    )
    stale = valid.model_copy(
        update={
            "proposal_id": "stale-content",
            "source": valid.source.model_copy(update={"content_hash": "f" * 64}),
        }
    )
    unknown = valid.model_copy(
        update={
            "proposal_id": "unknown-node",
            "target": valid.target.model_copy(update={"node_id": "node-missing"}),
        }
    )
    low = valid.model_copy(update={"proposal_id": "low-confidence", "confidence": 0.1})
    adversarial = submission.model_copy(
        update={"semantic_relation_proposals": (outside, stale, unknown, low)}
    )
    layered, result = SemanticHypothesisEngine().evaluate(
        work, adversarial, graph, project_root=tmp_path
    )
    assert not result.accepted and len(result.rejected) == 4
    codes = {code for item in result.rejected for code in item.reason_codes}
    assert {
        "evidence_outside_bounded_work_item",
        "stale_node_content_hash",
        "unknown_graph_node",
        "below_minimum_confidence",
    } <= codes
    assert layered == graph


def test_semantic_cache_is_exact_and_invalidates_only_touched_nodes(tmp_path: Path) -> None:
    graph, work, submission = _fixture(tmp_path)
    _, result = SemanticHypothesisEngine().evaluate(
        work, submission, graph, project_root=tmp_path
    )
    key = semantic_cache_key(work, model=submission.role_run.model)
    cache = SemanticHypothesisCache()
    assert cache.lookup(key).status == "miss"
    cache.put(key, result)
    assert cache.lookup(key).status == "hit"

    assert work.semantic_enrichment is not None
    second_work = work.model_copy(
        update={
            "analyzer_work_id": "semantic-work-other",
            "semantic_enrichment": work.semantic_enrichment.model_copy(
                update={"failure_cluster_id": "other-cluster"}
            ),
        }
    )
    second_key = semantic_cache_key(second_work, model=submission.role_run.model)
    second_result = result.model_copy(
        update={
            "analyzer_work_id": second_work.analyzer_work_id,
            "failure_cluster_id": "other-cluster",
            "accepted": (),
            "rejected": (),
        }
    )
    cache.put(second_key, second_result)
    untouched = next(
        node.node_id
        for node in graph.nodes
        if node.node_id not in {
            result.accepted[0].source_node_id,
            result.accepted[0].target_node_id,
        }
    )
    invalidation = cache.invalidate_touched({untouched})
    assert not invalidation.invalidated_key_ids
    invalidation = cache.invalidate_touched({result.accepted[0].source_node_id})
    assert key.key_id in invalidation.invalidated_key_ids
    assert second_key.key_id in invalidation.retained_key_ids


def test_semantic_edges_have_bounded_location_value_but_no_authority(tmp_path: Path) -> None:
    graph, work, submission = _fixture(tmp_path)
    by_label = {node.label: node for node in graph.nodes}
    other = by_label["ease"]
    false_high_confidence = submission.semantic_relation_proposals[0].model_copy(
        update={
            "proposal_id": "adversarial-high-confidence",
            "target": SemanticNodeAnchor(
                node_id=other.node_id,
                content_hash=other.content_hash,
                excerpt="bounded excerpt for ease",
            ),
            "relation_type": SemanticRelationType.CONFLICTS_WITH,
            "rationale_zh": "对抗样例: 高置信度声明也不能获得授权。",
            "confidence": 1.0,
        }
    )
    submission = submission.model_copy(
        update={"semantic_relation_proposals": (false_high_confidence,)}
    )
    layered, _ = SemanticHypothesisEngine().evaluate(
        work, submission, graph, project_root=tmp_path
    )
    script = by_label["validate_gif"]
    peer = by_label["is_slack_ready"]
    failure_slice = FailureSlice(
        package_id=graph.package_id,
        seed_node_ids=(other.node_id,),
        nodes=(
            FailureSliceNode(
                node_id=other.node_id,
                rank=1,
                distance=0,
                score=1.0,
                reason="fixture failure",
            ),
        ),
        omitted_nodes=0,
        token_estimate=1,
    )
    context = SelectionContext(
        graph=layered,
        targets=(_target(script), _target(peer), _target(other)),
        failure_slices=(failure_slice,),
        evidence_refs=("fixture:evidence",),
    )
    selected = GraphGuidedComponentSelector().select(context, limit=3)
    script_row = next(item for item in selected.selected if item.node_id == script.node_id)
    semantic_feature = next(
        item for item in script_row.contributions if item.feature == "semantic_hypothesis_support"
    )
    assert 0 < semantic_feature.contribution <= 0.35
    assert script_row.eligible
    other_row = next(item for item in selected.selected if item.node_id == other.node_id)
    other_semantic = next(
        item for item in other_row.contributions if item.feature == "semantic_hypothesis_support"
    )
    # This adversarial relation names both targets, so both endpoints receive
    # bounded support, while an unrelated function in the same file receives none.
    assert 0 < other_semantic.contribution <= 0.35
    peer_row = next(item for item in selected.selected if item.node_id == peer.node_id)
    peer_semantic = next(
        item for item in peer_row.contributions if item.feature == "semantic_hypothesis_support"
    )
    assert peer_semantic.contribution == 0
    base = GraphGuidedComponentSelector().select(
        context.model_copy(update={"graph": graph}), limit=3
    )
    base_by_id = {item.node_id: item for item in base.selected}
    assert all(
        row.score - base_by_id[row.node_id].score <= 0.35 + 1e-8
        for row in selected.selected
    )

    for consumer in (
        SemanticConsumer.PATCH_AUTHORIZATION,
        SemanticConsumer.TARGET_SET_EXPANSION,
        SemanticConsumer.TRUSTED_DEPENDENCY_CLOSURE,
        SemanticConsumer.SAFETY_CLOSURE,
        SemanticConsumer.MERGE_ELIGIBILITY,
        SemanticConsumer.MERGE_CONFLICT_RESOLUTION,
        SemanticConsumer.VALIDATION_GATE,
    ):
        assert work.semantic_enrichment is not None
        assert not semantic_consumer_decision(
            layered, consumer, work.semantic_enrichment.config
        ).allowed

    chosen, target_set = choose_bounded_target_set(
        layered,
        (_rank(script, 1), _rank(other, 2)),
        parent_candidate_id="same-parent",
        evidence_refs=("fixture:evidence",),
        scope_reason="semantic-only connection must not authorize a companion",
        max_targets=2,
    )
    assert len(chosen) == 1 and target_set is None
    assert dependency_closure(graph, {script.node_id}) == dependency_closure(
        layered, {script.node_id}
    )


def test_semantic_localization_is_opt_in_and_report_is_visibly_untrusted(
    tmp_path: Path,
) -> None:
    graph, work, submission = _fixture(tmp_path)
    layered, _ = SemanticHypothesisEngine().evaluate(
        work, submission, graph, project_root=tmp_path
    )
    semantic_edge = next(edge for edge in layered.edges if edge.layer == "semantic_hypothesis")
    without = reverse_slice(layered, (semantic_edge.target,), max_nodes=10)
    with_hypothesis = reverse_slice(
        layered,
        (semantic_edge.target,),
        max_nodes=10,
        include_semantic_hypotheses=True,
    )
    assert semantic_edge.source not in {item.node_id for item in without.nodes}
    assert semantic_edge.source in {item.node_id for item in with_hypothesis.nodes}
    html = render_graph_report(layered)
    assert "semantic_hypothesis" in html
    assert "stroke-dasharray" in html
    assert "Agent 假设" in html and "非事实" in html


def test_disabled_semantic_consumers_are_not_silently_used(tmp_path: Path) -> None:
    graph, work, submission = _fixture(tmp_path)
    assert work.semantic_enrichment is not None
    explanation_only = work.semantic_enrichment.config.model_copy(
        update={"allowed_consumers": (SemanticConsumer.ASI_EXPLANATION,)}
    )
    work = work.model_copy(
        update={
            "semantic_enrichment": work.semantic_enrichment.model_copy(
                update={"config": explanation_only}
            )
        }
    )
    layered, _ = SemanticHypothesisEngine().evaluate(
        work, submission, graph, project_root=tmp_path
    )
    edge = next(item for item in layered.edges if item.layer == "semantic_hypothesis")
    sliced = reverse_slice(
        layered,
        (edge.target,),
        max_nodes=10,
        include_semantic_hypotheses=True,
    )
    assert edge.source not in {item.node_id for item in sliced.nodes}

    nodes = {node.label: node for node in layered.nodes}
    context = SelectionContext(
        graph=layered,
        targets=(_target(nodes["validate_gif"]), _target(nodes["ease"])),
        failure_slices=(
            FailureSlice(
                package_id=layered.package_id,
                seed_node_ids=(nodes["ease"].node_id,),
                nodes=(
                    FailureSliceNode(
                        node_id=nodes["ease"].node_id,
                        rank=1,
                        distance=0,
                        score=1.0,
                        reason="fixture failure",
                    ),
                ),
                omitted_nodes=0,
                token_estimate=1,
            ),
        ),
        evidence_refs=("fixture:evidence",),
    )
    selected = GraphGuidedComponentSelector().select(context, limit=2)
    assert all(
        next(
            item
            for item in row.contributions
            if item.feature == "semantic_hypothesis_support"
        ).contribution
        == 0
        for row in selected.selected
    )
