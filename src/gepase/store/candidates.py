"""Durable candidate DAG, evaluation, reflection, frontier, and budget event store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from gepase.optimizer.candidate import PackageCandidate
from gepase.optimizer.frontier import FrontierSnapshot
from gepase.optimizer.gepa_adapter import CandidateEvaluation
from gepase.optimizer.status import CandidateStatus, CandidateStatusEvent
from gepase.optimizer.work_items import (
    ReflectionStatus,
    ReflectionSubmission,
    ReflectionWorkItem,
)
from gepase.store.artifacts import atomic_write, canonical_json_bytes


class CandidateStore:
    """SQLite is authoritative; JSON checkpoint/event files are auditable mirrors."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS candidates (
                candidate_id TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evaluations (
                evaluation_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                split TEXT NOT NULL,
                requested_tier TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(candidate_id, split, requested_tier)
            );
            CREATE TABLE IF NOT EXISTS reflection_work (
                work_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                submission_id TEXT,
                submission_payload TEXT
            );
            CREATE TABLE IF NOT EXISTS proposals (
                proposal_id TEXT PRIMARY KEY,
                candidate_id TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS frontiers (
                iteration INTEGER PRIMARY KEY,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS budget_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                charge_key TEXT UNIQUE NOT NULL,
                axis TEXT NOT NULL,
                amount REAL NOT NULL,
                usage_payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                entity_id TEXT,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candidate_status_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.commit()
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        self.connection.close()

    def __enter__(self) -> CandidateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _json(value: Any) -> str:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _event(self, kind: str, entity_id: str | None, payload: Any = None) -> None:
        self.connection.execute(
            "INSERT INTO events(event_type, entity_id, payload) VALUES (?, ?, ?)",
            (kind, entity_id, self._json(payload or {})),
        )

    def add_candidate(self, candidate: PackageCandidate, status: CandidateStatus) -> bool:
        payload = candidate.model_dump_json()
        row = self.connection.execute(
            "SELECT payload, status FROM candidates WHERE candidate_id = ?",
            (candidate.candidate_id,),
        ).fetchone()
        if row is not None:
            if PackageCandidate.model_validate_json(row["payload"]) != candidate:
                raise ValueError("candidate_id reused with a different immutable payload")
            return False
        with self.connection:
            self.connection.execute(
                "INSERT INTO candidates(candidate_id, content_hash, status, payload) "
                "VALUES (?, ?, ?, ?)",
                (candidate.candidate_id, candidate.content_hash, status.value, payload),
            )
            self._event("candidate_added", candidate.candidate_id, {"status": status.value})
        return True

    def set_candidate_status(
        self,
        candidate_id: str,
        status: CandidateStatus,
        *,
        event: CandidateStatusEvent | None = None,
    ) -> None:
        current_row = self.connection.execute(
            "SELECT status FROM candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if current_row is None:
            raise KeyError(candidate_id)
        current = CandidateStatus(str(current_row["status"]))
        if current is status:
            return
        if event is not None and (
            event.candidate_id != candidate_id
            or event.from_status is not current
            or event.to_status is not status
        ):
            raise ValueError("candidate status event does not match stored transition")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE candidates SET status = ? WHERE candidate_id = ?",
                (status.value, candidate_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(candidate_id)
            self._event("candidate_status", candidate_id, {"status": status.value})
            if event is not None:
                self.connection.execute(
                    "INSERT INTO candidate_status_events(candidate_id, payload) VALUES (?, ?)",
                    (candidate_id, event.model_dump_json()),
                )

    def candidate_status_events(self) -> list[CandidateStatusEvent]:
        rows = self.connection.execute(
            "SELECT payload FROM candidate_status_events ORDER BY event_id"
        ).fetchall()
        return [CandidateStatusEvent.model_validate_json(row["payload"]) for row in rows]

    def candidate(self, candidate_id: str) -> PackageCandidate:
        row = self.connection.execute(
            "SELECT payload FROM candidates WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return PackageCandidate.model_validate_json(row["payload"])

    def candidates(
        self, statuses: tuple[CandidateStatus, ...] | None = None
    ) -> list[PackageCandidate]:
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            rows = self.connection.execute(
                f"SELECT payload FROM candidates WHERE status IN ({placeholders}) ORDER BY rowid",
                tuple(status.value for status in statuses),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT payload FROM candidates ORDER BY rowid"
            ).fetchall()
        return [PackageCandidate.model_validate_json(row["payload"]) for row in rows]

    def candidate_statuses(self) -> dict[str, str]:
        rows = self.connection.execute(
            "SELECT candidate_id, status FROM candidates ORDER BY rowid"
        ).fetchall()
        return {str(row["candidate_id"]): str(row["status"]) for row in rows}

    def add_evaluation(self, evaluation: CandidateEvaluation) -> bool:
        payload = evaluation.model_dump_json()
        row = self.connection.execute(
            "SELECT payload FROM evaluations WHERE evaluation_id = ?",
            (evaluation.evaluation_id,),
        ).fetchone()
        if row is not None:
            if CandidateEvaluation.model_validate_json(row["payload"]) != evaluation:
                raise ValueError("evaluation_id reused with a different payload")
            return False
        with self.connection:
            self.connection.execute(
                "INSERT INTO evaluations("
                "evaluation_id, candidate_id, split, requested_tier, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    evaluation.evaluation_id,
                    evaluation.candidate_id,
                    evaluation.split,
                    evaluation.requested_tier.value,
                    payload,
                ),
            )
            self._event("evaluation_added", evaluation.evaluation_id)
        return True

    def evaluation(self, candidate_id: str, split: str) -> CandidateEvaluation:
        row = self.connection.execute(
            "SELECT payload FROM evaluations WHERE candidate_id = ? AND split = ? "
            "ORDER BY rowid DESC LIMIT 1",
            (candidate_id, split),
        ).fetchone()
        if row is None:
            raise KeyError((candidate_id, split))
        return CandidateEvaluation.model_validate_json(row["payload"])

    def evaluations(self) -> list[CandidateEvaluation]:
        rows = self.connection.execute("SELECT payload FROM evaluations ORDER BY rowid").fetchall()
        return [CandidateEvaluation.model_validate_json(row["payload"]) for row in rows]

    def add_reflection_work(self, work: ReflectionWorkItem) -> bool:
        row = self.connection.execute(
            "SELECT payload FROM reflection_work WHERE work_id = ?", (work.work_id,)
        ).fetchone()
        if row is not None:
            if ReflectionWorkItem.model_validate_json(row["payload"]) != work:
                raise ValueError("reflection work_id reused with a different payload")
            return False
        with self.connection:
            self.connection.execute(
                "INSERT INTO reflection_work(work_id, status, payload) VALUES (?, ?, ?)",
                (work.work_id, ReflectionStatus.PENDING.value, work.model_dump_json()),
            )
            self._event("reflection_planned", work.work_id)
        return True

    def reflection_work(self, work_id: str) -> ReflectionWorkItem:
        row = self.connection.execute(
            "SELECT payload FROM reflection_work WHERE work_id = ?", (work_id,)
        ).fetchone()
        if row is None:
            raise KeyError(work_id)
        return ReflectionWorkItem.model_validate_json(row["payload"])

    def export_reflection_work(self, limit: int | None = None) -> list[ReflectionWorkItem]:
        query = "SELECT payload FROM reflection_work WHERE status = ? ORDER BY rowid"
        params: tuple[object, ...] = (ReflectionStatus.PENDING.value,)
        if limit is not None:
            query += " LIMIT ?"
            params = (ReflectionStatus.PENDING.value, limit)
        rows = self.connection.execute(query, params).fetchall()
        work = [ReflectionWorkItem.model_validate_json(row["payload"]) for row in rows]
        with self.connection:
            for item in work:
                self.connection.execute(
                    "UPDATE reflection_work SET status = ? WHERE work_id = ?",
                    (ReflectionStatus.EXPORTED.value, item.work_id),
                )
                self._event("reflection_exported", item.work_id)
        return work

    def ingest_reflection(self, submission: ReflectionSubmission) -> bool:
        row = self.connection.execute(
            "SELECT status, submission_id, submission_payload FROM reflection_work "
            "WHERE work_id = ?",
            (submission.work_id,),
        ).fetchone()
        if row is None:
            raise KeyError(submission.work_id)
        if row["submission_id"] is not None:
            existing = ReflectionSubmission.model_validate_json(row["submission_payload"])
            if existing != submission:
                raise ValueError("reflection work already has a different submission")
            return False
        with self.connection:
            self.connection.execute(
                "UPDATE reflection_work SET status = ?, submission_id = ?, "
                "submission_payload = ? WHERE work_id = ?",
                (
                    ReflectionStatus.COMPLETED.value,
                    submission.submission_id,
                    submission.model_dump_json(),
                    submission.work_id,
                ),
            )
            self._event("reflection_completed", submission.work_id)
        return True

    def reflection_submission(self, work_id: str) -> ReflectionSubmission | None:
        row = self.connection.execute(
            "SELECT submission_payload FROM reflection_work WHERE work_id = ?", (work_id,)
        ).fetchone()
        if row is None:
            raise KeyError(work_id)
        return (
            ReflectionSubmission.model_validate_json(row["submission_payload"])
            if row["submission_payload"]
            else None
        )

    def resume_reflections(self) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE reflection_work SET status = ? WHERE status = ?",
                (ReflectionStatus.PENDING.value, ReflectionStatus.EXPORTED.value),
            )
            self._event("reflections_resumed", None, {"count": cursor.rowcount})
        return int(cursor.rowcount)

    def add_proposal(self, proposal_id: str, candidate_id: str, payload: dict[str, Any]) -> bool:
        serialized = self._json(payload)
        row = self.connection.execute(
            "SELECT payload FROM proposals WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if row is not None:
            if str(row["payload"]) != serialized:
                raise ValueError("proposal_id reused with a different payload")
            return False
        with self.connection:
            self.connection.execute(
                "INSERT INTO proposals(proposal_id, candidate_id, payload) VALUES (?, ?, ?)",
                (proposal_id, candidate_id, serialized),
            )
            self._event("proposal_added", proposal_id, {"candidate_id": candidate_id})
        return True

    def proposals(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT payload FROM proposals ORDER BY rowid").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def save_frontier(self, frontier: FrontierSnapshot) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO frontiers(iteration, payload) VALUES (?, ?)",
                (frontier.iteration, frontier.model_dump_json()),
            )
            self._event("frontier_advanced", str(frontier.iteration))

    def latest_frontier(self) -> FrontierSnapshot | None:
        row = self.connection.execute(
            "SELECT payload FROM frontiers ORDER BY iteration DESC LIMIT 1"
        ).fetchone()
        return FrontierSnapshot.model_validate_json(row["payload"]) if row else None

    def save_state(self, payload: dict[str, Any]) -> None:
        serialized = self._json(payload)
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO run_state(singleton, payload) VALUES (1, ?)",
                (serialized,),
            )
            self._event("run_state_saved", "run")

    def load_state(self) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT payload FROM run_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise KeyError("optimizer run state is not initialized")
        value = json.loads(row["payload"])
        if not isinstance(value, dict):
            raise ValueError("optimizer run state is not a mapping")
        return value

    def add_budget_event(
        self,
        charge_key: str,
        axis: str,
        amount: float,
        usage: dict[str, Any],
    ) -> bool:
        row = self.connection.execute(
            "SELECT axis, amount, usage_payload FROM budget_events WHERE charge_key = ?",
            (charge_key,),
        ).fetchone()
        if row is not None:
            if (
                row["axis"] != axis
                or float(row["amount"]) != float(amount)
                or json.loads(row["usage_payload"]) != usage
            ):
                raise ValueError("budget charge_key reused with different payload")
            return False
        with self.connection:
            self.connection.execute(
                "INSERT INTO budget_events(charge_key, axis, amount, usage_payload) "
                "VALUES (?, ?, ?, ?)",
                (charge_key, axis, amount, self._json(usage)),
            )
            self._event("budget_charged", charge_key, {"axis": axis, "amount": amount})
        return True

    def budget_events(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT charge_key, axis, amount, usage_payload FROM budget_events ORDER BY event_id"
        ).fetchall()
        return [
            {
                "charge_key": row["charge_key"],
                "axis": row["axis"],
                "amount": row["amount"],
                "usage": json.loads(row["usage_payload"]),
            }
            for row in rows
        ]

    def latest_budget_usage(self) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT usage_payload FROM budget_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        value = json.loads(row["usage_payload"])
        if not isinstance(value, dict):
            raise ValueError("budget usage payload is not a mapping")
        return value

    def snapshot(self) -> dict[str, Any]:
        frontier = self.latest_frontier()
        return {
            "schema_version": "1.0.0",
            "run_state": self.load_state(),
            "candidates": [item.model_dump(mode="json") for item in self.candidates()],
            "candidate_statuses": self.candidate_statuses(),
            "candidate_status_events": [
                item.model_dump(mode="json") for item in self.candidate_status_events()
            ],
            "evaluations": [item.model_dump(mode="json") for item in self.evaluations()],
            "proposals": self.proposals(),
            "frontier": frontier.model_dump(mode="json") if frontier is not None else None,
            "budget_events": self.budget_events(),
        }

    def write_checkpoint(self, run_dir: Path) -> dict[str, int]:
        self.connection.commit()
        self.connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        snapshot = self.snapshot()
        atomic_write(run_dir / "checkpoint.json", canonical_json_bytes(snapshot))
        rows = self.connection.execute(
            "SELECT event_id, event_type, entity_id, payload FROM events ORDER BY event_id"
        ).fetchall()
        event_text = "".join(
            json.dumps(
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "entity_id": row["entity_id"],
                    "payload": json.loads(row["payload"]),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
            for row in rows
        )
        atomic_write(run_dir / "events.jsonl", event_text.encode())
        return {
            "candidates": len(snapshot["candidates"]),
            "evaluations": len(snapshot["evaluations"]),
            "proposals": len(snapshot["proposals"]),
            "events": len(rows),
        }

    def counts(self) -> dict[str, int]:
        tables = ("candidates", "evaluations", "reflection_work", "proposals", "events")
        counts: dict[str, int] = {}
        for table in tables:
            row = self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            counts[table] = int(row[0])
        return counts
