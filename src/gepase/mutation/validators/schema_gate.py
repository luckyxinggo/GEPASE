"""Gate 0: local PackagePatch schema, scope, precondition, and policy validation."""

from __future__ import annotations

from gepase.mutation.schema import PackagePatch
from gepase.optimizer.acceptance.models import (
    GateLevel,
    GateOutcome,
    GateResult,
)
from gepase.optimizer.candidate import PackageCandidate
from gepase.package.ir import PackageGraph
from gepase.store.rejected import RejectedEditStore


def run_schema_gate(
    parent: PackageCandidate,
    patch: PackagePatch,
    graph: PackageGraph,
    *,
    rejected_store: RejectedEditStore | None = None,
    block_exact_rejected: bool = True,
) -> GateResult:
    checks: dict[str, object] = {}
    reasons: list[str] = []
    checks["base_candidate_match"] = patch.base_candidate_id == parent.candidate_id
    checks["base_content_hash_match"] = patch.base_content_hash == parent.content_hash
    checks["base_snapshot_hash_match"] = patch.base_snapshot_hash == parent.snapshot_hash
    by_id = {node.node_id: node for node in graph.nodes}
    target_checks = []
    for operation in patch.operations:
        if operation.target_node_id is None:
            target_checks.append(operation.precondition_hash == "absent")
            continue
        node = by_id.get(operation.target_node_id)
        target_checks.append(
            node is not None
            and node.path == operation.path
            and node.content_hash == operation.precondition_hash
            and operation.target_node_id in patch.selected_node_ids
        )
    checks["target_preconditions_match"] = all(target_checks)
    exact = (
        rejected_store.exact(patch.fingerprint, parent.candidate_id)
        if rejected_store is not None
        else None
    )
    checks["exact_rejected_history"] = exact.record_id if exact else None
    if not checks["base_candidate_match"] or not checks["base_content_hash_match"]:
        reasons.append("stale_parent")
    if not checks["base_snapshot_hash_match"] or not checks["target_preconditions_match"]:
        reasons.append("stale_precondition")
    if exact is not None and block_exact_rejected:
        reasons.append("rejected_patch_repetition")
    passed = not reasons
    return GateResult(
        level=GateLevel.GATE_0_SCHEMA,
        outcome=GateOutcome.PASSED if passed else GateOutcome.FAILED,
        reason_codes=("schema_valid",) if passed else tuple(reasons),
        human_summary=(
            "PackagePatch schema, scope, hashes, and edit budget are valid."
            if passed
            else "PackagePatch failed local schema, precondition, or rejected-memory checks."
        ),
        evidence_refs=tuple(patch.evidence_refs),
        checks=checks,
        target_calls=0,
    )
