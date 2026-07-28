"""Prepare and finalize the bounded GH-P1 semantic-hypothesis experiment."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from stage_gate_support import (
    load_json_object,
    protected_tree_hashes,
    run_command,
    tree_hash,
)

from gepase.evals.functional import (
    AnalysisNodeHint,
    AnalyzerSubmission,
    AnalyzerWorkItem,
)
from gepase.optimizer.graph_selector import GraphGuidedComponentSelector
from gepase.optimizer.selectors import SelectionContext, SelectionResult, SelectionTarget
from gepase.package.ir import FailureSlice, IRNode, NodeKind, PackageGraph
from gepase.package.semantic import (
    SemanticHypothesisCache,
    SemanticHypothesisEngine,
    graph_fingerprint,
    semantic_cache_key,
    semantic_consumer_decision,
    trusted_graph_view,
)
from gepase.package.semantic_models import (
    ALLOWED_SEMANTIC_CONSUMERS,
    SemanticConsumer,
    SemanticEnrichmentScope,
    SemanticHypothesisConfig,
    SemanticNodeContext,
    SemanticRelationType,
)
from gepase.package.slicing import reverse_slice
from gepase.reporting.graph_report import render_graph_report
from gepase.schemas.common import ArtifactRef
from gepase.store.artifacts import ArtifactStore, canonical_json_bytes, sha256_bytes

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "artifacts/stages/GH-P1"
GH_P0 = ROOT / "artifacts/stages/GH-P0"
R2 = ROOT / "artifacts/runs/r2-slack-gif-creator-evalplan"
R3 = ROOT / "artifacts/runs/r3-slack-gif-creator-paired"
R4 = ROOT / "artifacts/runs/r4-slack-gif-creator-evolution"
PACKAGE = ROOT / "benchmarks/canaries/slack-gif-creator/package"
BASE_ANALYZER_ID = "analyzer-work-66550acf708dcf7d83b689cb"
FAILURE_TASK_ID = "functional-train-input-badge-003"
PROPOSAL_WORK_ID = "proposal-work-ab7f56118676006487a9edc3"
GH_P1_WORK_ID = "analyzer-work-gh-p1-input-badge-loop-seam"
RAW_SUBMISSION = STAGE / "agent/analyzer-raw-submission.json"
GRAPH_REF = "artifacts/stages/GH-P0/new-package-graph.json"
PROMPT_REF = "src/gepase/evals/prompts/semantic_enrichment_analyzer.md"
SCHEMA_REF = "schemas/analyzer_submission.schema.json"
ALLOWED_NODE_IDS = (
    "node-0f241aa06504163be33132f8",  # validators.py file
    "node-774bca90618f6a6cbf560caa",  # validate_gif function
    "node-46d940221fa8caf3b6c22d2c",  # validator instruction
    "node-2c61585b9d40c2df7343f499",  # gif_builder.py file
    "node-f592ff3e34f1b63061471b9c",  # GIFBuilder.save function
    "node-39e1aff632e5776f04740a41",  # GIFBuilder instruction
    "node-a464c33893d3283bf7b3ea7f",  # imageio dependency
)


def _load(path: Path) -> dict[str, Any]:
    return load_json_object(path, root=ROOT)


def _relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def _artifact_ref(path: Path, media_type: str = "application/json") -> ArtifactRef:
    payload = path.read_bytes()
    return ArtifactRef(
        path=_relative(path),
        sha256=sha256_bytes(payload),
        media_type=media_type,
        size_bytes=len(payload),
    )


def _protected_hashes() -> dict[str, object]:
    return protected_tree_hashes(
        ROOT,
        public_canary_source=PACKAGE,
        extra_stage_ids=("GH-P0",),
    )


def _run(command: tuple[str, ...], commands: list[str]) -> dict[str, object]:
    return run_command(command, root=ROOT, commands=commands)


def _git_value(*args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout.strip()


def _node_excerpt(node: IRNode) -> str:
    path = PACKAGE / node.path
    if not path.is_file():
        return node.label[:1_200]
    text = path.read_text(encoding="utf-8")
    span = node.span
    if span is not None:
        lines = text.splitlines()
        text = "\n".join(lines[span.start_line - 1 : span.end_line])
    return text[:1_200] or node.label[:1_200]


def _prepare_work(graph: PackageGraph) -> AnalyzerWorkItem:
    base = AnalyzerWorkItem.model_validate_json(
        (R3 / f"analyzer-work-items/{BASE_ANALYZER_ID}.json").read_text(encoding="utf-8")
    )
    by_id = {node.node_id: node for node in graph.nodes}
    missing = set(ALLOWED_NODE_IDS) - set(by_id)
    if missing:
        raise ValueError(f"bounded semantic nodes absent from GH-P0 graph: {sorted(missing)}")
    allowed_nodes = tuple(
        SemanticNodeContext(
            node_id=by_id[node_id].node_id,
            path=by_id[node_id].path,
            locator=by_id[node_id].locator,
            kind=by_id[node_id].kind.value,
            label=by_id[node_id].label,
            content_hash=by_id[node_id].content_hash,
            excerpt=_node_excerpt(by_id[node_id]),
        )
        for node_id in ALLOWED_NODE_IDS
    )
    evidence_paths = (
        R3 / f"analyzer-submissions/{BASE_ANALYZER_ID}.json",
        R3 / "deterministic/work-4d807d3a99ff3d159ec6fb29.json",
        R3 / "grader-submissions/grader-work-2c8028d145484e7e5af04b94.json",
        R3 / "records/record-af968875d3c07bdc96f582cf.json",
        R3 / "package-access/work-4d807d3a99ff3d159ec6fb29.json",
        R4 / f"proposal-work-items/{PROPOSAL_WORK_ID}.json",
    )
    config = SemanticHypothesisConfig()
    scope = SemanticEnrichmentScope(
        package_id=graph.package_id,
        package_snapshot_hash=graph.snapshot_hash,
        failure_cluster_id="loop-seam-validation-and-gifbuilder-path",
        failure_summary_zh=(
            "original 产物虽通过既有确定性断言, 但循环首尾跳变且 GIFBuilder 因 imageio "
            "不可用而被执行器绕过; 需要判断验证指令、validate_gif、GIFBuilder.save 与 "
            "imageio 依赖之间是否存在静态/观测图未表达的语义关系。"
        ),
        evidence_artifacts=tuple(_artifact_ref(path) for path in evidence_paths),
        allowed_nodes=allowed_nodes,
        prompt_ref=PROMPT_REF,
        prompt_hash=sha256_bytes((ROOT / PROMPT_REF).read_bytes()),
        schema_ref=SCHEMA_REF,
        schema_hash=sha256_bytes((ROOT / SCHEMA_REF).read_bytes()),
        config=config,
    )
    return base.model_copy(
        update={
            "analyzer_work_id": GH_P1_WORK_ID,
            "package_graph_ref": GRAPH_REF,
            "node_hints": tuple(
                AnalysisNodeHint(
                    node_id=item.node_id,
                    path=item.path,
                    kind=item.kind,
                    label=item.label,
                )
                for item in allowed_nodes
            ),
            "submission_schema_ref": SCHEMA_REF,
            "semantic_enrichment": scope,
        }
    )


def _targets(graph: PackageGraph) -> tuple[SelectionTarget, ...]:
    kinds = {
        NodeKind.FILE,
        NodeKind.FRONTMATTER,
        NodeKind.SECTION,
        NodeKind.INSTRUCTION,
        NodeKind.REFERENCE_CHUNK,
        NodeKind.FUNCTION,
        NodeKind.CONFIG_KEY,
    }
    return tuple(
        SelectionTarget(
            node_id=node.node_id,
            path=node.path,
            locator=node.locator,
            node_kind=node.kind.value,
            content_hash=node.content_hash,
            token_estimate=max(1, (len(node.label) + len(str(node.metadata))) // 4),
        )
        for node in graph.nodes
        if node.mutable
        and (node.span is not None or node.kind in {NodeKind.FILE, NodeKind.CONFIG_KEY})
        and node.kind in kinds
    )


def _selection_context(graph: PackageGraph) -> SelectionContext:
    proposal = _load(R4 / f"proposal-work-items/{PROPOSAL_WORK_ID}.json")
    failure_slice = FailureSlice.model_validate(
        proposal["actionable_side_information"]["graph_slice"]
    )
    return SelectionContext(
        graph=graph,
        targets=_targets(graph),
        failure_slices=(failure_slice,),
        evidence_refs=(
            f"artifacts/runs/r3-slack-gif-creator-paired/analyzer-submissions/"
            f"{BASE_ANALYZER_ID}.json",
        ),
        diagnostic_severity={node_id: 1.0 for node_id in failure_slice.seed_node_ids},
    )


def _gate(gate_id: str, passed: bool, detail: str, evidence: list[str]) -> dict[str, object]:
    return {
        "gate_id": gate_id,
        "status": "passed" if passed else "failed",
        "detail": detail,
        "evidence": evidence,
    }


def prepare() -> int:
    STAGE.mkdir(parents=True, exist_ok=True)
    gh_p0_gate = _load(GH_P0 / "machine-gates.json")
    gh_p0_value = _load(GH_P0 / "offline-value-gate.json")
    graph = PackageGraph.model_validate_json(
        (GH_P0 / "new-package-graph.json").read_text(encoding="utf-8")
    )
    work = _prepare_work(graph)
    commands: list[str] = []
    targeted = _run(
        (
            "uv",
            "--cache-dir",
            "/private/tmp/gepase-ghp1-uv-cache",
            "run",
            "pytest",
            "-q",
            "tests/package/test_semantic_hypotheses.py",
        ),
        commands,
    )
    ruff = _run(
        (
            "uv",
            "--cache-dir",
            "/private/tmp/gepase-ghp1-uv-cache",
            "run",
            "ruff",
            "check",
            "src/gepase/package/semantic.py",
            "src/gepase/package/semantic_models.py",
            "tests/package/test_semantic_hypotheses.py",
        ),
        commands,
    )
    pyright = _run(
        (
            "uv",
            "--cache-dir",
            "/private/tmp/gepase-ghp1-uv-cache",
            "run",
            "pyright",
            "src/gepase/package/semantic.py",
            "src/gepase/package/semantic_models.py",
            "tests/package/test_semantic_hypotheses.py",
        ),
        commands,
    )
    fixture_valid = all(bool(item["ok"]) for item in (targeted, ruff, pyright))
    forbidden_consumers = sorted(
        item.value
        for item in SemanticConsumer
        if item not in ALLOWED_SEMANTIC_CONSUMERS
    )
    relation_schema = {
        "schema_version": "1.0.0",
        "layer": "semantic_hypothesis",
        "trust_label": "Agent 假设",
        "relation_types": [item.value for item in SemanticRelationType],
        "required_proposal_fields": [
            "existing source/target node",
            "content hash and excerpt/span anchor",
            "task and failure cluster",
            "bounded evidence references",
            "rationale, confidence, timestamp",
            "work/context/model/prompt/schema/config provenance",
        ],
        "allowed_consumers": [item.value for item in ALLOWED_SEMANTIC_CONSUMERS],
        "forbidden_consumers": forbidden_consumers,
        "semantic_edge_is_never_trusted_fact": True,
    }
    fixture_audit = {
        "schema_version": "1.0.0",
        "valid": fixture_valid,
        "fixture_first": True,
        "tests": {
            "typed_relation_and_provenance": "passed" if targeted["ok"] else "failed",
            "unknown_stale_cross_scope_rejection": (
                "passed" if targeted["ok"] else "failed"
            ),
            "layer_and_visual_trust_label": "passed" if targeted["ok"] else "failed",
            "consumer_allowlist": "passed" if targeted["ok"] else "failed",
            "exact_cache_and_precise_invalidation": (
                "passed" if targeted["ok"] else "failed"
            ),
            "high_confidence_has_no_authority": "passed" if targeted["ok"] else "failed",
            "no_semantic_input_degrades_to_trusted_graph": (
                "passed" if targeted["ok"] else "failed"
            ),
        },
        "commands": {"pytest": targeted, "ruff": ruff, "pyright": pyright},
    }
    gates = [
        _gate(
            "GHP1-G00-preflight_and_scope",
            bool(
                gh_p0_gate.get("valid")
                and gh_p0_value.get("valid")
                and _git_value("branch", "--show-current") == "codex/graph-hardening"
                and work.semantic_enrichment is not None
                and len(work.semantic_enrichment.allowed_nodes) <= 12
            ),
            (
                "GH-P0 passed; one public failure cluster, bounded nodes and one "
                "Analyzer call are frozen."
            ),
            ["preflight.json", "analyzer-work-item.json", "resolved-config.json"],
        ),
        _gate(
            "GHP1-G01-typed_relation_rejection",
            bool(targeted["ok"]),
            (
                "Relation enum, existing-node, snapshot/content/evidence/provenance "
                "checks passed fixtures."
            ),
            ["fixture-gates.json", "resolved-relation-schema.json"],
        ),
        _gate(
            "GHP1-G02-layer_and_display",
            bool(targeted["ok"]),
            "Semantic hypotheses remain a separate dashed Agent 假设 layer.",
            ["fixture-gates.json"],
        ),
        _gate(
            "GHP1-G03-consumer_allowlist",
            bool(targeted["ok"]),
            "Only localization/explanation/top-k/exploration consumers are enabled.",
            ["fixture-gates.json", "resolved-relation-schema.json"],
        ),
        _gate(
            "GHP1-G04-exact_cache",
            bool(targeted["ok"]),
            "Exact cache identity and touched-node-only invalidation passed fixtures.",
            ["fixture-gates.json"],
        ),
        _gate(
            "GHP1-G05-adversarial_and_degradation",
            fixture_valid,
            (
                "False high-confidence relations lack authority; absent semantic input "
                "preserves trusted behavior."
            ),
            ["fixture-gates.json"],
        ),
    ]
    preflight = {
        "schema_version": "1.0.0",
        "stage_id": "GH-P1",
        "branch": _git_value("branch", "--show-current"),
        "head": _git_value("rev-parse", "HEAD"),
        "fixture_first": True,
        "fixture_gates_valid": fixture_valid and all(item["status"] == "passed" for item in gates),
        "protected_before": _protected_hashes(),
        "sealed_inputs": {
            "gh_p0_artifact_index": _artifact_ref(GH_P0 / "artifact-index.json").model_dump(
                mode="json"
            ),
            "gh_p0_graph": _artifact_ref(GH_P0 / "new-package-graph.json").model_dump(
                mode="json"
            ),
            "r3_analyzer_work": _artifact_ref(
                R3 / f"analyzer-work-items/{BASE_ANALYZER_ID}.json"
            ).model_dump(mode="json"),
            "r3_analyzer_submission": _artifact_ref(
                R3 / f"analyzer-submissions/{BASE_ANALYZER_ID}.json"
            ).model_dump(mode="json"),
            "r4_failure_slice_work": _artifact_ref(
                R4 / f"proposal-work-items/{PROPOSAL_WORK_ID}.json"
            ).model_dump(mode="json"),
        },
        "call_budget_before_agent": {
            "agent": 0,
            "headless_api": 0,
            "executor": 0,
            "grader": 0,
            "comparator": 0,
            "analyzer": 0,
            "proposer": 0,
            "eval": 0,
            "new_candidate": 0,
            "new_skill_effect_score": 0,
        },
        "maximum_stage_calls": {"analyzer": 1, "all_other_roles_and_apis": 0},
        "forbidden_features": {
            "graphrag": False,
            "codebase_memory_mcp": False,
            "vector_database": False,
            "file_watcher": False,
            "binary_asset_mutation": False,
            "gh_p2": False,
        },
    }
    if work.semantic_enrichment is None:
        raise ValueError("semantic scope disappeared after validation")
    store = ArtifactStore(STAGE)
    store.write_json("preflight.json", preflight)
    store.write_json(
        "resolved-config.json", work.semantic_enrichment.config.model_dump(mode="json")
    )
    store.write_json("resolved-relation-schema.json", relation_schema)
    store.write_json("analyzer-work-item.json", work.model_dump(mode="json"))
    store.write_json("fixture-gates.json", fixture_audit)
    store.write_json(
        "adversarial-tests.json",
        {
            "schema_version": "1.0.0",
            "status": "passed" if fixture_valid else "failed",
            "cases": [
                "unknown node",
                "stale content hash",
                "evidence outside work item",
                "invalid relation type",
                "below confidence threshold",
                "high-confidence false relation cannot authorize TargetSet or merge closure",
                "no semantic input preserves trusted graph behavior",
            ],
            "source": "tests/package/test_semantic_hypotheses.py",
        },
    )
    store.write_json(
        "prepare-gates.json",
        {
            "schema_version": "1.0.0",
            "valid": fixture_valid and all(item["status"] == "passed" for item in gates),
            "gates": gates,
        },
    )
    store.write_text("commands.log", "\n".join(commands) + "\n")
    return 0 if fixture_valid and all(item["status"] == "passed" for item in gates) else 1


def _submission_boundary(
    work: AnalyzerWorkItem,
    submission: AnalyzerSubmission,
    graph: PackageGraph,
) -> tuple[bool, list[str]]:
    problems: list[str] = []
    if submission.analyzer_work_id != work.analyzer_work_id:
        problems.append("analyzer_work_id_mismatch")
    if not submission.analyses:
        problems.append("missing_failure_analysis")
    if work.semantic_enrichment is None:
        problems.append("missing_semantic_scope")
        return False, problems
    allowed_refs = {item.path for item in work.semantic_enrichment.evidence_artifacts}
    allowed_nodes = {item.node_id for item in work.semantic_enrichment.allowed_nodes}
    graph_nodes = {item.node_id for item in graph.nodes}
    for analysis in submission.analyses:
        if not set(analysis.evidence_refs) <= allowed_refs:
            problems.append("analysis_evidence_outside_work_item")
        if not set(analysis.target_node_ids) <= allowed_nodes:
            problems.append("analysis_target_outside_bounded_nodes")
        if not set(analysis.target_node_ids) <= graph_nodes:
            problems.append("analysis_target_unknown")
    existing_contexts = {
        item["role_run"]["context_id"]
        for path in (R3 / "analyzer-submissions").glob("*.json")
        for item in [_load(path)]
    }
    if submission.role_run.context_id in existing_contexts:
        problems.append("analyzer_context_reused")
    return not problems, list(dict.fromkeys(problems))


def _rank_map(result: SelectionResult) -> dict[str, Any]:
    return {item.node_id: item for item in result.selected}


def finalize() -> int:
    if not RAW_SUBMISSION.is_file():
        raise FileNotFoundError("isolated Analyzer raw submission is absent")
    preflight = _load(STAGE / "preflight.json")
    prepared = _load(STAGE / "prepare-gates.json")
    if not prepared.get("valid"):
        raise ValueError("fixture Gates did not pass; Analyzer output cannot be ingested")
    work = AnalyzerWorkItem.model_validate_json(
        (STAGE / "analyzer-work-item.json").read_text(encoding="utf-8")
    )
    graph = PackageGraph.model_validate_json(
        (GH_P0 / "new-package-graph.json").read_text(encoding="utf-8")
    )
    raw_validation_error: str | None = None
    submission: AnalyzerSubmission | None = None
    try:
        submission = AnalyzerSubmission.model_validate_json(
            RAW_SUBMISSION.read_text(encoding="utf-8")
        )
    except ValidationError as error:
        raw_validation_error = str(error)
    boundary_valid = False
    boundary_problems: list[str] = ["typed_submission_invalid"]
    if submission is not None:
        boundary_valid, boundary_problems = _submission_boundary(work, submission, graph)

    layered = graph
    overlay = None
    engine_error: str | None = None
    if submission is not None and boundary_valid:
        try:
            layered, overlay = SemanticHypothesisEngine().evaluate(
                work,
                submission,
                graph,
                project_root=ROOT,
            )
        except ValueError as error:
            engine_error = str(error)

    accepted = tuple(overlay.accepted) if overlay is not None else ()
    rejected = tuple(overlay.rejected) if overlay is not None else ()
    base_context = _selection_context(graph)
    layered_context = base_context.model_copy(update={"graph": layered})
    base_selection = GraphGuidedComponentSelector().select(
        base_context, limit=len(base_context.targets)
    )
    layered_selection = GraphGuidedComponentSelector().select(
        layered_context, limit=len(layered_context.targets)
    )
    base_ranks = _rank_map(base_selection)
    layered_ranks = _rank_map(layered_selection)
    semantic_rows: list[dict[str, object]] = []
    for node_id, row in layered_ranks.items():
        contribution = next(
            (
                item
                for item in row.contributions
                if item.feature == "semantic_hypothesis_support"
            ),
            None,
        )
        if contribution is None or contribution.contribution <= 0:
            continue
        before = base_ranks[node_id]
        semantic_rows.append(
            {
                "node_id": node_id,
                "path": row.path,
                "locator": row.locator,
                "rank_before": before.rank,
                "rank_after": row.rank,
                "rank_delta": before.rank - row.rank,
                "score_before": before.score,
                "score_after": row.score,
                "score_delta": round(row.score - before.score, 8),
                "semantic_feature": contribution.model_dump(mode="json"),
                "eligible_unchanged": before.eligible == row.eligible,
                "validation_intensity_unchanged": (
                    before.validation_intensity == row.validation_intensity
                ),
            }
        )
    consumer_decisions = []
    if work.semantic_enrichment is None:
        raise ValueError("semantic work scope is missing")
    role_token_total = (
        submission.role_run.usage.input_tokens + submission.role_run.usage.output_tokens
        if submission is not None
        else 0
    )
    role_duration_ms = (
        submission.role_run.usage.duration_ms if submission is not None else 0
    )
    usage_within_budget = bool(
        submission is not None
        and role_token_total <= work.semantic_enrichment.config.analyzer_token_budget
        and role_duration_ms <= work.semantic_enrichment.config.analyzer_timeout_ms
    )
    for consumer in SemanticConsumer:
        decision = semantic_consumer_decision(
            layered, consumer, work.semantic_enrichment.config
        )
        consumer_decisions.append(decision.model_dump(mode="json"))
    seed_ids = base_context.failure_slices[0].seed_node_ids
    trusted_slice = reverse_slice(layered, seed_ids, max_nodes=30)
    semantic_slice = reverse_slice(
        layered,
        seed_ids,
        max_nodes=30,
        include_semantic_hypotheses=True,
    )
    trusted_nodes = {item.node_id for item in trusted_slice.nodes}
    semantic_nodes = {item.node_id for item in semantic_slice.nodes}
    semantic_score_cap_respected = all(
        isinstance(item["score_delta"], (int, float))
        and item["score_delta"]
        <= work.semantic_enrichment.config.selector_weight_cap + 1e-8
        for item in semantic_rows
    )
    location_value = bool(accepted and semantic_rows and semantic_score_cap_respected)
    consumer_trace = {
        "schema_version": "1.0.0",
        "allowed_and_forbidden_consumers": consumer_decisions,
        "selector_semantic_rows": semantic_rows,
        "localization": {
            "trusted_node_count": len(trusted_nodes),
            "semantic_opt_in_node_count": len(semantic_nodes),
            "semantic_only_reached_node_ids": sorted(semantic_nodes - trusted_nodes),
        },
        "asi_explanation": [
            {
                "proposal_id": item.proposal_id,
                "source_node_id": item.source_node_id,
                "target_node_id": item.target_node_id,
                "relation_type": item.relation_type.value,
                "status": item.status.value,
            }
            for item in (*accepted, *rejected)
        ],
        "bounded_location_value": location_value,
        "semantic_score_cap_respected": semantic_score_cap_respected,
        "high_impact_authority_granted": False,
    }

    cache = SemanticHypothesisCache()
    cache_audit: dict[str, object] = {"schema_version": "1.0.0", "available": False}
    if submission is not None and overlay is not None:
        key = semantic_cache_key(work, model=submission.role_run.model)
        before_lookup = cache.lookup(key)
        cache.put(key, overlay)
        after_lookup = cache.lookup(key)
        touched_node_id = (
            accepted[0].source_node_id
            if accepted
            else work.semantic_enrichment.allowed_nodes[0].node_id
        )
        changed_index = next(
            index
            for index, node in enumerate(work.semantic_enrichment.allowed_nodes)
            if node.node_id == touched_node_id
        )
        changed_context = work.semantic_enrichment.allowed_nodes[changed_index].model_copy(
            update={"content_hash": "0" * 64}
        )
        changed_nodes = list(work.semantic_enrichment.allowed_nodes)
        changed_nodes[changed_index] = changed_context
        changed_scope = work.semantic_enrichment.model_copy(
            update={"allowed_nodes": tuple(changed_nodes)}
        )
        changed_work = work.model_copy(update={"semantic_enrichment": changed_scope})
        changed_key = semantic_cache_key(changed_work, model=submission.role_run.model)
        changed_lookup = cache.lookup(changed_key)
        invalidation = cache.invalidate_touched({changed_context.node_id})
        cache_audit = {
            "schema_version": "1.0.0",
            "available": True,
            "exact_key": key.model_dump(mode="json"),
            "first_lookup": before_lookup.model_dump(mode="json"),
            "second_lookup": after_lookup.model_dump(mode="json"),
            "changed_content_key": changed_key.model_dump(mode="json"),
            "changed_content_lookup": changed_lookup.model_dump(mode="json"),
            "invalidation": invalidation.model_dump(mode="json"),
            "valid": bool(
                before_lookup.status == "miss"
                and after_lookup.status == "hit"
                and changed_lookup.status == "miss"
                and key.key_id in invalidation.invalidated_key_ids
            ),
        }

    protected_after = _protected_hashes()
    protected_unchanged = preflight["protected_before"] == protected_after
    agent_calls = 1
    usage = {
        "schema_version": "1.0.0",
        "role_calls": {
            "analyzer": agent_calls,
            "executor": 0,
            "grader": 0,
            "comparator": 0,
            "proposer": 0,
            "headless_api": 0,
        },
        "eval_runs": 0,
        "new_candidates": 0,
        "new_skill_effect_scores": 0,
        "analyzer_role_run": (
            submission.role_run.model_dump(mode="json") if submission is not None else None
        ),
        "frozen_budget": {
            "token_budget": work.semantic_enrichment.config.analyzer_token_budget,
            "timeout_ms": work.semantic_enrichment.config.analyzer_timeout_ms,
        },
        "observed": {
            "input_plus_output_tokens": role_token_total,
            "duration_ms": role_duration_ms,
            "within_budget": usage_within_budget,
        },
    }
    diff = {
        "schema_version": "1.0.0",
        "source_graph_fingerprint": graph_fingerprint(graph),
        "layered_graph_fingerprint": graph_fingerprint(layered),
        "trusted_graph_unchanged": trusted_graph_view(graph) == trusted_graph_view(layered),
        "node_set_unchanged": graph.nodes == layered.nodes,
        "edge_counts_before": dict(Counter(edge.layer for edge in graph.edges)),
        "edge_counts_after": dict(Counter(edge.layer for edge in layered.edges)),
        "semantic_edge_ids": [item.edge_id for item in accepted if item.edge_id],
        "selector_rows_with_semantic_value": semantic_rows,
        "bounded_location_value": location_value,
    }

    commands = (STAGE / "commands.log").read_text(encoding="utf-8").splitlines()
    full_pytest = _run(
        (
            "uv",
            "--cache-dir",
            "/private/tmp/gepase-ghp1-uv-cache",
            "run",
            "pytest",
            "-q",
            "--junitxml",
            "artifacts/stages/GH-P1/test-results.xml",
        ),
        commands,
    )
    ruff = _run(
        (
            "uv",
            "--cache-dir",
            "/private/tmp/gepase-ghp1-uv-cache",
            "run",
            "ruff",
            "check",
            ".",
        ),
        commands,
    )
    pyright = _run(
        (
            "uv",
            "--cache-dir",
            "/private/tmp/gepase-ghp1-uv-cache",
            "run",
            "pyright",
        ),
        commands,
    )
    schema_first = _run(
        (
            "uv",
            "--cache-dir",
            "/private/tmp/gepase-ghp1-uv-cache",
            "run",
            "python",
            "scripts/export_core_schemas.py",
        ),
        commands,
    )
    schemas_first = {
        path.name: path.read_bytes() for path in sorted((ROOT / "schemas").glob("*.json"))
    }
    schema_second = _run(
        (
            "uv",
            "--cache-dir",
            "/private/tmp/gepase-ghp1-uv-cache",
            "run",
            "python",
            "scripts/export_core_schemas.py",
        ),
        commands,
    )
    schemas_second = {
        path.name: path.read_bytes() for path in sorted((ROOT / "schemas").glob("*.json"))
    }
    schema_idempotent = schemas_first == schemas_second
    secrets = _run(
        (
            "uv",
            "--cache-dir",
            "/private/tmp/gepase-ghp1-uv-cache",
            "run",
            "python",
            "scripts/check_secrets.py",
            "--format",
            "json",
        ),
        commands,
    )
    markdown = _run(
        (
            "uv",
            "--cache-dir",
            "/private/tmp/gepase-ghp1-uv-cache",
            "run",
            "python",
            "scripts/check_markdown_links.py",
        ),
        commands,
    )
    license_check = _run(
        (
            "uv",
            "--cache-dir",
            "/private/tmp/gepase-ghp1-uv-cache",
            "run",
            "python",
            "scripts/check_license.py",
        ),
        commands,
    )
    diff_check = _run(("git", "diff", "--check"), commands)
    verification = {
        "schema_version": "1.0.0",
        "checks": {
            "fixture_gates": prepared,
            "full_pytest": full_pytest,
            "ruff": ruff,
            "pyright": pyright,
            "schema_export_first": schema_first,
            "schema_export_second": schema_second,
            "schema_idempotent": schema_idempotent,
            "schema_count": len(schemas_second),
            "secret_and_private_path_scan": secrets,
            "markdown_links": markdown,
            "license": license_check,
            "git_diff_check": diff_check,
        },
    }
    checks_valid = bool(
        prepared.get("valid")
        and schema_idempotent
        and all(
            value.get("ok", True)
            for value in verification["checks"].values()
            if isinstance(value, dict)
        )
    )
    g00_g05 = list(prepared["gates"])
    real_valid = bool(
        submission is not None
        and raw_validation_error is None
        and boundary_valid
        and engine_error is None
        and overlay is not None
        and overlay.trusted_graph_unchanged
        and overlay.source_node_count_unchanged
        and cache_audit.get("valid")
        and agent_calls == 1
        and location_value
        and usage_within_budget
    )
    gates = [
        *g00_g05,
        _gate(
            "GHP1-G06-single_analyzer_viability_and_value",
            real_valid,
            (
                f"One isolated Analyzer produced {len(accepted)} accepted and "
                f"{len(rejected)} rejected hypotheses; bounded location value={location_value}; "
                f"usage within frozen budget={usage_within_budget}."
                if submission is not None
                else "The only isolated Analyzer submission was not schema-valid."
            ),
            [
                "analyzer-work-item.json",
                "agent/analyzer-raw-submission.json",
                "accepted-proposals.json",
                "rejected-proposals.json",
                "consumer-trace.json",
            ],
        ),
        _gate(
            "GHP1-G07-regression_seal_and_immutability",
            checks_valid and protected_unchanged,
            (
                f"Full tests/Ruff/Pyright/{len(schemas_second)} schemas/security/docs/license/"
                f"diff passed={checks_valid}; sealed inputs unchanged={protected_unchanged}."
            ),
            ["verification.json", "preflight.json", "artifact-index.json"],
        ),
    ]
    machine_valid = all(item["status"] == "passed" for item in gates)
    outcome = "complete" if machine_valid else "stalled"
    machine = {
        "schema_version": "1.0.0",
        "stage_id": "GH-P1",
        "valid": machine_valid,
        "outcome": outcome,
        "passed": sum(item["status"] == "passed" for item in gates),
        "failed": sum(item["status"] == "failed" for item in gates),
        "offline_value_gate": {
            "valid": location_value,
            "status": "passed" if location_value else "stalled",
            "accepted_semantic_hypotheses": len(accepted),
            "selector_nodes_with_bounded_semantic_contribution": len(semantic_rows),
            "does_not_claim_skill_improvement": True,
        },
        "gates": gates,
    }
    preflight["protected_after"] = protected_after
    preflight["protected_unchanged"] = protected_unchanged
    preflight["actual_call_budget"] = usage
    preflight["raw_submission_validation_error"] = raw_validation_error
    preflight["submission_boundary_problems"] = boundary_problems
    preflight["semantic_engine_error"] = engine_error
    store = ArtifactStore(STAGE)
    store.index_existing("agent/analyzer-raw-submission.json", "application/json")
    store.write_json("preflight.json", preflight)
    if submission is not None:
        store.write_json("analyzer-submission.json", submission.model_dump(mode="json"))
    else:
        store.write_json(
            "analyzer-submission.json",
            {"valid": False, "validation_error": raw_validation_error},
        )
    store.write_json(
        "accepted-proposals.json",
        {"schema_version": "1.0.0", "rows": [item.model_dump(mode="json") for item in accepted]},
    )
    store.write_json(
        "rejected-proposals.json",
        {
            "schema_version": "1.0.0",
            "rows": [item.model_dump(mode="json") for item in rejected],
            "submission_boundary_problems": boundary_problems,
            "engine_error": engine_error,
        },
    )
    store.write_json("cache-audit.json", cache_audit)
    store.write_json("layered-graph.json", layered.model_dump(mode="json"))
    store.write_json("layered-graph-diff.json", diff)
    store.write_text("layered-graph-report.html", render_graph_report(layered), "text/html")
    store.write_json("consumer-trace.json", consumer_trace)
    store.write_json("usage.json", usage)
    store.write_json("verification.json", verification)
    store.write_json("machine-gates.json", machine)
    store.write_text("commands.log", "\n".join(commands) + "\n")
    if (STAGE / "test-results.xml").is_file():
        store.index_existing("test-results.xml", "application/xml")
    security_final = _run(
        (
            "uv",
            "--cache-dir",
            "/private/tmp/gepase-ghp1-uv-cache",
            "run",
            "python",
            "scripts/check_secrets.py",
            "--format",
            "json",
        ),
        commands,
    )
    if not security_final["ok"]:
        machine["valid"] = False
        machine["outcome"] = "stalled"
        gates[-1]["status"] = "failed"
    store.write_json(
        "post-generation-security.json",
        {
            "schema_version": "1.0.0",
            "valid": security_final["ok"],
            "exit_code": security_final["exit_code"],
            "summary": security_final["summary"],
        },
    )
    verification["checks"]["post_generation_secret_and_private_path_scan"] = security_final
    store.write_json("verification.json", verification)
    store.write_json("machine-gates.json", machine)
    store.write_text("commands.log", "\n".join(commands) + "\n")
    source_scope = [
        ROOT / "src",
        ROOT / "tests",
        ROOT / "scripts",
        ROOT / "schemas",
        ROOT / "state.md",
    ]
    source_digest = hashlib.sha256(
        canonical_json_bytes([tree_hash(path) for path in source_scope])
    ).hexdigest()
    report = {
        "schema_version": "1.0.0",
        "stage_id": "GH-P1",
        "status": "complete" if machine["valid"] else "blocked",
        "stage_outcome": machine["outcome"],
        "started_from_commit": preflight["head"],
        "finished_commit": _git_value("rev-parse", "HEAD"),
        "source_tree_hash": source_digest,
        "generated_at": datetime.now(UTC).isoformat(),
        "input_artifacts": list(preflight["sealed_inputs"].values()),
        "output_artifacts": [
            item
            for item in _load(STAGE / "artifact-index.json")["artifacts"]
            if item["path"] != "stage_report.json"
        ],
        "commands": commands,
        "gate_results": gates,
        "real_agent_runs": 1,
        "headless_provider_runs": 0,
        "metrics": {
            "accepted_semantic_hypotheses": len(accepted),
            "rejected_semantic_hypotheses": len(rejected),
            "selector_nodes_with_semantic_value": len(semantic_rows),
            "machine_gates_passed": sum(item["status"] == "passed" for item in gates),
            "machine_gates_total": len(gates),
            "executor_runs": 0,
            "grader_runs": 0,
            "comparator_runs": 0,
            "proposer_runs": 0,
            "eval_runs": 0,
            "new_candidates": 0,
            "new_skill_effect_scores": 0,
        },
        "known_issues": [
            "GH-P1 validates bounded semantic localization only; it adds no Skill-effect evidence.",
            "Agent semantic relations remain fallible hypotheses even when confidence is high.",
            *(
                ["No explainable localization value was observed; GH-P1 is stalled."]
                if not location_value
                else []
            ),
            *(
                [
                    "The single Analyzer exceeded the frozen token/timeout budget; the "
                    "semantic relations remain auditable but GH-P1 is stalled."
                ]
                if not usage_within_budget
                else []
            ),
        ],
        "design_decisions": [
            "Reuse the existing Analyzer work/submission contract and Core-owned PackageGraph.",
            (
                "Restrict relations to seven types, existing nodes and one bounded "
                "failure neighborhood."
            ),
            "Permit semantic evidence only for localization/explanation/top-k/exploration.",
            (
                "Keep patch authorization, TargetSet, dependency/safety closure, Merge "
                "and Gate trusted-only."
            ),
            "Use exactly one isolated Agent-native Analyzer after fixture Gates pass.",
        ],
        "unlocks": ["GH-P2 planning eligibility"] if machine["valid"] else [],
        "conclusion_boundary": {
            "code_implemented": True,
            "engineering_mechanism_tested": bool(
                checks_valid
                and protected_unchanged
                and overlay is not None
                and overlay.trusted_graph_unchanged
            ),
            "new_algorithm_effect_validated": False,
        },
    }
    store.write_json("stage_report.json", report)
    final = store.verify()
    if not final.valid or final.unindexed_files:
        raise ValueError(f"GH-P1 artifact seal failed: {final.as_dict()}")
    return 0 if machine["valid"] else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "finalize"))
    args = parser.parse_args()
    return prepare() if args.mode == "prepare" else finalize()


if __name__ == "__main__":
    raise SystemExit(main())
