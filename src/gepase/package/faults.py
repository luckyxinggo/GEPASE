"""Materialized structural fault corpus and localization evaluation."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import Field

from gepase.package.analyzer import PackageAnalyzer
from gepase.package.ir import PackageGraph
from gepase.package.slicing import reverse_slice
from gepase.schemas.common import FrozenModel


class FaultOperation(FrozenModel):
    action: Literal["replace", "append", "add", "delete"]
    path: str
    old: str | None = None
    new: str = ""


class FaultCase(FrozenModel):
    case_id: str
    skill_id: str
    fault_family: str
    expected_diagnostic: str
    expected_path: str
    operations: tuple[FaultOperation, ...] = Field(min_length=1)


def load_fault_cases(path: Path) -> tuple[FaultCase, ...]:
    cases = tuple(
        FaultCase.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("fault corpus contains duplicate case ids")
    return cases


def apply_fault(root: Path, case: FaultCase) -> None:
    for operation in case.operations:
        path = (root / operation.path).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError("fault operation escapes package root")
        if operation.action == "add":
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                raise ValueError(f"fault add target already exists: {operation.path}")
            path.write_text(operation.new, encoding="utf-8")
        elif operation.action == "delete":
            path.unlink()
        elif operation.action == "append":
            path.write_text(
                path.read_text(encoding="utf-8") + operation.new,
                encoding="utf-8",
            )
        elif operation.action == "replace":
            if operation.old is None:
                raise ValueError("replace fault requires old text")
            text = path.read_text(encoding="utf-8")
            if operation.old not in text:
                raise ValueError(
                    f"fault replacement source absent in {case.case_id}: {operation.old}"
                )
            path.write_text(text.replace(operation.old, operation.new, 1), encoding="utf-8")


def evaluate_fault_corpus(project_root: Path, corpus: Path) -> dict[str, object]:
    cases = load_fault_cases(corpus)
    detected = 0
    family_total: Counter[str] = Counter()
    family_detected: Counter[str] = Counter()
    false_positive_types: Counter[str] = Counter()
    rows: list[dict[str, object]] = []
    analyzer = PackageAnalyzer()
    temporary_root = project_root / "artifacts/local"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gepase-faults-", dir=temporary_root) as temp:
        for case in cases:
            source = project_root / "benchmarks/skills" / case.skill_id
            target = Path(temp) / case.case_id
            shutil.copytree(source, target)
            apply_fault(target, case)
            result = analyzer.analyze(target)
            kinds = [item.kind for item in result.graph.diagnostics]
            passed = case.expected_diagnostic in kinds
            detected += int(passed)
            family_total[case.fault_family] += 1
            family_detected[case.fault_family] += int(passed)
            unexpected = sorted(set(kinds) - {case.expected_diagnostic})
            false_positive_types.update(unexpected)
            rows.append(
                {
                    "case_id": case.case_id,
                    "skill_id": case.skill_id,
                    "fault_family": case.fault_family,
                    "expected_diagnostic": case.expected_diagnostic,
                    "observed_diagnostics": kinds,
                    "detected": passed,
                    "unexpected_diagnostics": unexpected,
                }
            )
    recall = detected / len(cases) if cases else 0.0
    return {
        "valid": len(cases) >= 30 and recall >= 0.95,
        "cases": len(cases),
        "detected": detected,
        "recall": recall,
        "family_metrics": {
            family: {
                "cases": family_total[family],
                "detected": family_detected[family],
                "recall": family_detected[family] / family_total[family],
            }
            for family in sorted(family_total)
        },
        "false_positive_types": dict(sorted(false_positive_types.items())),
        "rows": rows,
    }


def evaluate_localization(project_root: Path, corpus: Path) -> dict[str, object]:
    cases = load_fault_cases(corpus)
    hits = 0
    misses: list[dict[str, str]] = []
    rows: list[dict[str, object]] = []
    analyzer = PackageAnalyzer()
    temporary_root = project_root / "artifacts/local"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gepase-localize-", dir=temporary_root) as temp:
        for case in cases:
            source = project_root / "benchmarks/skills" / case.skill_id
            target = Path(temp) / case.case_id
            shutil.copytree(source, target)
            apply_fault(target, case)
            graph = analyzer.analyze(target).graph
            matching = [
                item
                for item in graph.diagnostics
                if item.kind == case.expected_diagnostic
            ]
            seeds = tuple(
                dict.fromkeys(
                    node_id
                    for diagnostic in matching
                    for node_id in diagnostic.related_node_ids
                )
            )
            failure_slice = reverse_slice(graph, seeds, max_nodes=20, max_tokens=2_000)
            hit = _top_k_path_hit(graph, failure_slice, case.expected_path, 5)
            hits += int(hit)
            if not hit:
                misses.append(
                    {
                        "case_id": case.case_id,
                        "fault_family": case.fault_family,
                        "error_type": "expected_path_outside_top5",
                    }
                )
            rows.append(
                {
                    "case_id": case.case_id,
                    "expected_path": case.expected_path,
                    "top5": [
                        _node_path(graph, item.node_id) for item in failure_slice.nodes[:5]
                    ],
                    "hit": hit,
                    "slice": failure_slice.model_dump(mode="json"),
                }
            )
    recall = hits / len(cases) if cases else 0.0
    return {
        "valid": len(cases) >= 30 and recall >= 0.8,
        "cases": len(cases),
        "top5_hits": hits,
        "top5_recall": recall,
        "misses": misses,
        "rows": rows,
    }


def _node_path(graph: PackageGraph, node_id: str) -> str:
    return next(node.path for node in graph.nodes if node.node_id == node_id)


def _top_k_path_hit(
    graph: PackageGraph,
    failure_slice: object,
    expected_path: str,
    k: int,
) -> bool:
    from gepase.package.ir import FailureSlice

    typed = (
        failure_slice
        if isinstance(failure_slice, FailureSlice)
        else FailureSlice.model_validate(failure_slice)
    )
    return any(_node_path(graph, item.node_id) == expected_path for item in typed.nodes[:k])
