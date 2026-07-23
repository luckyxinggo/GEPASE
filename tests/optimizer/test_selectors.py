from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from gepase.optimizer.failure_union import FailureSliceInput, build_failure_union
from gepase.optimizer.selectors import (
    SelectionContext,
    SelectionTarget,
    SelectorKind,
    selector_for,
)
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.faults import apply_fault, load_fault_cases
from gepase.package.ir import NodeKind
from gepase.package.slicing import reverse_slice

ROOT = Path(__file__).resolve().parents[2]


def _fault_context() -> tuple[SelectionContext, str]:
    case = load_fault_cases(ROOT / "benchmarks/fault_localization.jsonl")[0]
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.mkdtemp(prefix="selector-test-", dir=local)
    target = Path(temporary) / case.skill_id
    shutil.copytree(ROOT / "benchmarks/skills" / case.skill_id, target)
    apply_fault(target, case)
    graph = PackageAnalyzer().analyze(target).graph
    matching = [item for item in graph.diagnostics if item.kind == case.expected_diagnostic]
    seeds = tuple(node for item in matching for node in item.related_node_ids)
    failure_slice = reverse_slice(graph, seeds, max_nodes=30, max_tokens=3_000)
    targets = tuple(
        SelectionTarget(
            node_id=node.node_id,
            path=node.path,
            locator=node.locator,
            node_kind=node.kind.value,
            content_hash=node.content_hash,
            token_estimate=10,
        )
        for node in graph.nodes
        if node.mutable
        and node.span is not None
        and node.kind
        in {
            NodeKind.FRONTMATTER,
            NodeKind.SECTION,
            NodeKind.INSTRUCTION,
            NodeKind.REFERENCE_CHUNK,
            NodeKind.FUNCTION,
        }
    )
    severity = {
        node_id: 1.0 for item in matching for node_id in item.related_node_ids
    }
    context = SelectionContext(
        graph=graph,
        targets=targets,
        failure_slices=(failure_slice,),
        evidence_refs=(f"fault:{case.case_id}",),
        diagnostic_severity=severity,
        seed=17,
    )
    return context, case.expected_path


def test_all_selectors_share_contract_and_are_deterministic() -> None:
    context, _ = _fault_context()
    for kind in SelectorKind:
        first = selector_for(kind).select(context, limit=3)
        second = selector_for(kind).select(context, limit=3)
        assert first == second
        assert len(first.selected) == 3
        assert all(item.contributions and item.evidence_refs for item in first.selected)


def test_graph_selector_localizes_fault_path() -> None:
    context, expected_path = _fault_context()
    result = selector_for(SelectorKind.GRAPH_GUIDED).select(context, limit=3)
    assert expected_path in {item.path for item in result.selected}
    assert all(item.reason_code == "graph_failure_impact_priority" for item in result.selected)


def test_failure_union_preserves_seed_under_budget() -> None:
    context, _ = _fault_context()
    union = build_failure_union(
        context.graph,
        (
            FailureSliceInput(
                failure_id="fault-1",
                root_cause="broken_reference",
                evidence_ref="fault:1",
                failure_slice=context.failure_slices[0],
            ),
        ),
        max_nodes=3,
        max_tokens=500,
    )
    assert len(union.nodes) <= 3
    assert union.token_estimate <= 500
    assert set(union.failure_seed_ids) & {item.node_id for item in union.nodes}
