from pathlib import Path

from gepase.mutation.applier import apply_package_patch
from gepase.mutation.proposer import (
    PatchProposalWorkItem,
    PatchTargetSnapshot,
    build_patch_submission,
    draft_replacement_proposal,
)
from gepase.mutation.schema import PatchEditBudget, PatchOperationKind
from gepase.optimizer.candidate import build_seed_candidate
from gepase.optimizer.selectors import FeatureContribution, RankedSelection
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.ir import NodeKind


def test_replace_text_file_applies_full_auditable_skill_edit(tmp_path: Path) -> None:
    root = Path.cwd().resolve()
    package_ref = "benchmarks/skills/structured-report-builder"
    package_root = root / package_ref
    parent = build_seed_candidate(root, package_ref, run_id="replace-text-test")
    graph = PackageAnalyzer().analyze(package_root).graph
    target = next(
        node
        for node in graph.nodes
        if node.kind is NodeKind.FILE and node.path == "SKILL.md"
    )
    content = (package_root / "SKILL.md").read_text(encoding="utf-8")
    selection = RankedSelection(
        rank=1,
        node_id=target.node_id,
        path=target.path,
        locator=target.locator,
        score=1.0,
        contributions=(
            FeatureContribution(
                feature="fixture",
                raw_value=1.0,
                weight=1.0,
                contribution=1.0,
            ),
        ),
        evidence_refs=("fixture:replace-text",),
        reason_code="fixture",
    )
    work = PatchProposalWorkItem(
        work_id="patch-work-replace-text",
        run_id="replace-text-test",
        task_id="fixture",
        parent_candidate_id=parent.candidate_id,
        parent_snapshot_hash=parent.snapshot_hash,
        parent_content_hash=parent.content_hash,
        selector="fixture",
        targets=(
            PatchTargetSnapshot(
                node_id=target.node_id,
                node_kind=target.kind.value,
                path=target.path,
                locator=target.locator,
                content_hash=target.content_hash,
                content=content,
                selection=selection,
            ),
        ),
        allowed_operations=(PatchOperationKind.REPLACE_TEXT_FILE,),
        edit_budget=PatchEditBudget(
            max_operations=1,
            max_changed_files=1,
            max_added_files=0,
            max_deleted_files=0,
            max_total_replacement_chars=50_000,
            allow_file_topology_edits=False,
        ),
        evidence_refs=("fixture:replace-text",),
        actionable_side_information={
            "causal_targets": [
                {
                    "node_id": target.node_id,
                    "failure_evidence_ids": ["failure-fixture"],
                    "causal_path_node_ids": [target.node_id],
                    "allowed_operation_classes": ["replace_text_file"],
                    "expected_affected_metrics": ["fixture_quality"],
                    "executable_target": False,
                }
            ],
            "causal_contract": {"required": True},
        },
        output_instructions="Return JSON.",
    )
    replacement = tmp_path / "SKILL.md"
    replacement.write_text(content + "\n<!-- portable fixture -->\n", encoding="utf-8")
    raw = draft_replacement_proposal(work, replacement, summary="Portable fixture.")
    submission = build_patch_submission(
        work,
        raw,
        host="pytest",
        model="fixture",
        host_task_id="replace-text",
        duration_ms=1,
        token_estimate=1,
    )
    assert submission.patch is not None
    workspace = (
        root
        / "artifacts/local/test-replace-text-file"
        / tmp_path.parent.name
        / tmp_path.name
    )
    application, candidate = apply_package_patch(
        root,
        parent,
        submission.patch,
        workspace,
        run_id="replace-text-test",
    )
    assert candidate is not None
    assert application.workspace_ref is not None
    assert "portable fixture" in (
        root / application.workspace_ref / "SKILL.md"
    ).read_text(encoding="utf-8")
