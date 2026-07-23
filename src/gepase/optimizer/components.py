"""Stable logical components projected from the S3 package IR."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator

from gepase.package.ir import FileKind, PackageIR
from gepase.schemas.common import FrozenModel


class ComponentKind(StrEnum):
    SKILL_INSTRUCTIONS = "skill_instructions"
    REFERENCE_CHUNK = "reference_chunk"
    SCRIPT_UNIT = "script_unit"
    ROUTING_METADATA = "routing_metadata"


class PackageComponent(FrozenModel):
    component_id: str
    source_node_id: str
    kind: ComponentKind
    path: str
    locator: str = "file"
    content: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_component(self) -> PackageComponent:
        path = Path(self.path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("component path must be package-relative")
        actual = hashlib.sha256(self.content.encode()).hexdigest()
        if actual != self.content_hash:
            raise ValueError("component content_hash does not match content")
        return self


def _role(path: str, file_kind: FileKind) -> ComponentKind | None:
    if path == "SKILL.md":
        return ComponentKind.SKILL_INSTRUCTIONS
    if file_kind is FileKind.REFERENCE:
        return ComponentKind.REFERENCE_CHUNK
    if file_kind is FileKind.SCRIPT:
        return ComponentKind.SCRIPT_UNIT
    if file_kind in {FileKind.AGENT_CONFIG, FileKind.METADATA}:
        return ComponentKind.ROUTING_METADATA
    return None


def register_components(package_root: Path, package_ir: PackageIR) -> tuple[PackageComponent, ...]:
    """Expose mutable file units using the stable S3 file-node identity.

    S5 deliberately uses reliable whole-file units for materialization. The identity and role are
    supplied by the S3 IR; S6 may later select these units with graph evidence, but this registry
    contains no graph-guided policy.
    """

    root = package_root.resolve(strict=True)
    file_nodes = {
        node.path: node
        for node in package_ir.nodes
        if node.kind.value == "file" and node.mutable
    }
    components: list[PackageComponent] = []
    for path, node in sorted(file_nodes.items()):
        raw_kind = node.metadata.get("file_kind")
        try:
            file_kind = FileKind(str(raw_kind))
        except ValueError:
            continue
        role = _role(path, file_kind)
        if role is None:
            continue
        target = (root / path).resolve(strict=True)
        if not target.is_relative_to(root) or not target.is_file():
            raise ValueError(f"component escapes package root: {path}")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        component_id = f"component-{node.node_id.removeprefix('node-')}"
        components.append(
            PackageComponent(
                component_id=component_id,
                source_node_id=node.node_id,
                kind=role,
                path=path,
                content=content,
                content_hash=hashlib.sha256(content.encode()).hexdigest(),
            )
        )
    if not any(item.kind is ComponentKind.SKILL_INSTRUCTIONS for item in components):
        raise ValueError("component registry did not expose SKILL.md")
    priority = {
        ComponentKind.SKILL_INSTRUCTIONS: 0,
        ComponentKind.REFERENCE_CHUNK: 1,
        ComponentKind.SCRIPT_UNIT: 2,
        ComponentKind.ROUTING_METADATA: 3,
    }
    return tuple(sorted(components, key=lambda item: (priority[item.kind], item.path)))
