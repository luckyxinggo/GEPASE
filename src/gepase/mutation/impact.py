"""Dependency-closure impact and validation-intensity policy."""

from __future__ import annotations

from collections import Counter
from enum import StrEnum

from pydantic import Field

from gepase.mutation.schema import PackagePatch, PatchOperationKind
from gepase.package.ir import NodeKind, PackageGraph, PackageGraphDiff
from gepase.schemas.common import FrozenModel


class ValidationIntensity(StrEnum):
    STANDARD = "standard"
    ELEVATED = "elevated"
    FULL = "full_validation_required"


class ImpactAssessment(FrozenModel):
    schema_version: str = "1.0.0"
    affected_node_ids: tuple[str, ...]
    blast_radius: int = Field(ge=0)
    touched_node_ids: tuple[str, ...]
    high_fan_out_node_ids: tuple[str, ...]
    script_or_api_interface_changed: bool
    topology_changed: bool
    validation_intensity: ValidationIntensity
    reason_codes: tuple[str, ...] = Field(min_length=1)


def assess_impact(
    before: PackageGraph,
    difference: PackageGraphDiff,
    patch: PackagePatch,
) -> ImpactAssessment:
    by_id = {node.node_id: node for node in before.nodes}
    fan_out: Counter[str] = Counter(edge.source for edge in before.edges)
    threshold = max(4, int(max(fan_out.values(), default=0) * 0.65))
    touched = tuple(
        sorted(
            {
                str(item.target_node_id)
                for item in patch.operations
                if item.target_node_id is not None
            }
        )
    )
    high_fan = tuple(sorted(node_id for node_id in touched if fan_out[node_id] >= threshold))
    script = any(
        item.op is PatchOperationKind.REPLACE_PYTHON_FUNCTION
        or (
            item.target_node_id is not None
            and by_id.get(str(item.target_node_id)) is not None
            and by_id[str(item.target_node_id)].kind
            in {NodeKind.FUNCTION, NodeKind.CLASS, NodeKind.ENTRYPOINT}
        )
        for item in patch.operations
    )
    topology = any(
        item.op
        in {
            PatchOperationKind.ADD_FILE,
            PatchOperationKind.INSERT_REFERENCE,
            PatchOperationKind.DELETE_FILE,
        }
        for item in patch.operations
    )
    reasons: list[str] = []
    if high_fan:
        reasons.append("high_fan_out")
    if script:
        reasons.append("script_or_api_interface")
    if topology:
        reasons.append("package_topology")
    if difference.blast_radius >= max(8, len(before.nodes) // 3):
        reasons.append("large_dependency_closure")
    if high_fan or script:
        intensity = ValidationIntensity.FULL
    elif topology or "large_dependency_closure" in reasons:
        intensity = ValidationIntensity.ELEVATED
    else:
        intensity = ValidationIntensity.STANDARD
        reasons.append("bounded_local_change")
    return ImpactAssessment(
        affected_node_ids=difference.affected_closure,
        blast_radius=difference.blast_radius,
        touched_node_ids=touched,
        high_fan_out_node_ids=high_fan,
        script_or_api_interface_changed=script,
        topology_changed=topology,
        validation_intensity=intensity,
        reason_codes=tuple(reasons),
    )
