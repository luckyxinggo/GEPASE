"""Deterministic recovery for Agent-native execution evidence.

This module extends the existing Eval path; it is not an Executor or a second
evaluator.  It inventories immutable Agent workspaces, selects only required
evidence, permits exact frozen-map metadata correction, and describes typed
terminal failure when evidence cannot be recovered without another Agent call.
"""

from __future__ import annotations

import json
import mimetypes
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from gepase.evals.evidence import ProviderFailureKind, TraceStep
from gepase.evals.redaction import sensitive_kinds
from gepase.evals.work_items import EvalWorkItem, PackageAccessEvent, WorkSubmission
from gepase.optimizer.session_runtime import HostAttemptAccounting
from gepase.schemas.common import ArtifactRef, FrozenModel
from gepase.store.artifacts import atomic_write, canonical_json_bytes, sha256_bytes


class RecoveryActionKind(StrEnum):
    DETERMINISTIC_SUBMISSION_PACKAGING = "deterministic_submission_packaging_correction"
    DETERMINISTIC_METADATA = "deterministic_metadata_correction"
    AGENT_REEXECUTION = "agent_reexecution"


class AgentAttemptKind(StrEnum):
    INITIAL_EXECUTION = "initial_execution"
    REEXECUTION = "reexecution"


class EvidenceRole(StrEnum):
    TASK_OUTPUT = "task_output"
    TRANSCRIPT = "transcript"
    PACKAGE_ACCESS = "package_access"
    OBSERVED_TRACE = "observed_trace"
    OPTIONAL_DIAGNOSTIC = "optional_diagnostic"


class EvidenceDisposition(StrEnum):
    INCLUDED_UNCHANGED = "included_unchanged"
    INCLUDED_CORRECTED_METADATA = "included_corrected_metadata"
    EXCLUDED_OPTIONAL = "excluded_optional"
    MISSING_REQUIRED = "missing_required"
    REJECTED_REQUIRED_SENSITIVE = "rejected_required_sensitive"
    REJECTED_INVALID_METADATA = "rejected_invalid_metadata"


class RecoveryDisposition(StrEnum):
    RECOVERABLE_WITHOUT_AGENT = "recoverable_without_agent"
    TERMINAL_FAILURE_REQUIRED = "terminal_failure_required"


class EvidenceManifestEntry(FrozenModel):
    source_ref: str
    staged_path: str
    role: EvidenceRole
    required: bool
    disposition: EvidenceDisposition
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size_bytes: int | None = Field(default=None, ge=0)
    media_type: str | None = None
    exclusion_reason: str | None = None
    sensitive_findings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def paths_are_portable(self) -> EvidenceManifestEntry:
        for value in (self.source_ref, self.staged_path):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("evidence manifest paths must be repository-relative")
        if self.required and self.disposition is EvidenceDisposition.EXCLUDED_OPTIONAL:
            raise ValueError("required evidence cannot be excluded as optional")
        return self


class PackageAccessMetadataCorrection(FrozenModel):
    sequence: int = Field(ge=0)
    path: str
    original_node_id: str | None = None
    corrected_node_id: str
    package_node_map_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: Literal["unique_exact_path_to_frozen_node_map"] = "unique_exact_path_to_frozen_node_map"

    @model_validator(mode="after")
    def preserve_portable_path(self) -> PackageAccessMetadataCorrection:
        path = Path(self.path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("corrected Package access path must remain repository-relative")
        return self


class RecoveryAttemptBinding(FrozenModel):
    """Bind one immutable submission/workspace to one observed Host attempt."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    run_id: str = Field(min_length=1)
    work_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    attempt_kind: AgentAttemptKind
    repair_attempt: bool
    source_submission_ref: str
    source_submission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_submission_id: str = Field(min_length=1)
    source_artifact_root_ref: str
    raw_workspace_ref: str
    raw_workspace_tree_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    host_task_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    selected_host_attempt_accounting_id: str = Field(min_length=1)
    host_attempt_accounting_ids: tuple[str, ...] = Field(min_length=1)
    host_attempt_accounting_hashes: dict[str, str] = Field(min_length=1)
    binding_valid: Literal[True] = True

    @model_validator(mode="after")
    def identities_are_exact(self) -> RecoveryAttemptBinding:
        for value in (
            self.source_submission_ref,
            self.source_artifact_root_ref,
            self.raw_workspace_ref,
        ):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("recovery attempt binding paths must be repository-relative")
        if self.source_artifact_root_ref != self.raw_workspace_ref:
            raise ValueError("source artifact_root must equal the selected raw workspace")
        expected_repair = self.attempt_kind is AgentAttemptKind.REEXECUTION
        if self.repair_attempt is not expected_repair:
            raise ValueError("attempt_kind does not match source repair_attempt")
        if len(self.host_attempt_accounting_ids) != len(
            set(self.host_attempt_accounting_ids)
        ):
            raise ValueError("HostAttemptAccounting IDs must be unique")
        if self.selected_host_attempt_accounting_id not in self.host_attempt_accounting_ids:
            raise ValueError("selected HostAttemptAccounting is outside the cited attempt set")
        if set(self.host_attempt_accounting_hashes) != set(self.host_attempt_accounting_ids):
            raise ValueError("HostAttemptAccounting hashes do not cover the cited attempt set")
        if any(
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
            for value in self.host_attempt_accounting_hashes.values()
        ):
            raise ValueError("HostAttemptAccounting hashes must be lowercase sha256 values")
        return self


class EvidenceStagingManifest(FrozenModel):
    schema_version: Literal["2.0.0"] = "2.0.0"
    manifest_id: str
    run_id: str = Field(min_length=1)
    work_id: str
    task_id: str = Field(min_length=1)
    attempt_binding: RecoveryAttemptBinding | None = None
    source_submission_ref: str | None = None
    source_submission_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    raw_workspace_ref: str
    raw_workspace_tree_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    package_node_map_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    entries: tuple[EvidenceManifestEntry, ...] = Field(min_length=1)
    metadata_corrections: tuple[PackageAccessMetadataCorrection, ...] = ()
    action_kinds: tuple[RecoveryActionKind, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def deterministic_actions_only(self) -> EvidenceStagingManifest:
        if RecoveryActionKind.AGENT_REEXECUTION in self.action_kinds:
            raise ValueError("evidence staging cannot authorize Agent reexecution")
        if len(self.action_kinds) != len(set(self.action_kinds)):
            raise ValueError("evidence staging action kinds must be unique")
        if self.attempt_binding is None:
            if self.source_submission_ref is not None or self.source_submission_sha256 is not None:
                raise ValueError("a source submission requires an exact attempt binding")
        elif (
            self.run_id != self.attempt_binding.run_id
            or self.work_id != self.attempt_binding.work_id
            or self.task_id != self.attempt_binding.task_id
            or self.source_submission_ref != self.attempt_binding.source_submission_ref
            or self.source_submission_sha256
            != self.attempt_binding.source_submission_sha256
            or self.raw_workspace_ref != self.attempt_binding.raw_workspace_ref
            or self.raw_workspace_tree_hash != self.attempt_binding.raw_workspace_tree_hash
        ):
            raise ValueError("manifest and attempt binding identities disagree")
        return self


class WorkRecoveryAudit(FrozenModel):
    schema_version: Literal["2.0.0"] = "2.0.0"
    audit_id: str
    run_id: str = Field(min_length=1)
    work_id: str
    task_id: str
    attempt_kind: AgentAttemptKind
    host_attempt_accounting_ids: tuple[str, ...]
    manifest: EvidenceStagingManifest
    disposition: RecoveryDisposition
    failure_kind_if_terminal: ProviderFailureKind | None = None
    reasons: tuple[str, ...]
    would_add_agent_calls: Literal[False] = False
    would_modify_raw_workspace: Literal[False] = False
    audited_at: datetime

    @model_validator(mode="after")
    def terminal_failure_is_typed(self) -> WorkRecoveryAudit:
        if (
            self.disposition is RecoveryDisposition.TERMINAL_FAILURE_REQUIRED
            and self.failure_kind_if_terminal is None
        ):
            raise ValueError("unrecoverable work must name a typed failure")
        if (
            self.disposition is RecoveryDisposition.RECOVERABLE_WITHOUT_AGENT
            and self.failure_kind_if_terminal is not None
        ):
            raise ValueError("recoverable work cannot also be a terminal failure")
        binding = self.manifest.attempt_binding
        if (
            self.run_id != self.manifest.run_id
            or self.work_id != self.manifest.work_id
            or self.task_id != self.manifest.task_id
        ):
            raise ValueError("audit and manifest identities disagree")
        if binding is None:
            if self.disposition is RecoveryDisposition.RECOVERABLE_WITHOUT_AGENT:
                raise ValueError("recoverable evidence requires an exact attempt binding")
        elif (
            self.attempt_kind is not binding.attempt_kind
            or self.host_attempt_accounting_ids != binding.host_attempt_accounting_ids
        ):
            raise ValueError("audit, manifest, and source attempt identities disagree")
        return self


class ReexecutionAuthorization(FrozenModel):
    """Explicit user checkpoint required beyond the frozen repair limit."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    authorization_id: str
    run_id: str
    work_id: str
    checkpoint_ref: str
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prior_reexecution_count: int = Field(ge=1)
    authorized_additional_reexecutions: int = Field(ge=1, le=1)
    authorized_by: Literal["user"] = "user"
    reason_zh: str = Field(min_length=1)
    authorized_at: datetime

    @model_validator(mode="after")
    def checkpoint_is_portable(self) -> ReexecutionAuthorization:
        path = Path(self.checkpoint_ref)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("reexecution checkpoint ref must be repository-relative")
        return self


class RepairExhaustionTerminalization(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    terminalization_id: str
    run_id: str
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    work_id: str
    failure_kind: ProviderFailureKind
    failure_detail: str = Field(min_length=1)
    source_submission_ref: str
    source_submission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    host_attempt_accounting_ids: tuple[str, ...] = Field(min_length=1)
    usage_already_accounted: Literal[True] = True
    requested_at: datetime

    @model_validator(mode="after")
    def is_terminal_ingest_failure(self) -> RepairExhaustionTerminalization:
        if self.failure_kind not in {
            ProviderFailureKind.INVALID_SUBMISSION,
            ProviderFailureKind.PARTIAL_ARTIFACT,
            ProviderFailureKind.INTERRUPTED,
        }:
            raise ValueError("repair exhaustion requires an ingest/evidence failure kind")
        path = Path(self.source_submission_ref)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("source submission ref must be repository-relative")
        if len(self.host_attempt_accounting_ids) != len(set(self.host_attempt_accounting_ids)):
            raise ValueError("host attempt accounting IDs must be unique")
        return self


class StagedEvidence(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    work_id: str
    staging_root_ref: str
    artifact_relative_paths: tuple[str, ...] = Field(min_length=1)
    transcript_relative_path: str
    package_access: tuple[PackageAccessEvent, ...]
    observed_trace: tuple[TraceStep, ...] = Field(min_length=1)
    manifest: EvidenceStagingManifest


def _project_ref(project_root: Path, path: Path) -> str:
    resolved_root = project_root.resolve()
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("recovery evidence escapes project root")
    return resolved.relative_to(resolved_root).as_posix()


def _tree_hash(root: Path) -> str:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        data = path.read_bytes()
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_bytes(data),
                "size_bytes": len(data),
            }
        )
    return sha256_bytes(canonical_json_bytes(rows))


def _json_list(path: Path, key: str) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    value = raw.get(key, []) if isinstance(raw, dict) else raw
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{path.name} must contain a {key} list")
    return value


def correct_package_access_metadata(
    item: EvalWorkItem,
    raw_events: list[dict[str, Any]],
) -> tuple[tuple[PackageAccessEvent, ...], tuple[PackageAccessMetadataCorrection, ...]]:
    """Correct node IDs only through an exact, unique frozen path mapping.

    Event paths and all non-node metadata remain byte-for-byte semantically
    identical.  Missing/unknown paths fail closed; no access event is invented.
    """

    mapping_hash = sha256_bytes(canonical_json_bytes(item.package_node_map))
    corrected: list[PackageAccessEvent] = []
    audits: list[PackageAccessMetadataCorrection] = []
    for raw in raw_events:
        event = PackageAccessEvent.model_validate(raw)
        matches = [node_id for path, node_id in item.package_node_map.items() if path == event.path]
        if len(matches) != 1:
            raise ValueError(
                f"Package access path has no unique exact frozen mapping: {event.path}"
            )
        expected = matches[0]
        if event.node_id != expected:
            audits.append(
                PackageAccessMetadataCorrection(
                    sequence=event.sequence,
                    path=event.path,
                    original_node_id=event.node_id,
                    corrected_node_id=expected,
                    package_node_map_hash=mapping_hash,
                )
            )
            event = event.model_copy(update={"node_id": expected})
        corrected.append(event)
    return tuple(corrected), tuple(audits)


def audit_recovery_attempt(
    project_root: Path,
    item: EvalWorkItem,
    *,
    run_id: str,
    run_root: Path,
    raw_workspace: Path,
    source_submission_path: Path | None,
    attempt_kind: AgentAttemptKind,
    host_attempt_accountings: tuple[HostAttemptAccounting, ...],
    audited_at: datetime | None = None,
) -> WorkRecoveryAudit:
    """Read an immutable workspace and produce a no-write recovery decision."""

    owner = run_root.resolve(strict=True)
    if not owner.is_dir() or not owner.is_relative_to(project_root.resolve()):
        raise ValueError("recovery run root must be a directory inside the project")
    workspace = raw_workspace.resolve(strict=True)
    if not workspace.is_dir() or not workspace.is_relative_to(owner):
        raise ValueError("raw Agent workspace must be a directory inside the bound run")
    workspace_ref = _project_ref(project_root, workspace)
    workspace_hash = _tree_hash(workspace)
    if not host_attempt_accountings or len(host_attempt_accountings) != len(
        {attempt.accounting_id for attempt in host_attempt_accountings}
    ):
        raise ValueError("recovery requires distinct HostAttemptAccounting records")
    host_attempt_accounting_ids = tuple(
        attempt.accounting_id for attempt in host_attempt_accountings
    )
    for attempt in host_attempt_accountings:
        if attempt.run_id != run_id or attempt.work_id != item.work_id:
            raise ValueError("HostAttemptAccounting belongs to another run/work")

    source_ref: str | None = None
    source_hash: str | None = None
    attempt_binding: RecoveryAttemptBinding | None = None
    if source_submission_path is not None:
        submission_path = source_submission_path.resolve(strict=True)
        if not submission_path.is_file() or not submission_path.is_relative_to(owner):
            raise ValueError("source submission must be a file inside the bound run")
        source_ref = _project_ref(project_root, submission_path)
        source_bytes = submission_path.read_bytes()
        source_hash = sha256_bytes(source_bytes)
        source = WorkSubmission.model_validate_json(source_bytes)
        if source.work_id != item.work_id:
            raise ValueError("source submission belongs to another work item")
        expected_repair = attempt_kind is AgentAttemptKind.REEXECUTION
        if source.repair_attempt is not expected_repair:
            raise ValueError("source repair_attempt disagrees with selected attempt kind")
        if source.artifact_root is None:
            raise ValueError("source submission has no artifact_root")
        artifact_root = (project_root / source.artifact_root).resolve(strict=True)
        if artifact_root != workspace:
            raise ValueError("source artifact_root does not equal the selected raw workspace")
        context_id = source.context_id or source.host_task_id
        matching_attempts = tuple(
            attempt
            for attempt in host_attempt_accountings
            if attempt.host_task_id == source.host_task_id
            and attempt.context_id == context_id
        )
        if len(matching_attempts) != 1:
            raise ValueError(
                "source host_task/context must match exactly one cited HostAttemptAccounting"
            )
        attempt_binding = RecoveryAttemptBinding(
            run_id=run_id,
            work_id=item.work_id,
            task_id=item.task_id,
            attempt_kind=attempt_kind,
            repair_attempt=source.repair_attempt,
            source_submission_ref=source_ref,
            source_submission_sha256=source_hash,
            source_submission_id=source.submission_id,
            source_artifact_root_ref=_project_ref(project_root, artifact_root),
            raw_workspace_ref=workspace_ref,
            raw_workspace_tree_hash=workspace_hash,
            host_task_id=source.host_task_id,
            context_id=context_id,
            selected_host_attempt_accounting_id=matching_attempts[0].accounting_id,
            host_attempt_accounting_ids=host_attempt_accounting_ids,
            host_attempt_accounting_hashes={
                attempt.accounting_id: sha256_bytes(
                    canonical_json_bytes(attempt.model_dump(mode="json"))
                )
                for attempt in host_attempt_accountings
            },
        )

    output_name = item.requested_output.get("filename")
    if not output_name:
        raise ValueError("work item does not freeze a requested output filename")
    required_roles: dict[str, EvidenceRole] = {
        output_name: EvidenceRole.TASK_OUTPUT,
        "transcript.md": EvidenceRole.TRANSCRIPT,
        "observed-trace.json": EvidenceRole.OBSERVED_TRACE,
    }
    if item.variant != "no-skill":
        required_roles["package-access.json"] = EvidenceRole.PACKAGE_ACCESS

    entries: list[EvidenceManifestEntry] = []
    reasons: list[str] = []
    metadata_corrections: tuple[PackageAccessMetadataCorrection, ...] = ()
    required_valid = True
    present = {
        path.relative_to(workspace).as_posix(): path
        for path in sorted(workspace.rglob("*"))
        if path.is_file()
    }
    for relative, role in required_roles.items():
        path = present.get(relative)
        if path is None:
            required_valid = False
            reasons.append(f"missing required evidence: {relative}")
            entries.append(
                EvidenceManifestEntry(
                    source_ref=f"{workspace_ref}/{relative}",
                    staged_path=relative,
                    role=role,
                    required=True,
                    disposition=EvidenceDisposition.MISSING_REQUIRED,
                    exclusion_reason="required evidence is absent from immutable workspace",
                )
            )
            continue
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            findings: tuple[str, ...] = ()
        else:
            findings = sensitive_kinds(text)
        disposition = EvidenceDisposition.INCLUDED_UNCHANGED
        exclusion_reason = None
        if findings:
            required_valid = False
            disposition = EvidenceDisposition.REJECTED_REQUIRED_SENSITIVE
            exclusion_reason = "required evidence contains prohibited sensitive content"
            reasons.append(f"sensitive required evidence: {relative}")
        if role is EvidenceRole.PACKAGE_ACCESS and not findings:
            try:
                raw_events = _json_list(path, "package_access")
                _events, metadata_corrections = correct_package_access_metadata(item, raw_events)
                if metadata_corrections:
                    disposition = EvidenceDisposition.INCLUDED_CORRECTED_METADATA
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                required_valid = False
                disposition = EvidenceDisposition.REJECTED_INVALID_METADATA
                exclusion_reason = str(error)
                reasons.append(f"invalid Package access metadata: {relative}")
        elif role is EvidenceRole.OBSERVED_TRACE and not findings:
            try:
                for value in _json_list(path, "observed_trace"):
                    TraceStep.model_validate(value)
            except (ValueError, TypeError, json.JSONDecodeError) as error:
                required_valid = False
                disposition = EvidenceDisposition.REJECTED_INVALID_METADATA
                exclusion_reason = str(error)
                reasons.append(f"invalid observed trace: {relative}")
        entries.append(
            EvidenceManifestEntry(
                source_ref=_project_ref(project_root, path),
                staged_path=relative,
                role=role,
                required=True,
                disposition=disposition,
                sha256=sha256_bytes(data),
                size_bytes=len(data),
                media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                exclusion_reason=exclusion_reason,
                sensitive_findings=findings,
            )
        )

    for relative, path in present.items():
        if relative in required_roles:
            continue
        data = path.read_bytes()
        try:
            findings = sensitive_kinds(data.decode("utf-8"))
        except UnicodeDecodeError:
            findings = ()
        entries.append(
            EvidenceManifestEntry(
                source_ref=_project_ref(project_root, path),
                staged_path=relative,
                role=EvidenceRole.OPTIONAL_DIAGNOSTIC,
                required=False,
                disposition=EvidenceDisposition.EXCLUDED_OPTIONAL,
                sha256=sha256_bytes(data),
                size_bytes=len(data),
                media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                exclusion_reason=(
                    "optional diagnostic is retained in the immutable raw workspace; "
                    "it is outside the required submission evidence set"
                ),
                sensitive_findings=findings,
            )
        )

    action_kinds = [RecoveryActionKind.DETERMINISTIC_SUBMISSION_PACKAGING]
    if metadata_corrections:
        action_kinds.append(RecoveryActionKind.DETERMINISTIC_METADATA)
    manifest_payload = {
        "run_id": run_id,
        "work_id": item.work_id,
        "task_id": item.task_id,
        "attempt_binding": (
            attempt_binding.model_dump(mode="json") if attempt_binding is not None else None
        ),
        "source_submission_ref": source_ref,
        "source_submission_sha256": source_hash,
        "raw_workspace_ref": workspace_ref,
        "raw_workspace_tree_hash": workspace_hash,
        "package_node_map_hash": sha256_bytes(canonical_json_bytes(item.package_node_map)),
        "entries": [entry.model_dump(mode="json") for entry in entries],
        "metadata_corrections": [
            correction.model_dump(mode="json") for correction in metadata_corrections
        ],
        "action_kinds": action_kinds,
    }
    manifest_id = f"evidence-manifest-{sha256_bytes(canonical_json_bytes(manifest_payload))[:24]}"
    manifest = EvidenceStagingManifest(manifest_id=manifest_id, **manifest_payload)
    disposition = (
        RecoveryDisposition.RECOVERABLE_WITHOUT_AGENT
        if required_valid and source_ref is not None
        else RecoveryDisposition.TERMINAL_FAILURE_REQUIRED
    )
    if source_ref is None:
        reasons.append("no complete typed source submission exists for this attempt")
    if not reasons and metadata_corrections:
        reasons.append("required bytes are complete; node_id has an exact frozen-map correction")
    if not reasons:
        reasons.append("required evidence is complete and can be staged unchanged")
    failure_kind = None
    if disposition is RecoveryDisposition.TERMINAL_FAILURE_REQUIRED:
        failure_kind = (
            ProviderFailureKind.PARTIAL_ARTIFACT
            if any("missing required" in reason or "no complete" in reason for reason in reasons)
            else ProviderFailureKind.INVALID_SUBMISSION
        )
    audit_payload = {
        "run_id": run_id,
        "work_id": item.work_id,
        "task_id": item.task_id,
        "attempt_kind": attempt_kind,
        "host_attempt_accounting_ids": host_attempt_accounting_ids,
        "manifest": manifest.model_dump(mode="json"),
        "disposition": disposition,
        "failure_kind_if_terminal": failure_kind,
        "reasons": tuple(reasons),
        "audited_at": audited_at or datetime.now(UTC),
    }
    identity = {**audit_payload, "audited_at": None}
    audit_id = f"recovery-audit-{sha256_bytes(canonical_json_bytes(identity))[:24]}"
    return WorkRecoveryAudit(audit_id=audit_id, **audit_payload)


def validate_recovery_attempt_binding(
    project_root: Path,
    audit: WorkRecoveryAudit,
    *,
    expected_run_id: str,
    expected_task_id: str,
    host_attempt_accountings: tuple[HostAttemptAccounting, ...],
) -> RecoveryAttemptBinding:
    """Revalidate the complete attempt binding immediately before staging/ingest."""

    binding = audit.manifest.attempt_binding
    if binding is None or not binding.binding_valid:
        raise ValueError("recovery audit has no complete attempt binding")
    if (
        audit.run_id != expected_run_id
        or binding.run_id != expected_run_id
        or audit.task_id != expected_task_id
        or binding.task_id != expected_task_id
        or audit.work_id != binding.work_id
    ):
        raise ValueError("recovery audit belongs to another run/work/task")
    if tuple(item.accounting_id for item in host_attempt_accountings) != (
        binding.host_attempt_accounting_ids
    ):
        raise ValueError("HostAttemptAccounting order/set differs from the recovery audit")
    by_id = {item.accounting_id: item for item in host_attempt_accountings}
    for accounting_id, expected_hash in binding.host_attempt_accounting_hashes.items():
        attempt = by_id[accounting_id]
        if attempt.run_id != binding.run_id or attempt.work_id != binding.work_id:
            raise ValueError("HostAttemptAccounting belongs to another run/work")
        actual_hash = sha256_bytes(canonical_json_bytes(attempt.model_dump(mode="json")))
        if actual_hash != expected_hash:
            raise ValueError("HostAttemptAccounting changed after recovery audit")
    selected = by_id[binding.selected_host_attempt_accounting_id]
    if (
        selected.host_task_id != binding.host_task_id
        or selected.context_id != binding.context_id
    ):
        raise ValueError("selected HostAttemptAccounting host/context binding changed")
    workspace = (project_root / binding.raw_workspace_ref).resolve(strict=True)
    if not workspace.is_dir() or _tree_hash(workspace) != binding.raw_workspace_tree_hash:
        raise ValueError("immutable raw workspace changed after recovery audit")
    source_path = (project_root / binding.source_submission_ref).resolve(strict=True)
    source_bytes = source_path.read_bytes()
    if sha256_bytes(source_bytes) != binding.source_submission_sha256:
        raise ValueError("source submission changed after recovery audit")
    source = WorkSubmission.model_validate_json(source_bytes)
    if (
        source.submission_id != binding.source_submission_id
        or source.work_id != binding.work_id
        or source.host_task_id != binding.host_task_id
        or (source.context_id or source.host_task_id) != binding.context_id
        or source.repair_attempt != binding.repair_attempt
        or source.artifact_root != binding.source_artifact_root_ref
    ):
        raise ValueError("source submission no longer matches the recovery attempt binding")
    return binding


def stage_recovery_evidence(
    project_root: Path,
    audit: WorkRecoveryAudit,
    destination: Path,
) -> StagedEvidence:
    """Materialize a new required-only bundle without changing raw evidence."""

    if audit.disposition is not RecoveryDisposition.RECOVERABLE_WITHOUT_AGENT:
        raise ValueError("unrecoverable evidence cannot be staged")
    binding = audit.manifest.attempt_binding
    if binding is None or not binding.binding_valid:
        raise ValueError("recovery evidence has no valid attempt/workspace/Host binding")
    raw_workspace = (project_root / binding.raw_workspace_ref).resolve(strict=True)
    if _tree_hash(raw_workspace) != binding.raw_workspace_tree_hash:
        raise ValueError("immutable raw workspace changed after recovery audit")
    root = destination.resolve()
    if root.exists():
        raise FileExistsError("recovery staging destination already exists")
    if not root.parent.resolve().is_relative_to(project_root.resolve()):
        raise ValueError("recovery staging destination must remain inside the project")
    root.mkdir(parents=True)
    corrected_by_sequence = {item.sequence: item for item in audit.manifest.metadata_corrections}
    package_access: tuple[PackageAccessEvent, ...] = ()
    observed_trace: tuple[TraceStep, ...] = ()
    artifact_paths: list[str] = []
    for entry in audit.manifest.entries:
        if not entry.required:
            continue
        if entry.disposition not in {
            EvidenceDisposition.INCLUDED_UNCHANGED,
            EvidenceDisposition.INCLUDED_CORRECTED_METADATA,
        }:
            raise ValueError("required evidence manifest is not stageable")
        source = (project_root / entry.source_ref).resolve(strict=True)
        data = source.read_bytes()
        if sha256_bytes(data) != entry.sha256 or len(data) != entry.size_bytes:
            raise ValueError("immutable raw evidence changed after audit")
        output = root / entry.staged_path
        if entry.role is EvidenceRole.PACKAGE_ACCESS:
            raw_events = _json_list(source, "package_access")
            values: list[PackageAccessEvent] = []
            for raw in raw_events:
                event = PackageAccessEvent.model_validate(raw)
                correction = corrected_by_sequence.get(event.sequence)
                if correction is not None:
                    if (
                        correction.path != event.path
                        or correction.original_node_id != event.node_id
                    ):
                        raise ValueError("Package access correction no longer matches raw event")
                    event = event.model_copy(update={"node_id": correction.corrected_node_id})
                values.append(event)
            package_access = tuple(values)
            data = canonical_json_bytes(
                {"package_access": [event.model_dump(mode="json") for event in values]}
            )
        elif entry.role is EvidenceRole.OBSERVED_TRACE:
            observed_trace = tuple(
                TraceStep.model_validate(value) for value in _json_list(source, "observed_trace")
            )
        atomic_write(output, data)
        artifact_paths.append(entry.staged_path)
    if not observed_trace:
        raise ValueError("staged evidence has no observed trace")
    staging_ref = _project_ref(project_root, root)
    return StagedEvidence(
        work_id=audit.work_id,
        staging_root_ref=staging_ref,
        artifact_relative_paths=tuple(sorted(artifact_paths)),
        transcript_relative_path="transcript.md",
        package_access=package_access,
        observed_trace=observed_trace,
        manifest=audit.manifest,
    )


def build_recovered_submission(
    project_root: Path,
    audit: WorkRecoveryAudit,
    staged: StagedEvidence,
) -> WorkSubmission:
    """Bind staged evidence to the original Agent provenance and usage.

    This creates a new deterministic submission identity but does not represent
    another execution.  Runtime settlement must therefore use the audit's
    preaccounted HostAttempt IDs.
    """

    if audit.disposition is not RecoveryDisposition.RECOVERABLE_WITHOUT_AGENT:
        raise ValueError("unrecoverable evidence cannot become a submission")
    binding = audit.manifest.attempt_binding
    if binding is None or not binding.binding_valid:
        raise ValueError("recovered submission requires a valid attempt binding")
    if staged.work_id != audit.work_id or staged.manifest != audit.manifest:
        raise ValueError("staged evidence does not match the recovery audit")
    source_ref = audit.manifest.source_submission_ref
    source_hash = audit.manifest.source_submission_sha256
    if source_ref is None or source_hash is None:
        raise ValueError("recovered submission requires a complete source submission")
    source_path = (project_root / source_ref).resolve(strict=True)
    source_bytes = source_path.read_bytes()
    if sha256_bytes(source_bytes) != source_hash:
        raise ValueError("source submission changed after recovery audit")
    source = WorkSubmission.model_validate_json(source_bytes)
    if (
        source.submission_id != binding.source_submission_id
        or source.work_id != binding.work_id
        or source.host_task_id != binding.host_task_id
        or (source.context_id or source.host_task_id) != binding.context_id
        or source.repair_attempt != binding.repair_attempt
        or source.artifact_root != binding.raw_workspace_ref
    ):
        raise ValueError("source submission no longer matches the recovery attempt binding")
    staging_root = (project_root / staged.staging_root_ref).resolve(strict=True)
    references: list[ArtifactRef] = []
    for relative in staged.artifact_relative_paths:
        path = (staging_root / relative).resolve(strict=True)
        if not path.is_relative_to(staging_root) or not path.is_file():
            raise ValueError("staged evidence path escapes or is missing")
        data = path.read_bytes()
        references.append(
            ArtifactRef(
                path=relative,
                sha256=sha256_bytes(data),
                media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                size_bytes=len(data),
            )
        )
    transcript = next(
        (
            reference
            for reference in references
            if reference.path == staged.transcript_relative_path
        ),
        None,
    )
    if transcript is None:
        raise ValueError("staged transcript is not in the explicit artifact set")
    identity = {
        "source_submission_id": source.submission_id,
        "evidence_manifest_id": audit.manifest.manifest_id,
        "artifacts": [reference.model_dump(mode="json") for reference in references],
        "package_access": [event.model_dump(mode="json") for event in staged.package_access],
        "observed_trace": [event.model_dump(mode="json") for event in staged.observed_trace],
    }
    return source.model_copy(
        update={
            "submission_id": (f"submission-{sha256_bytes(canonical_json_bytes(identity))[:24]}"),
            "artifact_root": staged.staging_root_ref,
            "artifacts": tuple(references),
            "transcript": transcript,
            "package_access": staged.package_access,
            "observed_trace": staged.observed_trace,
            "failure_kind": None,
            "failure_detail": None,
        }
    )


def validate_agent_reexecution(
    project_root: Path,
    *,
    run_id: str,
    work_id: str,
    prior_reexecution_count: int,
    frozen_max_reexecutions: int,
    authorization: ReexecutionAuthorization | None = None,
) -> RecoveryActionKind:
    """Keep the frozen single reexecution default and require a new user checkpoint."""

    if prior_reexecution_count < 0 or frozen_max_reexecutions < 0:
        raise ValueError("reexecution counts cannot be negative")
    if prior_reexecution_count < frozen_max_reexecutions:
        return RecoveryActionKind.AGENT_REEXECUTION
    if authorization is None:
        raise ValueError("additional Agent reexecution requires a new user checkpoint")
    if (
        authorization.run_id != run_id
        or authorization.work_id != work_id
        or authorization.prior_reexecution_count != prior_reexecution_count
    ):
        raise ValueError("reexecution authorization is bound to another run/work/count")
    checkpoint = (project_root / authorization.checkpoint_ref).resolve(strict=True)
    if not checkpoint.is_relative_to(project_root.resolve()):
        raise ValueError("reexecution checkpoint escapes project root")
    if sha256_bytes(checkpoint.read_bytes()) != authorization.checkpoint_sha256:
        raise ValueError("reexecution checkpoint hash mismatch")
    return RecoveryActionKind.AGENT_REEXECUTION


def build_repair_exhaustion_terminalization(
    project_root: Path,
    *,
    run_id: str,
    config_hash: str,
    work_id: str,
    failure_kind: ProviderFailureKind,
    failure_detail: str,
    source_submission_path: Path,
    host_attempt_accounting_ids: tuple[str, ...],
    requested_at: datetime | None = None,
) -> RepairExhaustionTerminalization:
    source_ref = _project_ref(project_root, source_submission_path)
    payload = {
        "run_id": run_id,
        "config_hash": config_hash,
        "work_id": work_id,
        "failure_kind": failure_kind,
        "failure_detail": failure_detail,
        "source_submission_ref": source_ref,
        "source_submission_sha256": sha256_bytes(source_submission_path.read_bytes()),
        "host_attempt_accounting_ids": host_attempt_accounting_ids,
        "requested_at": requested_at or datetime.now(UTC),
    }
    identity = {**payload, "requested_at": None}
    terminalization_id = f"terminalization-{sha256_bytes(canonical_json_bytes(identity))[:24]}"
    return RepairExhaustionTerminalization(
        terminalization_id=terminalization_id,
        **payload,
    )
