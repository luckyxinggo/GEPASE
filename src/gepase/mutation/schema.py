"""PackagePatch v1 schema and immutable patch/application identities."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from gepase.package.ir import PackageGraphDiff
from gepase.schemas.common import FrozenModel

PACKAGE_PATCH_SCHEMA_VERSION = "1.0.0"
ABSENT_PRECONDITION = "absent"


class PatchOperationKind(StrEnum):
    REPLACE_MARKDOWN_BLOCK = "replace_markdown_block"
    INSERT_REFERENCE = "insert_reference"
    UPDATE_FRONTMATTER = "update_frontmatter"
    REPLACE_PYTHON_FUNCTION = "replace_python_function"
    REPLACE_TEXT_FILE = "replace_text_file"
    ADD_FILE = "add_file"
    DELETE_FILE = "delete_file"


class RegressionRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def _safe_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("patch operation path must be package-relative")
    if path.parts[0] in {".git", ".venv", "artifacts", "tests"}:
        raise ValueError("patch operation targets a protected path")
    return path.as_posix()


class PatchOperationBase(FrozenModel):
    operation_id: str = Field(pattern=r"^op-[a-zA-Z0-9._-]+$")
    path: str
    precondition_hash: str = Field(pattern=r"^(?:[0-9a-f]{64}|absent)$")
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    expected_benefit: str = Field(min_length=1, max_length=600)
    regression_risk: RegressionRisk
    rationale: str = Field(min_length=1, max_length=1_200)

    @model_validator(mode="after")
    def safe_operation_path(self) -> PatchOperationBase:
        normalized = _safe_path(self.path)
        if normalized != self.path:
            raise ValueError("patch operation path must be normalized")
        return self


class ReplaceMarkdownBlock(PatchOperationBase):
    op: Literal[PatchOperationKind.REPLACE_MARKDOWN_BLOCK]
    target_node_id: str
    replacement: str = Field(min_length=1)


class InsertReference(PatchOperationBase):
    op: Literal[PatchOperationKind.INSERT_REFERENCE]
    precondition_hash: str = ABSENT_PRECONDITION
    target_node_id: None = None
    content: str = Field(min_length=1)
    referenced_from_node_id: str

    @model_validator(mode="after")
    def reference_path(self) -> InsertReference:
        if self.precondition_hash != ABSENT_PRECONDITION:
            raise ValueError("insert_reference requires an absent precondition")
        if Path(self.path).parts[0] != "references" or Path(self.path).suffix.lower() != ".md":
            raise ValueError("insert_reference must create a Markdown file under references/")
        return self


class UpdateFrontmatter(PatchOperationBase):
    op: Literal[PatchOperationKind.UPDATE_FRONTMATTER]
    target_node_id: str
    replacement: str = Field(min_length=1)


class ReplacePythonFunction(PatchOperationBase):
    op: Literal[PatchOperationKind.REPLACE_PYTHON_FUNCTION]
    target_node_id: str
    replacement: str = Field(min_length=1)

    @model_validator(mode="after")
    def python_path(self) -> ReplacePythonFunction:
        if Path(self.path).suffix.lower() != ".py":
            raise ValueError("replace_python_function requires a Python path")
        return self


class ReplaceTextFile(PatchOperationBase):
    op: Literal[PatchOperationKind.REPLACE_TEXT_FILE]
    target_node_id: str
    replacement: str = Field(min_length=1)

    @model_validator(mode="after")
    def auditable_text_path(self) -> ReplaceTextFile:
        if Path(self.path).suffix.lower() not in {
            ".md",
            ".py",
            ".sh",
            ".yaml",
            ".yml",
            ".json",
        }:
            raise ValueError("replace_text_file requires an auditable text path")
        return self


class AddFile(PatchOperationBase):
    op: Literal[PatchOperationKind.ADD_FILE]
    precondition_hash: str = ABSENT_PRECONDITION
    target_node_id: None = None
    content: str = Field(min_length=1)
    executable: bool = False

    @model_validator(mode="after")
    def editable_text_file(self) -> AddFile:
        if self.precondition_hash != ABSENT_PRECONDITION:
            raise ValueError("add_file requires an absent precondition")
        if Path(self.path).suffix.lower() not in {".md", ".py", ".sh", ".yaml", ".yml", ".json"}:
            raise ValueError("add_file only supports auditable text formats")
        return self


class DeleteFile(PatchOperationBase):
    op: Literal[PatchOperationKind.DELETE_FILE]
    target_node_id: str
    orphan_evidence_ref: str = Field(min_length=1)


PatchOperation = Annotated[
    ReplaceMarkdownBlock
    | InsertReference
    | UpdateFrontmatter
    | ReplacePythonFunction
    | ReplaceTextFile
    | AddFile
    | DeleteFile,
    Field(discriminator="op"),
]
PATCH_OPERATION_ADAPTER = TypeAdapter(PatchOperation)


class PatchEditBudget(FrozenModel):
    max_operations: int = Field(default=3, ge=1, le=20)
    max_changed_files: int = Field(default=3, ge=1, le=20)
    max_added_files: int = Field(default=1, ge=0, le=10)
    max_deleted_files: int = Field(default=1, ge=0, le=10)
    max_total_replacement_chars: int = Field(default=12_000, ge=1, le=200_000)
    allow_script_edits: bool = True
    allow_file_topology_edits: bool = True


def patch_id_for(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"patch-{hashlib.sha256(encoded.encode()).hexdigest()[:24]}"


class PackagePatch(FrozenModel):
    schema_version: Literal["1.0.0"] = PACKAGE_PATCH_SCHEMA_VERSION
    patch_id: str
    proposal_work_id: str
    base_candidate_id: str
    base_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    selector: str
    selected_node_ids: tuple[str, ...] = Field(min_length=1)
    operations: tuple[PatchOperation, ...] = Field(min_length=1)
    edit_budget: PatchEditBudget
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=1_500)

    @model_validator(mode="after")
    def patch_invariants(self) -> PackagePatch:
        operation_ids = [item.operation_id for item in self.operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("patch operation ids must be unique")
        if len(self.operations) > self.edit_budget.max_operations:
            raise ValueError("patch exceeds max_operations")
        paths = {item.path for item in self.operations}
        if len(paths) > self.edit_budget.max_changed_files:
            raise ValueError("patch exceeds max_changed_files")
        additions = sum(
            item.op in {PatchOperationKind.ADD_FILE, PatchOperationKind.INSERT_REFERENCE}
            for item in self.operations
        )
        deletions = sum(item.op is PatchOperationKind.DELETE_FILE for item in self.operations)
        if additions > self.edit_budget.max_added_files:
            raise ValueError("patch exceeds max_added_files")
        if deletions > self.edit_budget.max_deleted_files:
            raise ValueError("patch exceeds max_deleted_files")
        if (additions or deletions) and not self.edit_budget.allow_file_topology_edits:
            raise ValueError("file topology edits are disabled")
        replacement_chars = sum(
            len(getattr(item, "replacement", getattr(item, "content", "")))
            for item in self.operations
        )
        if replacement_chars > self.edit_budget.max_total_replacement_chars:
            raise ValueError("patch exceeds replacement character budget")
        script_edit = any(
            item.op
            in {
                PatchOperationKind.REPLACE_PYTHON_FUNCTION,
                PatchOperationKind.REPLACE_TEXT_FILE,
            }
            or Path(item.path).suffix.lower() in {".py", ".sh"}
            for item in self.operations
        )
        if script_edit and not self.edit_budget.allow_script_edits:
            raise ValueError("script edits are disabled")
        targeted = {
            str(item.target_node_id) for item in self.operations if item.target_node_id is not None
        }
        allowed = set(self.selected_node_ids)
        extra = targeted - allowed
        if extra:
            raise ValueError(f"operation targets nodes outside selected scope: {sorted(extra)}")
        expected = patch_id_for(self.identity_payload())
        if self.patch_id != expected:
            raise ValueError("patch_id does not match immutable patch payload")
        return self

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "proposal_work_id": self.proposal_work_id,
            "base_candidate_id": self.base_candidate_id,
            "base_snapshot_hash": self.base_snapshot_hash,
            "base_content_hash": self.base_content_hash,
            "selector": self.selector,
            "selected_node_ids": list(self.selected_node_ids),
            "operations": [item.model_dump(mode="json") for item in self.operations],
            "edit_budget": self.edit_budget.model_dump(mode="json"),
            "evidence_refs": list(self.evidence_refs),
            "summary": self.summary,
        }

    @property
    def fingerprint(self) -> str:
        signatures = sorted(
            (
                item.op.value,
                item.path,
                str(getattr(item, "target_node_id", "")),
                hashlib.sha256(
                    str(getattr(item, "replacement", getattr(item, "content", ""))).encode()
                ).hexdigest(),
            )
            for item in self.operations
        )
        payload = json.dumps(signatures, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def package_patch_from_proposal(values: dict[str, object]) -> PackagePatch:
    """Validate proposer JSON and assign its content-derived patch identity in Core."""

    raw = dict(values)
    raw.pop("patch_id", None)
    raw.setdefault("schema_version", PACKAGE_PATCH_SCHEMA_VERSION)
    operation_values = raw.get("operations")
    if not isinstance(operation_values, (list, tuple)):
        raise ValueError("proposal operations must be a list")
    operations = tuple(PATCH_OPERATION_ADAPTER.validate_python(item) for item in operation_values)
    budget_value = raw.get("edit_budget")
    budget = (
        budget_value
        if isinstance(budget_value, PatchEditBudget)
        else PatchEditBudget.model_validate(budget_value)
    )
    identity = {
        "schema_version": str(raw["schema_version"]),
        "proposal_work_id": str(raw["proposal_work_id"]),
        "base_candidate_id": str(raw["base_candidate_id"]),
        "base_snapshot_hash": str(raw["base_snapshot_hash"]),
        "base_content_hash": str(raw["base_content_hash"]),
        "selector": str(raw["selector"]),
        "selected_node_ids": list(raw["selected_node_ids"]),  # type: ignore[arg-type]
        "operations": [item.model_dump(mode="json") for item in operations],
        "edit_budget": budget.model_dump(mode="json"),
        "evidence_refs": list(raw["evidence_refs"]),  # type: ignore[arg-type]
        "summary": str(raw["summary"]),
    }
    return PackagePatch.model_validate(
        {
            **identity,
            "patch_id": patch_id_for(identity),
            "operations": operations,
            "edit_budget": budget,
        }
    )


class PatchApplicationStatus(StrEnum):
    APPLIED = "applied"
    INVALID = "invalid"
    STALE_PARENT = "stale_parent"
    ROLLED_BACK = "rolled_back"


class FileChange(FrozenModel):
    path: str
    change: Literal["added", "modified", "deleted"]
    before_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    after_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class RollbackRecord(FrozenModel):
    parent_candidate_id: str
    parent_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    restored_content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    verified: bool
    removed_workspace: str | None = None


class PatchApplication(FrozenModel):
    schema_version: str = PACKAGE_PATCH_SCHEMA_VERSION
    application_id: str
    patch_id: str
    parent_candidate_id: str
    parent_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: PatchApplicationStatus
    candidate_id: str | None = None
    candidate_content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    workspace_ref: str | None = None
    file_changes: tuple[FileChange, ...] = ()
    graph_diff: PackageGraphDiff | None = None
    post_apply_diagnostics: tuple[dict[str, object], ...] = ()
    affected_node_ids: tuple[str, ...] = ()
    validation_intensity: str | None = None
    rollback: RollbackRecord | None = None
    error_code: str | None = None
    error_detail: str | None = None
    original_workspace_hash_unchanged: bool = True


def application_id_for(patch_id: str, parent_candidate_id: str) -> str:
    digest = hashlib.sha256(f"{patch_id}:{parent_candidate_id}".encode()).hexdigest()
    return f"application-{digest[:24]}"
