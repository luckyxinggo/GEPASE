"""Agent-native structured PackagePatch work queue and bounded ingestion."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from gepase.mutation.schema import (
    PACKAGE_PATCH_SCHEMA_VERSION,
    PackagePatch,
    PatchEditBudget,
    PatchOperationKind,
    package_patch_from_proposal,
)
from gepase.mutation.target_set import TargetSet
from gepase.optimizer.selectors import RankedSelection, SelectorRankingAudit
from gepase.package.dynamic_graph import SelectorGraphBinding
from gepase.schemas.common import FrozenModel
from gepase.store.artifacts import atomic_write, canonical_json_bytes
from gepase.store.rejected import RejectedEditStore


class ProposalWorkStatus(StrEnum):
    PENDING = "pending"
    EXPORTED = "exported"
    COMPLETED = "completed"
    FAILED = "failed"


class PatchTargetSnapshot(FrozenModel):
    node_id: str
    node_kind: str
    path: str
    locator: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content: str
    selection: RankedSelection


class PatchProposalWorkItem(FrozenModel):
    schema_version: str = PACKAGE_PATCH_SCHEMA_VERSION
    work_type: str = "package_patch_proposal"
    work_id: str
    run_id: str
    task_id: str
    parent_candidate_id: str
    parent_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    selector: str
    selector_graph: SelectorGraphBinding | None = None
    selector_ranking: SelectorRankingAudit | None = None
    targets: tuple[PatchTargetSnapshot, ...] = Field(min_length=1)
    target_set: TargetSet | None = None
    allowed_operations: tuple[PatchOperationKind, ...] = Field(min_length=1)
    edit_budget: PatchEditBudget
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    actionable_side_information: dict[str, Any]
    rejected_history: tuple[dict[str, Any], ...] = ()
    output_instructions: str
    repair_attempt: int = Field(default=0, ge=0, le=2)

    @model_validator(mode="after")
    def bounded_work(self) -> PatchProposalWorkItem:
        if (self.selector_graph is None) != (self.selector_ranking is None):
            raise ValueError("selector graph and ranking provenance must appear together")
        if self.selector_graph is not None:
            if self.selector_graph.parent_candidate_id != self.parent_candidate_id:
                raise ValueError("selector graph is bound to another parent")
            if self.selector_graph.parent_snapshot_hash != self.parent_snapshot_hash:
                raise ValueError("selector graph snapshot differs from proposal parent")
            if self.selector_graph.parent_content_hash != self.parent_content_hash:
                raise ValueError("selector graph content differs from proposal parent")
        if len(self.targets) > 2:
            raise ValueError("proposal work exceeds GH-P0 two-target limit")
        if len(self.targets) > self.edit_budget.max_operations:
            raise ValueError("proposal work exposes more targets than operation budget")
        if self.edit_budget.max_operations > 2 or self.edit_budget.max_changed_files > 2:
            raise ValueError("proposal work exceeds GH-P0 2 operation/file limits")
        target_ids = tuple(item.node_id for item in self.targets)
        if (
            self.selector_ranking is not None
            and self.selector_ranking.selected_node_ids != target_ids
        ):
            raise ValueError("selector ranking selected targets differ from proposal targets")
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("proposal work targets must be unique")
        if len(self.targets) == 2:
            if self.target_set is None:
                raise ValueError("two-target proposal work requires a typed TargetSet")
            if self.target_set.parent_candidate_id != self.parent_candidate_id:
                raise ValueError("TargetSet parent differs from proposal parent")
            if set(self.target_set.target_node_ids) != set(target_ids):
                raise ValueError("TargetSet does not match exported targets")
            if not set(self.target_set.evidence_refs) <= set(self.evidence_refs):
                raise ValueError("TargetSet evidence is outside proposal evidence")
            if (
                self.edit_budget.allow_file_topology_edits
                or self.edit_budget.max_added_files
                or self.edit_budget.max_deleted_files
            ):
                raise ValueError("GH-P0 TargetSet work may not change file topology")
        elif self.target_set is not None:
            raise ValueError("single-target proposal work must not manufacture a TargetSet")
        if not self.actionable_side_information:
            raise ValueError("proposal work requires actionable side information")
        return self


class ProposalProvenance(FrozenModel):
    host: str = Field(min_length=1)
    model: str = Field(min_length=1)
    host_task_id: str = Field(min_length=1)
    duration_ms: int = Field(ge=0)
    token_estimate: int = Field(ge=0)


class PatchProposalSubmission(FrozenModel):
    schema_version: str = PACKAGE_PATCH_SCHEMA_VERSION
    submission_id: str
    work_id: str
    status: ProposalWorkStatus
    patch: PackagePatch | None = None
    provenance: ProposalProvenance
    valid_on_first_attempt: bool
    repair_count: int = Field(default=0, ge=0, le=2)
    failure_kind: str | None = None
    failure_detail: str | None = None

    @model_validator(mode="after")
    def submission_outcome(self) -> PatchProposalSubmission:
        if self.status is ProposalWorkStatus.COMPLETED and self.patch is None:
            raise ValueError("completed proposal submission requires a patch")
        if self.status is ProposalWorkStatus.FAILED and not self.failure_kind:
            raise ValueError("failed proposal submission requires failure_kind")
        return self


def proposal_accounting_increments(work: PatchProposalWorkItem) -> tuple[int, int]:
    """Return (new mutation intents, Agent repairs) for one proposal work item."""

    return (0, 1) if work.repair_attempt > 0 else (1, 0)


def _submission_id(work_id: str, patch: PackagePatch | None, task_id: str) -> str:
    payload = f"{work_id}:{patch.patch_id if patch else 'failed'}:{task_id}"
    return f"proposal-submission-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def build_patch_submission(
    work: PatchProposalWorkItem,
    proposal: dict[str, object],
    *,
    host: str,
    model: str,
    host_task_id: str,
    duration_ms: int,
    token_estimate: int,
    valid_on_first_attempt: bool = True,
    repair_count: int = 0,
) -> PatchProposalSubmission:
    operations = proposal.get("operations")
    summary = proposal.get("summary")
    raw: dict[str, object] = {
        "schema_version": PACKAGE_PATCH_SCHEMA_VERSION,
        "proposal_work_id": work.work_id,
        "base_candidate_id": work.parent_candidate_id,
        "base_snapshot_hash": work.parent_snapshot_hash,
        "base_content_hash": work.parent_content_hash,
        "selector": work.selector,
        "selected_node_ids": [item.node_id for item in work.targets],
        "operations": operations,
        "edit_budget": work.edit_budget,
        "evidence_refs": list(work.evidence_refs),
        "summary": summary,
    }
    patch = package_patch_from_proposal(raw)
    allowed = set(work.allowed_operations)
    disallowed = {item.op for item in patch.operations} - allowed
    if disallowed:
        raise ValueError(f"proposal used disallowed operations: {sorted(disallowed)}")
    if work.actionable_side_information.get("causal_contract", {}).get("required") is True:
        from gepase.mutation.causal import bind_causal_operations

        bind_causal_operations(work, patch)
    provenance = ProposalProvenance(
        host=host,
        model=model,
        host_task_id=host_task_id,
        duration_ms=duration_ms,
        token_estimate=token_estimate,
    )
    return PatchProposalSubmission(
        submission_id=_submission_id(work.work_id, patch, host_task_id),
        work_id=work.work_id,
        status=ProposalWorkStatus.COMPLETED,
        patch=patch,
        provenance=provenance,
        valid_on_first_attempt=valid_on_first_attempt,
        repair_count=repair_count,
    )


def build_failed_patch_submission(
    work: PatchProposalWorkItem,
    *,
    host: str,
    model: str,
    host_task_id: str,
    duration_ms: int,
    token_estimate: int,
    failure_kind: str,
    failure_detail: str,
) -> PatchProposalSubmission:
    provenance = ProposalProvenance(
        host=host,
        model=model,
        host_task_id=host_task_id,
        duration_ms=duration_ms,
        token_estimate=token_estimate,
    )
    return PatchProposalSubmission(
        submission_id=_submission_id(work.work_id, None, host_task_id),
        work_id=work.work_id,
        status=ProposalWorkStatus.FAILED,
        provenance=provenance,
        valid_on_first_attempt=False,
        failure_kind=failure_kind,
        failure_detail=failure_detail,
    )


def prepare_proposal_workspace(
    work: PatchProposalWorkItem,
    output_dir: Path,
) -> dict[str, object]:
    """Materialize only exported target content for an isolated proposal worker."""

    output = output_dir.resolve()
    replacements: list[dict[str, str]] = []
    for index, target in enumerate(work.targets, 1):
        base = output / "replacement" if len(work.targets) == 1 else output / f"target-{index}"
        replacement = base / target.path
        atomic_write(replacement, target.content.encode())
        replacements.append({"node_id": target.node_id, "path": replacement.as_posix()})
    context = work.model_dump(mode="json")
    for target in context["targets"]:
        target["content"] = "<materialized-in-target-directory>"
    atomic_write(output / "work-context.json", canonical_json_bytes(context))
    return {
        "schema_version": PACKAGE_PATCH_SCHEMA_VERSION,
        "work_id": work.work_id,
        "replacement": replacements[0]["path"],
        "replacements": replacements,
        "context": (output / "work-context.json").as_posix(),
        "assertions_included": False,
        "sibling_outputs_included": False,
    }


def draft_replacement_proposal(
    work: PatchProposalWorkItem,
    replacement_path: Path,
    *,
    summary: str,
) -> dict[str, object]:
    """Build proposer JSON around worker-edited content without exposing Core state."""

    if len(work.targets) != 1 or len(work.allowed_operations) != 1:
        raise ValueError("replacement draft requires one target and one allowed operation")
    target = work.targets[0]
    operation = work.allowed_operations[0]
    if operation not in {
        PatchOperationKind.REPLACE_MARKDOWN_BLOCK,
        PatchOperationKind.UPDATE_FRONTMATTER,
        PatchOperationKind.REPLACE_PYTHON_FUNCTION,
        PatchOperationKind.REPLACE_TEXT_FILE,
    }:
        raise ValueError("replacement draft only supports bounded replacement operations")
    replacement = replacement_path.read_text(encoding="utf-8")
    if not replacement.strip() or replacement == target.content:
        raise ValueError("proposal replacement must be non-empty and non-noop")
    return {
        "operations": [
            {
                "operation_id": f"op-{work.work_id}",
                "op": operation.value,
                "target_node_id": target.node_id,
                "path": target.path,
                "precondition_hash": target.content_hash,
                "replacement": replacement,
                "evidence_refs": list(work.evidence_refs),
                "expected_benefit": (
                    "Repair the exported evidence-grounded package contract while "
                    "preserving unrelated Skill behavior."
                ),
                "regression_risk": "medium",
                "rationale": (
                    "The edit is bounded to the causal target and addresses the typed "
                    "structural failure supplied in actionable side information."
                ),
            }
        ],
        "summary": summary,
    }


def draft_bounded_replacement_proposal(
    work: PatchProposalWorkItem,
    replacement_paths: dict[str, Path],
    *,
    summary: str,
) -> dict[str, object]:
    """Build a one/two-target replacement proposal from isolated files."""

    if set(replacement_paths) != {target.node_id for target in work.targets}:
        raise ValueError("replacement paths must exactly match exported targets")
    if len(work.allowed_operations) not in {1, len(work.targets)}:
        raise ValueError("allowed operations must be shared or one-per-target")
    operations: list[dict[str, object]] = []
    for index, target in enumerate(work.targets):
        operation = (
            work.allowed_operations[0]
            if len(work.allowed_operations) == 1
            else work.allowed_operations[index]
        )
        if operation not in {
            PatchOperationKind.REPLACE_MARKDOWN_BLOCK,
            PatchOperationKind.UPDATE_FRONTMATTER,
            PatchOperationKind.REPLACE_PYTHON_FUNCTION,
            PatchOperationKind.REPLACE_TEXT_FILE,
        }:
            raise ValueError("bounded draft only supports replacement operations")
        replacement = replacement_paths[target.node_id].read_text(encoding="utf-8")
        if not replacement.strip() or replacement == target.content:
            raise ValueError(f"replacement for {target.node_id} is empty or a no-op")
        operations.append(
            {
                "operation_id": f"op-{work.work_id}-{index + 1}",
                "op": operation.value,
                "target_node_id": target.node_id,
                "path": target.path,
                "precondition_hash": target.content_hash,
                "replacement": replacement,
                "evidence_refs": list(work.evidence_refs),
                "expected_benefit": "Apply the bounded evidence-grounded repair atomically.",
                "regression_risk": "medium",
                "rationale": "The target belongs to the exported same-parent causal scope.",
            }
        )
    return {"operations": operations, "summary": summary}


def inject_rejected_history(
    work: PatchProposalWorkItem,
    store: RejectedEditStore,
    *,
    limit: int = 8,
) -> PatchProposalWorkItem:
    history = store.relevant(tuple(item.node_id for item in work.targets), limit=limit)
    return work.model_copy(
        update={
            "rejected_history": tuple(
                {
                    "record_id": item.record_id,
                    "patch_fingerprint": item.patch_fingerprint,
                    "operation_signatures": list(item.operation_signatures),
                    "failed_gate": item.failed_gate,
                    "score_delta": item.score_delta,
                    "reason_codes": list(item.reason_codes),
                    "evidence_refs": list(item.evidence_refs),
                }
                for item in history
            )
        }
    )


class PatchProposalStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS work (
                work_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                submission_id TEXT,
                submission_payload TEXT
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.commit()
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        self.connection.close()

    def __enter__(self) -> PatchProposalStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _json(value: object) -> str:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")  # type: ignore[union-attr]
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _event(self, kind: str, entity: str, payload: object = None) -> None:
        self.connection.execute(
            "INSERT INTO events(event_type, entity_id, payload) VALUES (?, ?, ?)",
            (kind, entity, self._json(payload or {})),
        )

    def add_work(self, work: PatchProposalWorkItem) -> bool:
        row = self.connection.execute(
            "SELECT payload FROM work WHERE work_id = ?", (work.work_id,)
        ).fetchone()
        if row:
            if PatchProposalWorkItem.model_validate_json(row["payload"]) != work:
                raise ValueError("proposal work_id reused with different payload")
            return False
        with self.connection:
            self.connection.execute(
                "INSERT INTO work(work_id, task_id, status, payload) VALUES (?, ?, ?, ?)",
                (
                    work.work_id,
                    work.task_id,
                    ProposalWorkStatus.PENDING.value,
                    work.model_dump_json(),
                ),
            )
            self._event("proposal_planned", work.work_id)
        return True

    def get_work(self, work_id: str) -> PatchProposalWorkItem:
        row = self.connection.execute(
            "SELECT payload FROM work WHERE work_id = ?", (work_id,)
        ).fetchone()
        if not row:
            raise KeyError(work_id)
        return PatchProposalWorkItem.model_validate_json(row["payload"])

    def next_work(self) -> PatchProposalWorkItem | None:
        row = self.connection.execute(
            "SELECT payload FROM work WHERE status = ? ORDER BY rowid LIMIT 1",
            (ProposalWorkStatus.PENDING.value,),
        ).fetchone()
        if not row:
            return None
        work = PatchProposalWorkItem.model_validate_json(row["payload"])
        with self.connection:
            self.connection.execute(
                "UPDATE work SET status = ? WHERE work_id = ?",
                (ProposalWorkStatus.EXPORTED.value, work.work_id),
            )
            self._event("proposal_exported", work.work_id)
        return work

    def pending_work(self) -> PatchProposalWorkItem | None:
        """Inspect the next exportable work item without changing its status."""

        row = self.connection.execute(
            "SELECT payload FROM work WHERE status = ? ORDER BY rowid LIMIT 1",
            (ProposalWorkStatus.PENDING.value,),
        ).fetchone()
        return PatchProposalWorkItem.model_validate_json(row["payload"]) if row else None

    def plan_repair(
        self,
        failed_work_id: str,
        *,
        max_repair_attempts: int,
    ) -> PatchProposalWorkItem:
        """Append one bounded retry after a typed failed proposer submission.

        A malformed raw Agent response is first preserved as a failed
        ``PatchProposalSubmission``.  The retry receives a distinct work id
        and an explicit repair counter, so it cannot overwrite that failure or
        silently reuse the original exported context.
        """

        row = self.connection.execute(
            "SELECT status, submission_payload, payload FROM work WHERE work_id = ?",
            (failed_work_id,),
        ).fetchone()
        if not row:
            raise KeyError(failed_work_id)
        if row["status"] != ProposalWorkStatus.FAILED.value or not row["submission_payload"]:
            raise ValueError("proposal repair requires an ingested failed work item")
        failed = PatchProposalSubmission.model_validate_json(row["submission_payload"])
        if failed.patch is not None:
            raise ValueError("proposal repair cannot replace a completed patch")
        work = PatchProposalWorkItem.model_validate_json(row["payload"])
        next_attempt = work.repair_attempt + 1
        if next_attempt > max_repair_attempts:
            raise ValueError("proposal repair budget is exhausted")
        repair = work.model_copy(
            update={
                "work_id": f"{work.work_id}-repair-{next_attempt}",
                "repair_attempt": next_attempt,
                "rejected_history": (
                    *work.rejected_history,
                    {
                        "failed_work_id": work.work_id,
                        "failed_submission_id": failed.submission_id,
                        "failure_kind": failed.failure_kind,
                        "failure_detail": failed.failure_detail,
                    },
                ),
            }
        )
        if not self.add_work(repair):
            return repair
        with self.connection:
            self._event(
                "proposal_repair_planned",
                repair.work_id,
                {"failed_work_id": failed_work_id, "repair_attempt": next_attempt},
            )
        return repair

    def ingest(self, submission: PatchProposalSubmission) -> bool:
        row = self.connection.execute(
            "SELECT status, submission_payload FROM work WHERE work_id = ?",
            (submission.work_id,),
        ).fetchone()
        if not row:
            raise KeyError(submission.work_id)
        if row["submission_payload"]:
            existing = PatchProposalSubmission.model_validate_json(row["submission_payload"])
            if existing != submission:
                raise ValueError("proposal work already has a different submission")
            return False
        status = submission.status.value
        with self.connection:
            self.connection.execute(
                "UPDATE work SET status = ?, submission_id = ?, submission_payload = ? "
                "WHERE work_id = ?",
                (
                    status,
                    submission.submission_id,
                    submission.model_dump_json(),
                    submission.work_id,
                ),
            )
            self._event("proposal_ingested", submission.work_id, {"status": status})
        return True

    def resume(self) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE work SET status = ? WHERE status = ? AND submission_id IS NULL",
                (ProposalWorkStatus.PENDING.value, ProposalWorkStatus.EXPORTED.value),
            )
            self._event("proposal_resumed", "run", {"count": cursor.rowcount})
        return int(cursor.rowcount)

    def submissions(self) -> list[PatchProposalSubmission]:
        rows = self.connection.execute(
            "SELECT submission_payload FROM work "
            "WHERE submission_payload IS NOT NULL ORDER BY rowid"
        ).fetchall()
        return [PatchProposalSubmission.model_validate_json(row[0]) for row in rows]

    def work_items(self) -> list[PatchProposalWorkItem]:
        rows = self.connection.execute("SELECT payload FROM work ORDER BY rowid").fetchall()
        return [PatchProposalWorkItem.model_validate_json(row[0]) for row in rows]

    def status(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status, COUNT(*) count FROM work GROUP BY status"
        ).fetchall()
        counts = {status.value: 0 for status in ProposalWorkStatus}
        counts.update({str(row["status"]): int(row["count"]) for row in rows})
        counts["total"] = sum(value for key, value in counts.items() if key != "total")
        return counts

    def write_snapshot(self, run_dir: Path) -> None:
        payload = {
            "schema_version": PACKAGE_PATCH_SCHEMA_VERSION,
            "status": self.status(),
            "submissions": [item.model_dump(mode="json") for item in self.submissions()],
        }
        atomic_write(run_dir / "proposal-checkpoint.json", canonical_json_bytes(payload))
