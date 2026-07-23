"""Versioned models for package snapshots, IR nodes, graph edges, and diagnostics."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from gepase.schemas.common import FrozenModel

PACKAGE_IR_SCHEMA_VERSION = "1.0.0"


class FileKind(StrEnum):
    SKILL = "skill"
    REFERENCE = "reference"
    SCRIPT = "script"
    ASSET = "asset"
    TEST = "test"
    AGENT_CONFIG = "agent_config"
    METADATA = "metadata"
    LICENSE = "license"
    UNKNOWN = "unknown"


class NodeKind(StrEnum):
    PACKAGE = "package"
    FILE = "file"
    FRONTMATTER = "frontmatter"
    SECTION = "section"
    INSTRUCTION = "instruction"
    REFERENCE_CHUNK = "reference_chunk"
    PYTHON_MODULE = "python_module"
    IMPORT = "import"
    FUNCTION = "function"
    CLASS = "class"
    CALL = "call"
    ENTRYPOINT = "entrypoint"
    SHELL_COMMAND = "shell_command"
    BINARY = "binary"
    CAPABILITY = "capability"
    DEPENDENCY = "dependency"
    EXTERNAL = "external"
    UNKNOWN = "unknown"
    ERROR = "error"
    EVIDENCE = "evidence"
    ARTIFACT = "artifact"


class EdgeKind(StrEnum):
    CONTAINS = "contains"
    REFERENCES = "references"
    IMPORTS = "imports"
    CALLS = "calls"
    EXECUTES = "executes"
    TESTS = "tests"
    READS = "reads"
    PRODUCES = "produces"
    REQUIRES_HOST = "requires_host"
    USES_TOOL = "uses_tool"
    CALLS_EXTERNAL_SERVICE = "calls_external_service"
    REQUIRES_SECRET = "requires_secret"
    UNKNOWN = "unknown"
    PLANNED_READ = "planned_read"
    PLANNED_EXECUTE = "planned_execute"
    PLANNED_PRODUCE = "planned_produce"
    OBSERVED_READ = "observed_read"
    OBSERVED_EXECUTE = "observed_execute"
    OBSERVED_PRODUCE = "observed_produce"
    FAILED_AT = "failed_at"


class SourceSpan(FrozenModel):
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def ordered(self) -> SourceSpan:
        if self.end_line < self.start_line:
            raise ValueError("source span end precedes start")
        return self


class PackageFile(FrozenModel):
    path: str
    kind: FileKind
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    binary: bool = False
    mutable: bool = True
    reason: str | None = None


class CapabilityFacts(FrozenModel):
    skill_id: str
    required_hosts: tuple[str, ...] = ()
    required_tools: tuple[str, ...] = ()
    required_services: tuple[str, ...] = ()
    required_secrets: tuple[str, ...] = ()
    side_effects: tuple[str, ...] = ()
    agent_config: dict[str, Any] = Field(default_factory=dict)


class PackageSnapshot(FrozenModel):
    schema_version: str = PACKAGE_IR_SCHEMA_VERSION
    package_id: str
    root_name: str
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: tuple[PackageFile, ...]
    capabilities: CapabilityFacts


class IRNode(FrozenModel):
    node_id: str
    kind: NodeKind
    path: str
    locator: str
    label: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    span: SourceSpan | None = None
    mutable: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(FrozenModel):
    edge_id: str
    source: str
    target: str
    kind: EdgeKind
    layer: Literal["static", "planned", "observed"] = "static"
    evidence_tier: str | None = None
    evaluation_id: str | None = None
    task_id: str | None = None
    provider: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    count: int = Field(default=1, ge=1)
    trace_completeness: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PackageIR(FrozenModel):
    schema_version: str = PACKAGE_IR_SCHEMA_VERSION
    package_id: str
    snapshot_hash: str
    nodes: tuple[IRNode, ...]
    edges: tuple[GraphEdge, ...]


class Diagnostic(FrozenModel):
    diagnostic_id: str
    kind: str
    severity: Literal["info", "warning", "error"]
    message: str
    related_node_ids: tuple[str, ...] = ()
    path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class DiagnosticReport(FrozenModel):
    schema_version: str = PACKAGE_IR_SCHEMA_VERSION
    package_id: str
    diagnostics: tuple[Diagnostic, ...]


class PackageGraph(FrozenModel):
    schema_version: str = PACKAGE_IR_SCHEMA_VERSION
    package_id: str
    snapshot_hash: str
    nodes: tuple[IRNode, ...]
    edges: tuple[GraphEdge, ...]
    diagnostics: tuple[Diagnostic, ...] = ()

    @model_validator(mode="after")
    def graph_invariants(self) -> PackageGraph:
        node_ids = [node.node_id for node in self.nodes]
        edge_ids = [edge.edge_id for edge in self.edges]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("duplicate node_id")
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("duplicate edge_id")
        known = set(node_ids)
        dangling = [
            edge.edge_id
            for edge in self.edges
            if edge.source not in known or edge.target not in known
        ]
        if dangling:
            raise ValueError(f"dangling node references: {dangling[:3]}")
        return self


class FailureSliceNode(FrozenModel):
    node_id: str
    rank: int = Field(ge=1)
    distance: int = Field(ge=0)
    score: float = Field(ge=0)
    reason: str


class FailureSlice(FrozenModel):
    schema_version: str = PACKAGE_IR_SCHEMA_VERSION
    package_id: str
    seed_node_ids: tuple[str, ...]
    nodes: tuple[FailureSliceNode, ...]
    omitted_nodes: int = Field(ge=0)
    token_estimate: int = Field(ge=0)


class PackageGraphDiff(FrozenModel):
    schema_version: str = PACKAGE_IR_SCHEMA_VERSION
    before_snapshot: str
    after_snapshot: str
    added_nodes: tuple[str, ...]
    removed_nodes: tuple[str, ...]
    modified_nodes: tuple[str, ...]
    added_edges: tuple[str, ...]
    removed_edges: tuple[str, ...]
    affected_closure: tuple[str, ...]
    blast_radius: int = Field(ge=0)


def stable_id(prefix: str, *parts: object) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"{prefix}-{hashlib.sha256(canonical.encode()).hexdigest()[:24]}"


def content_hash(value: str | bytes) -> str:
    data = value if isinstance(value, bytes) else value.encode()
    return hashlib.sha256(data).hexdigest()


def make_node(
    package_id: str,
    kind: NodeKind,
    path: str,
    locator: str,
    label: str,
    content: str | bytes,
    **kwargs: Any,
) -> IRNode:
    return IRNode(
        node_id=stable_id("node", package_id, kind.value, path, locator),
        kind=kind,
        path=path,
        locator=locator,
        label=label,
        content_hash=content_hash(content),
        **kwargs,
    )


def make_edge(
    source: str,
    target: str,
    kind: EdgeKind,
    *,
    layer: Literal["static", "planned", "observed"] = "static",
    identity: object | None = None,
    **kwargs: Any,
) -> GraphEdge:
    return GraphEdge(
        edge_id=stable_id("edge", source, target, kind.value, layer, identity),
        source=source,
        target=target,
        kind=kind,
        layer=layer,
        **kwargs,
    )
