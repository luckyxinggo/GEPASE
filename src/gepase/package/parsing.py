"""Parser-neutral relation facts emitted before graph target resolution."""

from __future__ import annotations

from dataclasses import dataclass, field

from gepase.package.ir import EdgeKind, IRNode


@dataclass(frozen=True)
class RelationFact:
    source: str
    kind: EdgeKind
    target_path: str | None = None
    target_locator: str | None = None
    external_name: str | None = None
    reason: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedFile:
    nodes: tuple[IRNode, ...]
    relations: tuple[RelationFact, ...]
