from __future__ import annotations

import tempfile
from pathlib import Path

from gepase.mutation.applier import apply_package_patch
from gepase.mutation.proposer import PatchProposalWorkItem, PatchTargetSnapshot
from gepase.mutation.schema import (
    PatchApplicationStatus,
    PatchEditBudget,
    PatchOperationKind,
    package_patch_from_proposal,
)
from gepase.mutation.target_set import choose_bounded_target_set
from gepase.optimizer.candidate import build_seed_candidate
from gepase.optimizer.selectors import FeatureContribution, RankedSelection
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.ir import NodeKind
from gepase.package.loader import load_package

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_REF = "tests/fixtures/graph_hardening/target_set_package"


def _rank(node_id: str, path: str, locator: str, rank: int) -> RankedSelection:
    return RankedSelection(
        rank=rank,
        node_id=node_id,
        path=path,
        locator=locator,
        score=2.0 - rank,
        contributions=(
            FeatureContribution(feature="fixture", raw_value=1.0, weight=1.0, contribution=1.0),
        ),
        evidence_refs=("fixture:failure",),
        reason_code="fixture",
    )


def test_two_target_set_is_graph_connected_bounded_and_fault_atomic() -> None:
    package = ROOT / PACKAGE_REF
    graph = PackageAnalyzer().analyze(package).graph
    instruction = next(
        node
        for node in graph.nodes
        if node.kind is NodeKind.INSTRUCTION and "worker.py" in node.label
    )
    function = next(node for node in graph.nodes if node.kind is NodeKind.FUNCTION)
    selected, target_set = choose_bounded_target_set(
        graph,
        (
            _rank(instruction.node_id, instruction.path, instruction.locator, 1),
            _rank(function.node_id, function.path, function.locator, 2),
        ),
        parent_candidate_id="candidate-fixture",
        evidence_refs=("fixture:failure",),
        scope_reason="The instruction references the script implementing the same repair.",
        max_targets=2,
    )
    assert len(selected) == 2 and target_set is not None
    assert target_set.causal_path_edge_ids
    assert len(target_set.target_node_ids) == 2

    parent = build_seed_candidate(ROOT, PACKAGE_REF, run_id="gh-p0-target-set")
    target_set = target_set.model_copy(update={"parent_candidate_id": parent.candidate_id})
    budget = PatchEditBudget(
        max_operations=2,
        max_changed_files=2,
        max_added_files=0,
        max_deleted_files=0,
        allow_file_topology_edits=False,
    )
    targets = tuple(
        PatchTargetSnapshot(
            node_id=node.node_id,
            node_kind=node.kind.value,
            path=node.path,
            locator=node.locator,
            content_hash=node.content_hash,
            content=(package / node.path).read_text(encoding="utf-8"),
            selection=ranked,
        )
        for ranked, node in zip(selected, (instruction, function), strict=True)
    )
    work = PatchProposalWorkItem(
        work_id="gh-p0-target-set-work",
        run_id="gh-p0",
        task_id="fixture-failure",
        parent_candidate_id=parent.candidate_id,
        parent_snapshot_hash=parent.snapshot_hash,
        parent_content_hash=parent.content_hash,
        selector="graph_guided",
        targets=targets,
        target_set=target_set,
        allowed_operations=(
            PatchOperationKind.REPLACE_MARKDOWN_BLOCK,
            PatchOperationKind.REPLACE_PYTHON_FUNCTION,
        ),
        edit_budget=budget,
        evidence_refs=("fixture:failure",),
        actionable_side_information={"causal_contract": {"required": False}},
        output_instructions="Fixture only; no Agent invocation.",
    )
    patch = package_patch_from_proposal(
        {
            "proposal_work_id": work.work_id,
            "base_candidate_id": parent.candidate_id,
            "base_snapshot_hash": parent.snapshot_hash,
            "base_content_hash": parent.content_hash,
            "selector": "graph_guided",
            "selected_node_ids": [item.node_id for item in targets],
            "operations": [
                {
                    "operation_id": "op-instruction",
                    "op": "replace_markdown_block",
                    "target_node_id": instruction.node_id,
                    "path": instruction.path,
                    "precondition_hash": instruction.content_hash,
                    "replacement": "Read and execute `scripts/worker.py` exactly once.",
                    "evidence_refs": ["fixture:failure"],
                    "expected_benefit": "Bound execution behavior.",
                    "regression_risk": "low",
                    "rationale": "The failure spans instruction and implementation.",
                },
                {
                    "operation_id": "op-script",
                    "op": "replace_python_function",
                    "target_node_id": function.node_id,
                    "path": function.path,
                    "precondition_hash": function.content_hash,
                    "replacement": (
                        "def render(value: str) -> str:\n    return value.strip().lower()\n"
                    ),
                    "evidence_refs": ["fixture:failure"],
                    "expected_benefit": "Make implementation deterministic.",
                    "regression_risk": "medium",
                    "rationale": "The script is the graph-connected implementation.",
                },
            ],
            "edit_budget": budget,
            "evidence_refs": ["fixture:failure"],
            "summary": "Atomic two-target fixture patch.",
        }
    )
    before = load_package(package).snapshot_hash
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gh-p0-target-set-", dir=local) as temporary:
        application, child = apply_package_patch(
            ROOT,
            parent,
            patch,
            Path(temporary),
            run_id="gh-p0",
            fail_after_operations=1,
        )
        assert application.status is PatchApplicationStatus.INVALID
        assert application.error_code == "injected_failure"
        assert child is None
        assert not (Path(temporary) / "applications").exists()
        assert load_package(package).snapshot_hash == before


def test_target_set_skips_overlapping_same_file_locus_before_companion() -> None:
    graph = PackageAnalyzer().analyze(ROOT / PACKAGE_REF).graph
    instruction = next(
        node
        for node in graph.nodes
        if node.kind is NodeKind.INSTRUCTION and "worker.py" in node.label
    )
    overlapping = next(
        node
        for node in graph.nodes
        if node.path == instruction.path
        and node.node_id != instruction.node_id
        and node.kind is NodeKind.FILE
    )
    function = next(node for node in graph.nodes if node.kind is NodeKind.FUNCTION)
    selected, target_set = choose_bounded_target_set(
        graph,
        (
            _rank(instruction.node_id, instruction.path, instruction.locator, 1),
            _rank(overlapping.node_id, overlapping.path, overlapping.locator, 2),
            _rank(function.node_id, function.path, function.locator, 3),
        ),
        parent_candidate_id="candidate-fixture",
        evidence_refs=("fixture:failure",),
        scope_reason="deduplicate physical mutation loci",
        max_targets=2,
    )
    assert tuple(item.node_id for item in selected) == (
        instruction.node_id,
        function.node_id,
    )
    assert target_set is not None
