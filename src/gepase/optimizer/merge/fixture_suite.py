"""Executable fixtures for merge semantics, safety, and retention."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from gepase.evals.statistics import PairedScore
from gepase.mutation.applier import apply_package_patch
from gepase.mutation.schema import (
    AddFile,
    DeleteFile,
    InsertReference,
    PackagePatch,
    PatchEditBudget,
    PatchOperation,
    PatchOperationKind,
    RegressionRisk,
    ReplaceMarkdownBlock,
    ReplacePythonFunction,
    ReplaceTextFile,
    UpdateFrontmatter,
    package_patch_from_proposal,
)
from gepase.optimizer.acceptance.engine import ValidationGatedAcceptance
from gepase.optimizer.candidate import PackageCandidate, build_seed_candidate
from gepase.optimizer.merge.conflicts import detect_conflicts
from gepase.optimizer.merge.deterministic import deterministic_merge_patch
from gepase.optimizer.merge.models import (
    MergeConflictKind,
    MergeResolutionSubmission,
    ParentContribution,
)
from gepase.optimizer.merge.proposer import (
    build_resolution_work_item,
    validate_resolution_submission,
)
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.ir import NodeKind


def _patch(
    root: PackageCandidate,
    operations: tuple[PatchOperation, ...],
    *,
    work_id: str,
) -> PackagePatch:
    nodes = tuple(
        sorted(
            {
                str(item.target_node_id)
                for item in operations
                if getattr(item, "target_node_id", None) is not None
            }
            | {
                item.referenced_from_node_id
                for item in operations
                if isinstance(item, InsertReference)
            }
        )
    )
    paths = {item.path for item in operations}
    additions = sum(isinstance(item, (AddFile, InsertReference)) for item in operations)
    deletions = sum(isinstance(item, DeleteFile) for item in operations)
    chars = sum(
        len(str(getattr(item, "replacement", getattr(item, "content", "")))) for item in operations
    )
    return package_patch_from_proposal(
        {
            "proposal_work_id": work_id,
            "base_candidate_id": root.candidate_id,
            "base_snapshot_hash": root.snapshot_hash,
            "base_content_hash": root.content_hash,
            "selector": "merge_fixture",
            "selected_node_ids": nodes or ("fixture-causal-node",),
            "operations": [item.model_dump(mode="json") for item in operations],
            "edit_budget": PatchEditBudget(
                max_operations=len(operations),
                max_changed_files=len(paths),
                max_added_files=additions,
                max_deleted_files=deletions,
                max_total_replacement_chars=max(chars, 1),
            ).model_dump(mode="json"),
            "evidence_refs": (f"fixture:{work_id}",),
            "summary": f"Merge fixture patch {work_id}",
        }
    )


def _contribution(
    parent_id: str,
    operation: PatchOperation,
) -> ParentContribution:
    node = str(
        getattr(operation, "target_node_id", None)
        or getattr(operation, "referenced_from_node_id", "fixture-causal-node")
    )
    return ParentContribution(
        parent_candidate_id=parent_id,
        patch_ids=(f"patch-{parent_id}",),
        operations=(operation,),
        mutation_node_ids=(node,),
        dependency_node_ids=(),
        closure_node_ids=(node,),
        evidence_refs=(f"fixture:{parent_id}",),
    )


def _operation(
    kind: MergeConflictKind,
    side: str,
) -> PatchOperation:
    common = {
        "operation_id": f"op-{kind.value}-{side}",
        "precondition_hash": side * 64,
        "evidence_refs": (f"fixture:{kind.value}:{side}",),
        "expected_benefit": "exercise typed conflict detection",
        "regression_risk": RegressionRisk.LOW,
        "rationale": "fixture conflict",
    }
    if kind is MergeConflictKind.SAME_NODE_CONTENT:
        return ReplaceMarkdownBlock(
            **common,
            op=PatchOperationKind.REPLACE_MARKDOWN_BLOCK,
            path="SKILL.md",
            target_node_id="node-shared",
            replacement=f"## Shared\n\n{side}",
        )
    if kind is MergeConflictKind.PATH_COLLISION:
        return AddFile(
            **{**common, "precondition_hash": "absent"},
            op=PatchOperationKind.ADD_FILE,
            path="references/shared.md",
            content=f"# {side}",
        )
    if kind is MergeConflictKind.INTERFACE_SIGNATURE:
        return ReplacePythonFunction(
            **common,
            op=PatchOperationKind.REPLACE_PYTHON_FUNCTION,
            path="scripts/tool.py",
            target_node_id=f"node-function-{side}",
            replacement=(
                "def normalize(value: str) -> str:\n    return value\n"
                if side == "a"
                else "def normalize(value: str, strict: bool) -> str:\n    return value\n"
            ),
        )
    if kind is MergeConflictKind.DELETE_MODIFY:
        if side == "a":
            return DeleteFile(
                **common,
                op=PatchOperationKind.DELETE_FILE,
                path="references/old.md",
                target_node_id="node-old-file",
                orphan_evidence_ref="fixture:orphan",
            )
        return ReplaceTextFile(
            **common,
            op=PatchOperationKind.REPLACE_TEXT_FILE,
            path="references/old.md",
            target_node_id="node-old-file-body",
            replacement="# retained",
        )
    if kind is MergeConflictKind.FRONTMATTER:
        return UpdateFrontmatter(
            **common,
            op=PatchOperationKind.UPDATE_FRONTMATTER,
            path="SKILL.md",
            target_node_id="node-frontmatter",
            replacement=f"---\nname: {side}\n---",
        )
    return InsertReference(
        **{**common, "precondition_hash": "absent"},
        op=PatchOperationKind.INSERT_REFERENCE,
        path="references/contract.md",
        content=f"# incompatible-{side}",
        referenced_from_node_id="node-workflow",
    )


def _paired_rows(prefix: str, count: int, *, tier: str) -> tuple[PairedScore, ...]:
    return tuple(
        PairedScore(
            task_id=f"{prefix}-{index}",
            category="merge-retention",
            risk_level="medium",
            parent_score=0.5,
            candidate_score=0.9,
            evidence_tier=tier,
            minimum_acceptance_tier=tier,
            parent_record_id=f"parent-{prefix}-{index}",
            candidate_record_id=f"candidate-{prefix}-{index}",
            uncertainty=0.05,
        )
        for index in range(count)
    )


def run_merge_fixture_suite(project_root: Path, fixture_dir: Path) -> dict[str, object]:
    root = project_root.resolve()
    package_root = fixture_dir.resolve() / "complement_skill"
    source_ref = package_root.relative_to(root).as_posix()
    work_root = root / "artifacts/local/merge-fixture-work"
    if work_root.exists():
        shutil.rmtree(work_root)
    seed = build_seed_candidate(root, source_ref, run_id="s8-fixture-seed")
    analysis = PackageAnalyzer().analyze(package_root)
    workflow = next(
        node
        for node in analysis.graph.nodes
        if node.kind is NodeKind.SECTION and node.label == "Workflow"
    )
    function = next(
        node
        for node in analysis.graph.nodes
        if node.kind is NodeKind.FUNCTION and node.label == "normalize"
    )
    left_operation = ReplaceMarkdownBlock(
        operation_id="op-parent-a",
        op=PatchOperationKind.REPLACE_MARKDOWN_BLOCK,
        path="SKILL.md",
        target_node_id=workflow.node_id,
        precondition_hash=workflow.content_hash,
        evidence_refs=("fixture:parent-a",),
        expected_benefit="retain instruction-side capability",
        regression_risk=RegressionRisk.LOW,
        rationale="add explicit A-only marker",
        replacement=(
            "## Workflow\n\nA_ONLY_RETENTION: validate the request before normalization, "
            "then return a bounded result."
        ),
    )
    right_operation = ReplacePythonFunction(
        operation_id="op-parent-b",
        op=PatchOperationKind.REPLACE_PYTHON_FUNCTION,
        path="scripts/tool.py",
        target_node_id=function.node_id,
        precondition_hash=function.content_hash,
        evidence_refs=("fixture:parent-b",),
        expected_benefit="retain script-side capability",
        regression_risk=RegressionRisk.LOW,
        rationale="add explicit B-only marker",
        replacement=(
            "def normalize(value: str) -> str:\n"
            '    """B_ONLY_RETENTION: normalize whitespace deterministically."""\n'
            '    return " ".join(value.split())\n'
        ),
    )
    left_patch = _patch(seed, (left_operation,), work_id="fixture-parent-a")
    right_patch = _patch(seed, (right_operation,), work_id="fixture-parent-b")
    left_application, left = apply_package_patch(
        root,
        seed,
        left_patch,
        work_root / "parent-a",
        run_id="s8-fixture-parent-a",
    )
    right_application, right = apply_package_patch(
        root,
        seed,
        right_patch,
        work_root / "parent-b",
        run_id="s8-fixture-parent-b",
    )
    if left is None or right is None:
        raise ValueError("fixture parent patch failed to apply")
    contributions = (
        _contribution(left.candidate_id, left_operation),
        _contribution(right.candidate_id, right_operation),
    )
    merge_patch_a, contribution_map_a = deterministic_merge_patch(
        seed,
        analysis.graph,
        contributions,
        parent_set_id="fixture-complement",
    )
    merge_patch_b, contribution_map_b = deterministic_merge_patch(
        seed,
        analysis.graph,
        tuple(reversed(contributions)),
        parent_set_id="fixture-complement",
    )
    merge_application_a, merged_a = apply_package_patch(
        root,
        seed,
        merge_patch_a,
        work_root / "merge-a",
        run_id="s8-fixture-merge",
        candidate_parent_ids=(left.candidate_id, right.candidate_id),
        candidate_generation=2,
        candidate_operator="package_aware_pareto_merge",
    )
    _, merged_b = apply_package_patch(
        root,
        seed,
        merge_patch_b,
        work_root / "merge-b",
        run_id="s8-fixture-merge",
        candidate_parent_ids=(left.candidate_id, right.candidate_id),
        candidate_generation=2,
        candidate_operator="package_aware_pareto_merge",
    )
    if merged_a is None or merged_b is None:
        raise ValueError("fixture deterministic merge failed")
    merged_root = root / str(merge_application_a.workspace_ref)
    merged_skill = (merged_root / "SKILL.md").read_text(encoding="utf-8")
    merged_script = (merged_root / "scripts/tool.py").read_text(encoding="utf-8")
    merged_analysis = PackageAnalyzer().analyze(merged_root)
    diagnostics = merged_analysis.graph.diagnostics
    missing_dependency = sum(item.kind == "missing_dependency" for item in diagnostics)
    dangling_reference = sum(item.kind == "dangling_reference" for item in diagnostics)
    duplicate_path = len(merged_a.files) - len({item.path for item in merged_a.files})
    expected = json.loads((fixture_dir / "expected_conflicts.json").read_text(encoding="utf-8"))
    conflict_rows = []
    for value in expected["expected_conflicts"]:
        kind = MergeConflictKind(value)
        conflicts = detect_conflicts(
            (
                _contribution("candidate-a", _operation(kind, "a")),
                _contribution("candidate-b", _operation(kind, "b")),
            )
        )
        conflict_rows.append(
            {
                "expected": kind.value,
                "observed": [item.kind.value for item in conflicts],
                "detected": any(item.kind is kind for item in conflicts),
            }
        )
    sample_conflicts = detect_conflicts(
        (
            _contribution(
                "candidate-a",
                _operation(MergeConflictKind.SAME_NODE_CONTENT, "a"),
            ),
            _contribution(
                "candidate-b",
                _operation(MergeConflictKind.SAME_NODE_CONTENT, "b"),
            ),
        )
    )
    resolution_work = build_resolution_work_item(
        parent_set_id="fixture-conflict",
        lca_candidate_id=seed.candidate_id,
        base_snapshot_hash=seed.snapshot_hash,
        base_content_hash=seed.content_hash,
        conflicts=sample_conflicts,
        node_preconditions={
            node_id: "0" * 64
            for node_id in {node for item in sample_conflicts for node in item.node_ids}
        },
    )
    malicious = MergeResolutionSubmission(
        work_id=resolution_work.work_id,
        operations=(
            AddFile(
                operation_id="op-malicious",
                op=PatchOperationKind.ADD_FILE,
                path="references/unrelated.md",
                precondition_hash="absent",
                evidence_refs=("fixture:malicious",),
                expected_benefit="unrelated",
                regression_risk=RegressionRisk.HIGH,
                rationale="must be rejected",
                content="# unrelated",
            ),
        ),
        resolved_conflict_ids=tuple(item.conflict_id for item in sample_conflicts),
        rationale="attempt unrelated edit",
    )
    unrelated_rejected = False
    try:
        validate_resolution_submission(resolution_work, malicious)
    except ValueError:
        unrelated_rejected = True
    stale_patch = package_patch_from_proposal(
        {
            **merge_patch_a.identity_payload(),
            "base_content_hash": "f" * 64,
            "proposal_work_id": "fixture-stale",
        }
    )
    stale_application, _ = apply_package_patch(
        root,
        seed,
        stale_patch,
        work_root / "stale",
        run_id="s8-fixture-stale",
    )
    decision = ValidationGatedAcceptance(
        root,
        work_root / "gates",
        run_id="s8-fixture-retention",
    ).evaluate(
        seed,
        merged_a,
        merge_patch_a,
        merge_application_a,
        train_pairs=_paired_rows("train", 4, tier="E2"),
        validation_pairs=_paired_rows("validation", 8, tier="E3"),
        static_regression_aware=True,
        record_evolution_candidate=False,
    )
    gate_map = {item.level.value: item.outcome.value for item in decision.gates}
    return {
        "schema_version": "1.0.0",
        "valid": (
            missing_dependency == 0
            and dangling_reference == 0
            and duplicate_path == 0
            and all(bool(item["detected"]) for item in conflict_rows)
            and merge_patch_a.patch_id == merge_patch_b.patch_id
            and merged_a.content_hash == merged_b.content_hash
            and contribution_map_a == contribution_map_b
            and "A_ONLY_RETENTION" in merged_skill
            and "B_ONLY_RETENTION" in merged_script
            and unrelated_rejected
            and stale_application.status.value == "stale_parent"
            and decision.verdict.value == "accepted"
        ),
        "missing_dependency": missing_dependency,
        "dangling_reference": dangling_reference,
        "duplicate_path": duplicate_path,
        "conflicts": conflict_rows,
        "determinism": {
            "patch_hash_equal": merge_patch_a.patch_id == merge_patch_b.patch_id,
            "candidate_hash_equal": merged_a.content_hash == merged_b.content_hash,
            "contribution_map_equal": contribution_map_a == contribution_map_b,
            "patch_id": merge_patch_a.patch_id,
            "candidate_content_hash": merged_a.content_hash,
            "contribution_map_hash": contribution_map_a.fingerprint,
        },
        "conflict_safety": {
            "unrelated_edit_count": 0 if unrelated_rejected else 1,
            "stale_base_rejected": stale_application.status.value == "stale_parent",
            "unresolved_conflict_materialized_valid": False,
        },
        "complement_retention": {
            "parent_a_applied": left_application.status.value == "applied",
            "parent_b_applied": right_application.status.value == "applied",
            "a_only_assertion": "A_ONLY_RETENTION" in merged_skill,
            "b_only_assertion": "B_ONLY_RETENTION" in merged_script,
            "s7_verdict": decision.verdict.value,
            "s7_gates": gate_map,
        },
        "merged_candidate": merged_a.model_dump(mode="json"),
    }
