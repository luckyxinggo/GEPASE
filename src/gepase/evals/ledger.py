"""SQLite work-item ledger for idempotent ingest, cache, and resume."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from gepase.evals.errors import DuplicateSubmission, InvalidSubmission
from gepase.evals.evidence import EvaluationRecord
from gepase.evals.work_items import EvalWorkItem, WorkStatus, WorkSubmission, canonical_hash


class EvalLedger:
    def __init__(self, path: Path, *, create: bool = True, read_only: bool = False) -> None:
        if read_only and create:
            raise ValueError("a read-only ledger cannot be created")
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        elif not path.is_file():
            raise FileNotFoundError(f"evaluation ledger does not exist: {path}")
        self.path = path
        self.read_only = read_only
        if read_only:
            # Terminal-sealed ledgers must not create WAL/SHM sidecars merely
            # because an independent verifier opens them.  ``immutable=1`` is
            # safe here because this branch is used only after the caller has
            # resolved a sealed existing database.
            self.connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        else:
            self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        if not read_only:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
        if create:
            self._initialize()
        else:
            self._validate_schema()

    def _validate_schema(self) -> None:
        required = {"work_items", "records", "submissions", "cache", "events"}
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        present = {str(row["name"]) for row in rows}
        if not required <= present:
            raise ValueError(
                f"evaluation ledger schema is incomplete: {sorted(required - present)}"
            )

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS work_items (
                work_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
                record_id TEXT PRIMARY KEY,
                work_id TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS submissions (
                submission_id TEXT PRIMARY KEY,
                work_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cache (
                cache_key TEXT PRIMARY KEY,
                record_payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                work_id TEXT,
                detail TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def checkpoint(self) -> None:
        """Flush the WAL into the durable database before a run is sealed."""
        self.connection.commit()
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()

    def __enter__(self) -> EvalLedger:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def plan(self, item: EvalWorkItem, cache_key: str) -> str:
        existing = self.connection.execute(
            "SELECT status FROM work_items WHERE work_id = ?", (item.work_id,)
        ).fetchone()
        if existing is not None:
            return str(existing["status"])
        cached = self.connection.execute(
            "SELECT record_payload FROM cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        status = WorkStatus.COMPLETED if cached is not None else WorkStatus.PENDING
        with self.connection:
            self.connection.execute(
                "INSERT INTO work_items(work_id, status, cache_key, payload) VALUES (?, ?, ?, ?)",
                (
                    item.work_id,
                    status.value,
                    cache_key,
                    item.model_dump_json(),
                ),
            )
            if cached is not None:
                record = EvaluationRecord.model_validate_json(cached["record_payload"])
                self.connection.execute(
                    "INSERT OR IGNORE INTO records(record_id, work_id, payload) VALUES (?, ?, ?)",
                    (record.record_id, item.work_id, record.model_dump_json()),
                )
                self._event("cache_hit", item.work_id)
            else:
                self._event("planned", item.work_id)
        return status.value

    def get_work(self, work_id: str) -> EvalWorkItem:
        row = self.connection.execute(
            "SELECT payload FROM work_items WHERE work_id = ?", (work_id,)
        ).fetchone()
        if row is None:
            raise InvalidSubmission(f"unknown work_id: {work_id}")
        return EvalWorkItem.model_validate_json(row["payload"])

    def export_ready(self, limit: int | None = None) -> list[EvalWorkItem]:
        query = "SELECT payload FROM work_items WHERE status = ? ORDER BY work_id"
        parameters: tuple[object, ...] = (WorkStatus.PENDING.value,)
        if limit is not None:
            query += " LIMIT ?"
            parameters = (WorkStatus.PENDING.value, limit)
        rows = self.connection.execute(query, parameters).fetchall()
        items = [EvalWorkItem.model_validate_json(row["payload"]) for row in rows]
        with self.connection:
            for item in items:
                self.connection.execute(
                    "UPDATE work_items SET status = ? WHERE work_id = ?",
                    (WorkStatus.EXPORTED.value, item.work_id),
                )
                self._event("dispatched", item.work_id)
        return items

    def ready_items(self, limit: int | None = None) -> list[EvalWorkItem]:
        """Inspect the next atomic export batch without mutating work status."""

        query = "SELECT payload FROM work_items WHERE status = ? ORDER BY work_id"
        parameters: tuple[object, ...] = (WorkStatus.PENDING.value,)
        if limit is not None:
            query += " LIMIT ?"
            parameters = (WorkStatus.PENDING.value, limit)
        rows = self.connection.execute(query, parameters).fetchall()
        return [EvalWorkItem.model_validate_json(row["payload"]) for row in rows]

    def resume_interrupted(self) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE work_items SET status = ? WHERE status = ?",
                (WorkStatus.PENDING.value, WorkStatus.EXPORTED.value),
            )
            count = cursor.rowcount
            self._event("resumed", None, str(count))
        return count

    def record_for_work(self, work_id: str) -> EvaluationRecord | None:
        row = self.connection.execute(
            "SELECT payload FROM records WHERE work_id = ?", (work_id,)
        ).fetchone()
        return EvaluationRecord.model_validate_json(row["payload"]) if row else None

    def store_submission(
        self,
        submission: WorkSubmission,
        record: EvaluationRecord,
        *,
        failed: bool = False,
    ) -> tuple[EvaluationRecord, bool]:
        item = self.get_work(submission.work_id)
        payload_hash = canonical_hash(submission)
        existing_submission = self.connection.execute(
            "SELECT payload_hash FROM submissions WHERE submission_id = ?",
            (submission.submission_id,),
        ).fetchone()
        if existing_submission is not None:
            if existing_submission["payload_hash"] != payload_hash:
                raise DuplicateSubmission("submission_id reused with different payload")
            existing_record = self.record_for_work(item.work_id)
            if existing_record is None:
                raise InvalidSubmission("submission exists without a normalized record")
            return existing_record, True
        existing_record = self.record_for_work(item.work_id)
        if existing_record is not None:
            raise DuplicateSubmission("completed work received a different submission")
        row = self.connection.execute(
            "SELECT cache_key FROM work_items WHERE work_id = ?", (item.work_id,)
        ).fetchone()
        if row is None:
            raise InvalidSubmission("work item disappeared during ingest")
        status = WorkStatus.FAILED if failed else WorkStatus.COMPLETED
        with self.connection:
            self.connection.execute(
                "INSERT INTO submissions(submission_id, work_id, payload_hash, payload) "
                "VALUES (?, ?, ?, ?)",
                (
                    submission.submission_id,
                    item.work_id,
                    payload_hash,
                    submission.model_dump_json(),
                ),
            )
            self.connection.execute(
                "INSERT INTO records(record_id, work_id, payload) VALUES (?, ?, ?)",
                (record.record_id, item.work_id, record.model_dump_json()),
            )
            self.connection.execute(
                "UPDATE work_items SET status = ? WHERE work_id = ?",
                (status.value, item.work_id),
            )
            if not failed:
                self.connection.execute(
                    "INSERT OR REPLACE INTO cache(cache_key, record_payload) VALUES (?, ?)",
                    (row["cache_key"], record.model_dump_json()),
                )
            self._event("failed" if failed else "completed", item.work_id)
        return record, False

    def store_derived_record(self, record: EvaluationRecord) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO records(record_id, work_id, payload) VALUES (?, ?, ?)",
                (record.record_id, record.work_id, record.model_dump_json()),
            )
            self._event("derived_record", record.work_id, record.record_id)

    def complete_internal(self, item: EvalWorkItem, record: EvaluationRecord) -> None:
        row = self.connection.execute(
            "SELECT cache_key FROM work_items WHERE work_id = ?", (item.work_id,)
        ).fetchone()
        if row is None:
            raise InvalidSubmission("internal completion requires a planned work item")
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO records(record_id, work_id, payload) VALUES (?, ?, ?)",
                (record.record_id, item.work_id, record.model_dump_json()),
            )
            self.connection.execute(
                "UPDATE work_items SET status = ? WHERE work_id = ?",
                (WorkStatus.COMPLETED.value, item.work_id),
            )
            self.connection.execute(
                "INSERT OR REPLACE INTO cache(cache_key, record_payload) VALUES (?, ?)",
                (row["cache_key"], record.model_dump_json()),
            )
            self._event("completed_internal", item.work_id)

    def records(self) -> list[EvaluationRecord]:
        rows = self.connection.execute("SELECT payload FROM records ORDER BY record_id").fetchall()
        return [EvaluationRecord.model_validate_json(row["payload"]) for row in rows]

    def submissions(self) -> list[WorkSubmission]:
        rows = self.connection.execute(
            "SELECT payload FROM submissions ORDER BY submission_id"
        ).fetchall()
        return [WorkSubmission.model_validate_json(row["payload"]) for row in rows]

    def submission_for_work(self, work_id: str) -> WorkSubmission | None:
        row = self.connection.execute(
            "SELECT payload FROM submissions WHERE work_id = ?", (work_id,)
        ).fetchone()
        return WorkSubmission.model_validate_json(row["payload"]) if row else None

    def work_items(self) -> list[EvalWorkItem]:
        rows = self.connection.execute("SELECT payload FROM work_items ORDER BY work_id").fetchall()
        return [EvalWorkItem.model_validate_json(row["payload"]) for row in rows]

    def work_statuses(self) -> dict[str, WorkStatus]:
        rows = self.connection.execute(
            "SELECT work_id, status FROM work_items ORDER BY work_id"
        ).fetchall()
        return {str(row["work_id"]): WorkStatus(str(row["status"])) for row in rows}

    def status(self) -> dict[str, int]:
        counts = {status.value: 0 for status in WorkStatus}
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM work_items GROUP BY status"
        ).fetchall()
        for row in rows:
            counts[str(row["status"])] = int(row["count"])
        counts["records"] = int(
            self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        )
        counts["submissions"] = int(
            self.connection.execute("SELECT COUNT(*) FROM submissions").fetchone()[0]
        )
        counts["dispatches"] = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type = 'dispatched'"
            ).fetchone()[0]
        )
        counts["cache_hits"] = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type = 'cache_hit'"
            ).fetchone()[0]
        )
        return counts

    def _event(self, event_type: str, work_id: str | None, detail: str = "") -> None:
        self.connection.execute(
            "INSERT INTO events(event_type, work_id, detail) VALUES (?, ?, ?)",
            (event_type, work_id, detail),
        )
