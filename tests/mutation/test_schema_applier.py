from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from gepase.mutation.applier import apply_package_patch, rollback_application
from gepase.mutation.schema import (
    PackagePatch,
    PatchApplicationStatus,
    PatchEditBudget,
    package_patch_from_proposal,
)
from gepase.optimizer.candidate import PackageCandidate, build_seed_candidate
from gepase.optimizer.materialize import materialize_candidate
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.ir import NodeKind
from gepase.package.loader import load_package

ROOT = Path(__file__).resolve().parents[2]


def _patch(*, precondition: str | None = None) -> tuple[PackageCandidate, PackagePatch]:
    parent = build_seed_candidate(
        ROOT,
        "benchmarks/skills/structured-report-builder",
        run_id="s6-applier-test",
    )
    analysis = PackageAnalyzer().analyze(
        ROOT / "benchmarks/skills/structured-report-builder"
    )
    node = next(
        item
        for item in analysis.graph.nodes
        if item.kind is NodeKind.INSTRUCTION and item.path == "SKILL.md"
    )
    line = (
        ROOT / "benchmarks/skills/structured-report-builder/SKILL.md"
    ).read_text(encoding="utf-8").splitlines()[node.span.start_line - 1]  # type: ignore[union-attr]
    patch = package_patch_from_proposal(
        {
            "proposal_work_id": "patch-work-test",
            "base_candidate_id": parent.candidate_id,
            "base_snapshot_hash": parent.snapshot_hash,
            "base_content_hash": parent.content_hash,
            "selector": "graph_guided",
            "selected_node_ids": [node.node_id],
            "operations": [
                {
                    "operation_id": "op-clarify",
                    "op": "replace_markdown_block",
                    "target_node_id": node.node_id,
                    "path": node.path,
                    "precondition_hash": precondition or node.content_hash,
                    "replacement": line + " Preserve exact source representations.",
                    "evidence_refs": ["record:test"],
                    "expected_benefit": "Clarify source fidelity.",
                    "regression_risk": "low",
                    "rationale": "The failure trace identifies silent coercion risk.",
                }
            ],
            "edit_budget": PatchEditBudget(),
            "evidence_refs": ["record:test"],
            "summary": "Clarify exact representation preservation.",
        }
    )
    return parent, patch


def test_schema_rejects_path_escape_and_unknown_op() -> None:
    _, patch = _patch()
    payload = patch.model_dump(mode="json")
    payload["operations"][0]["path"] = "../../outside"
    with pytest.raises(ValidationError):
        PackagePatch.model_validate(payload)
    payload = patch.model_dump(mode="json")
    payload["operations"][0]["op"] = "shell_write"
    with pytest.raises(ValidationError):
        PackagePatch.model_validate(payload)


def test_atomic_apply_materialize_and_rollback() -> None:
    parent, patch = _patch()
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="s6-apply-", dir=local) as temporary:
        application, child = apply_package_patch(
            ROOT, parent, patch, Path(temporary), run_id="s6-applier-test"
        )
        assert application.status is PatchApplicationStatus.APPLIED
        assert child is not None
        assert application.graph_diff is not None
        assert application.file_changes
        assert application.original_workspace_hash_unchanged
        output = Path(temporary) / "roundtrip/package"
        manifest = materialize_candidate(ROOT, child, output)
        assert manifest.file_set_equal
        assert manifest.content_hash_equal
        rolled_back = rollback_application(ROOT, parent, application)
        assert rolled_back.status is PatchApplicationStatus.ROLLED_BACK
        assert rolled_back.rollback and rolled_back.rollback.verified


def test_stale_parent_and_fault_injection_leave_no_partial_candidate() -> None:
    parent, stale = _patch(precondition="0" * 64)
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="s6-stale-", dir=local) as temporary:
        application, child = apply_package_patch(
            ROOT, parent, stale, Path(temporary), run_id="s6-applier-test"
        )
        assert application.status is PatchApplicationStatus.STALE_PARENT
        assert child is None
        assert not (Path(temporary) / "applications").exists()
    parent, valid = _patch()
    source_hash = load_package(ROOT / parent.source_package_ref).snapshot_hash
    with tempfile.TemporaryDirectory(prefix="s6-fault-", dir=local) as temporary:
        application, child = apply_package_patch(
            ROOT,
            parent,
            valid,
            Path(temporary),
            run_id="s6-applier-test",
            fail_after_operations=1,
        )
        assert application.status is PatchApplicationStatus.INVALID
        assert application.error_code == "injected_failure"
        assert child is None
        assert not (Path(temporary) / "applications").exists()
        assert load_package(ROOT / parent.source_package_ref).snapshot_hash == source_hash
