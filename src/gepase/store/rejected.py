"""Immutable rejected-edit memory keyed by canonical patch and node/op signatures."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import Field, model_validator

from gepase.mutation.schema import PackagePatch
from gepase.schemas.common import FrozenModel


class RejectedEditRecord(FrozenModel):
    schema_version: str = "1.0.0"
    record_id: str
    patch_id: str
    patch_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_candidate_id: str
    candidate_id: str | None = None
    node_ids: tuple[str, ...] = Field(min_length=1)
    operation_signatures: tuple[str, ...] = Field(min_length=1)
    evidence_refs: tuple[str, ...]
    failed_gate: str
    score_delta: float | None = None
    error_type: str
    reason_codes: tuple[str, ...] = Field(min_length=1)
    decision_id: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @model_validator(mode="after")
    def identity(self) -> RejectedEditRecord:
        payload = {
            "patch_id": self.patch_id,
            "fingerprint": self.patch_fingerprint,
            "parent": self.parent_candidate_id,
            "gate": self.failed_gate,
            "reasons": list(self.reason_codes),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        expected = f"rejected-{hashlib.sha256(raw.encode()).hexdigest()[:24]}"
        if self.record_id != expected:
            raise ValueError("rejected record id does not match immutable payload")
        return self


def rejected_record(
    patch: PackagePatch,
    *,
    parent_candidate_id: str,
    candidate_id: str | None,
    evidence_refs: tuple[str, ...],
    failed_gate: str,
    score_delta: float | None,
    error_type: str,
    reason_codes: tuple[str, ...],
    decision_id: str | None = None,
) -> RejectedEditRecord:
    payload = {
        "patch_id": patch.patch_id,
        "fingerprint": patch.fingerprint,
        "parent": parent_candidate_id,
        "gate": failed_gate,
        "reasons": list(reason_codes),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return RejectedEditRecord(
        record_id=f"rejected-{hashlib.sha256(raw.encode()).hexdigest()[:24]}",
        patch_id=patch.patch_id,
        patch_fingerprint=patch.fingerprint,
        parent_candidate_id=parent_candidate_id,
        candidate_id=candidate_id,
        node_ids=tuple(sorted(set(patch.selected_node_ids))),
        operation_signatures=tuple(
            sorted(
                f"{item.op.value}:{item.path}:{item.target_node_id or '-'}"
                for item in patch.operations
            )
        ),
        evidence_refs=evidence_refs,
        failed_gate=failed_gate,
        score_delta=score_delta,
        error_type=error_type,
        reason_codes=reason_codes,
        decision_id=decision_id,
    )


class RejectedEditStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS rejected_edits (
                record_id TEXT PRIMARY KEY,
                patch_id TEXT NOT NULL,
                patch_fingerprint TEXT NOT NULL,
                parent_candidate_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS rejected_fingerprint
                ON rejected_edits(patch_fingerprint, parent_candidate_id);
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.commit()
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        self.connection.close()

    def __enter__(self) -> RejectedEditStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def add(self, record: RejectedEditRecord) -> bool:
        row = self.connection.execute(
            "SELECT payload FROM rejected_edits WHERE record_id = ?", (record.record_id,)
        ).fetchone()
        if row:
            if RejectedEditRecord.model_validate_json(row["payload"]) != record:
                raise ValueError("rejected record id reused with different payload")
            return False
        with self.connection:
            self.connection.execute(
                "INSERT INTO rejected_edits(record_id, patch_id, patch_fingerprint, "
                "parent_candidate_id, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    record.record_id,
                    record.patch_id,
                    record.patch_fingerprint,
                    record.parent_candidate_id,
                    record.model_dump_json(),
                ),
            )
        return True

    def exact(self, patch_fingerprint: str, parent_candidate_id: str) -> RejectedEditRecord | None:
        row = self.connection.execute(
            "SELECT payload FROM rejected_edits WHERE patch_fingerprint = ? "
            "AND parent_candidate_id = ? ORDER BY rowid DESC LIMIT 1",
            (patch_fingerprint, parent_candidate_id),
        ).fetchone()
        return RejectedEditRecord.model_validate_json(row["payload"]) if row else None

    def relevant(
        self,
        node_ids: tuple[str, ...],
        *,
        limit: int = 10,
    ) -> list[RejectedEditRecord]:
        wanted = set(node_ids)
        rows = self.connection.execute(
            "SELECT payload FROM rejected_edits ORDER BY rowid DESC"
        ).fetchall()
        result = []
        for row in rows:
            item = RejectedEditRecord.model_validate_json(row["payload"])
            if wanted & set(item.node_ids):
                result.append(item)
            if len(result) >= limit:
                break
        return result

    def all(self) -> list[RejectedEditRecord]:
        rows = self.connection.execute(
            "SELECT payload FROM rejected_edits ORDER BY rowid"
        ).fetchall()
        return [RejectedEditRecord.model_validate_json(row["payload"]) for row in rows]
