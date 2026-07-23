"""Causal contract validation for bounded PackagePatch proposals."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import Field

from gepase.mutation.proposer import PatchProposalSubmission, PatchProposalWorkItem
from gepase.mutation.schema import PackagePatch
from gepase.schemas.common import FrozenModel


class CausalOperationBinding(FrozenModel):
    operation_id: str
    failure_evidence_id: str
    target_node_id: str
    causal_path_node_ids: tuple[str, ...] = Field(min_length=1)
    expected_affected_assertions: tuple[str, ...] = ()
    expected_affected_metrics: tuple[str, ...] = Field(min_length=1)
    executable_target: bool


def bind_causal_operations(
    work: PatchProposalWorkItem,
    patch: PackagePatch,
) -> tuple[CausalOperationBinding, ...]:
    raw_targets = work.actionable_side_information.get("causal_targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValueError("proposal work lacks causal targets")
    by_node: dict[str, dict[str, Any]] = {}
    for value in raw_targets:
        if isinstance(value, dict) and value.get("node_id"):
            by_node[str(value["node_id"])] = value
    bindings: list[CausalOperationBinding] = []
    for operation in patch.operations:
        target_id = getattr(operation, "target_node_id", None)
        if target_id is None or str(target_id) not in by_node:
            raise ValueError(
                f"operation {operation.operation_id} has no exported failure-to-node contract"
            )
        target = by_node[str(target_id)]
        allowed = {str(item) for item in target.get("allowed_operation_classes", [])}
        if operation.op.value not in allowed:
            raise ValueError(
                f"operation {operation.operation_id} is not causal-contract compatible"
            )
        evidence_ids = tuple(str(item) for item in target.get("failure_evidence_ids", []))
        metrics = tuple(str(item) for item in target.get("expected_affected_metrics", []))
        path = tuple(str(item) for item in target.get("causal_path_node_ids", []))
        if not evidence_ids or not metrics or not path:
            raise ValueError("causal target lacks evidence, path, or expected metric")
        bindings.append(
            CausalOperationBinding(
                operation_id=operation.operation_id,
                failure_evidence_id=evidence_ids[0],
                target_node_id=str(target_id),
                causal_path_node_ids=path,
                expected_affected_assertions=tuple(
                    str(item) for item in target.get("expected_affected_assertions", [])
                ),
                expected_affected_metrics=metrics,
                executable_target=bool(target.get("executable_target", False)),
            )
        )
    return tuple(bindings)


def audit_causality(
    work_items: Iterable[PatchProposalWorkItem],
    submissions: Iterable[PatchProposalSubmission],
) -> dict[str, object]:
    by_work = {item.work_id: item for item in work_items}
    proposals = 0
    operations = 0
    missing_path = 0
    missing_metrics = 0
    out_of_scope = 0
    executable_target_excluded = 0
    rows: list[dict[str, object]] = []
    for submission in submissions:
        if submission.patch is None:
            continue
        proposals += 1
        work = by_work.get(submission.work_id)
        if work is None:
            out_of_scope += len(submission.patch.operations)
            continue
        target_ids = {item.node_id for item in work.targets}
        for operation in submission.patch.operations:
            operations += 1
            if str(getattr(operation, "target_node_id", "")) not in target_ids:
                out_of_scope += 1
        try:
            bindings = bind_causal_operations(work, submission.patch)
        except ValueError as error:
            rows.append({"work_id": work.work_id, "valid": False, "error": str(error)})
            missing_path += 1
            missing_metrics += 1
            continue
        for binding in bindings:
            missing_path += int(not binding.causal_path_node_ids)
            missing_metrics += int(not binding.expected_affected_metrics)
        failure = work.actionable_side_information.get("failure_evidence", {})
        behavioral = isinstance(failure, dict) and failure.get("kind") in {
            "failed_assertion",
            "observed_failure",
            "low_score",
            "judge_disagreement",
        }
        exported_executable = any(
            bool(item.get("executable_target"))
            for item in work.actionable_side_information.get("causal_targets", [])
            if isinstance(item, dict)
        )
        chosen_executable = any(item.executable_target for item in bindings)
        executable_target_excluded += int(
            behavioral and exported_executable and not chosen_executable
        )
        rows.append(
            {
                "work_id": work.work_id,
                "valid": True,
                "bindings": [item.model_dump(mode="json") for item in bindings],
            }
        )
    valid = (
        proposals > 0
        and operations > 0
        and missing_path == 0
        and missing_metrics == 0
        and out_of_scope == 0
        and executable_target_excluded == 0
    )
    return {
        "schema_version": "1.0.0",
        "valid": valid,
        "proposals": proposals,
        "operations": operations,
        "causal_path_missing": missing_path,
        "expected_affected_metrics_missing": missing_metrics,
        "scope_errors": out_of_scope,
        "behavioral_executable_target_excluded": executable_target_excluded,
        "rows": rows,
    }
