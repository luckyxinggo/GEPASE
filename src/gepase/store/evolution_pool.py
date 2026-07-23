"""Strictly separated train-only evolution and held-out deployable pools."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Generic, Self, TypeVar

from pydantic import Field, model_validator

from gepase.optimizer.evolution.models import MergeEligibility
from gepase.schemas.common import FrozenModel
from gepase.store.artifacts import atomic_write, canonical_json_bytes
from gepase.store.migrations import (
    EVOLUTION_POOL_SCHEMA_V2,
    ensure_evolution_pool_v2,
    migrate_evolution_pool_json,
)


def _looks_held_out(ref: str) -> bool:
    text = ref.casefold()
    return any(marker in text for marker in ("validation", "/test", "test-", "held-out"))


class EvolutionPoolEntry(FrozenModel):
    schema_version: str = EVOLUTION_POOL_SCHEMA_V2
    migrated_from_schema_version: str | None = None
    candidate_id: str
    parent_candidate_id: str
    patch_id: str
    package_id: str | None = None
    source_package_ref: str | None = None
    source_snapshot_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    lineage_root_candidate_id: str | None = None
    branch_id: str | None = None
    branch_root_candidate_id: str | None = None
    failure_cluster_ids: tuple[str, ...] = ()
    ancestor_candidate_ids: tuple[str, ...] = ()
    candidate_content_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    train_evidence_refs: tuple[str, ...] = Field(min_length=1)
    exclusive_task_keys: tuple[str, ...] = ()
    exclusive_objective_keys: tuple[str, ...] = ()
    exclusive_component_ids: tuple[str, ...] = ()
    exclusive_closure_ids: tuple[str, ...] = ()
    train_mean_delta: float
    train_floor_satisfied: bool
    gate_0_1_passed: bool
    not_deployable: bool = True
    source_split: str = "train"
    merge_eligibility: MergeEligibility = MergeEligibility.UNKNOWN_LEGACY

    @model_validator(mode="after")
    def train_only(self) -> EvolutionPoolEntry:
        if self.source_split != "train":
            raise ValueError("evolution pool accepts train evidence only")
        if any(_looks_held_out(ref) for ref in self.train_evidence_refs):
            raise ValueError("held-out evidence cannot enter the evolution pool")
        if not self.not_deployable:
            raise ValueError("evolution entries are never deployable")
        if not self.gate_0_1_passed or not self.train_floor_satisfied:
            raise ValueError("evolution entry requires structural gates and train floor")
        if not any(
            (
                self.exclusive_task_keys,
                self.exclusive_objective_keys,
                self.exclusive_component_ids,
                self.exclusive_closure_ids,
            )
        ):
            raise ValueError("evolution entry requires an exclusive local contribution")
        for values, label in (
            (self.failure_cluster_ids, "failure_cluster_ids"),
            (self.ancestor_candidate_ids, "ancestor_candidate_ids"),
            (self.exclusive_task_keys, "exclusive_task_keys"),
            (self.exclusive_objective_keys, "exclusive_objective_keys"),
            (self.exclusive_component_ids, "exclusive_component_ids"),
            (self.exclusive_closure_ids, "exclusive_closure_ids"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must not contain duplicates")
        if self.candidate_id in self.ancestor_candidate_ids:
            raise ValueError("candidate cannot be its own ancestor")
        if self.merge_eligibility is not MergeEligibility.UNKNOWN_LEGACY:
            required = {
                "package_id": self.package_id,
                "source_package_ref": self.source_package_ref,
                "source_snapshot_hash": self.source_snapshot_hash,
                "lineage_root_candidate_id": self.lineage_root_candidate_id,
                "branch_id": self.branch_id,
                "branch_root_candidate_id": self.branch_root_candidate_id,
                "candidate_content_hash": self.candidate_content_hash,
            }
            missing = sorted(key for key, value in required.items() if not value)
            if missing:
                raise ValueError(f"non-legacy evolution entry missing explicit identity: {missing}")
            source = Path(self.source_package_ref or "")
            if source.is_absolute() or ".." in source.parts:
                raise ValueError("source_package_ref must be repository-relative")
            if not self.failure_cluster_ids:
                raise ValueError("non-legacy evolution entry requires failure_cluster_ids")
            if not self.ancestor_candidate_ids:
                raise ValueError("non-legacy evolution entry requires explicit ancestry proof")
            if self.parent_candidate_id not in self.ancestor_candidate_ids:
                raise ValueError("parent_candidate_id must appear in ancestry proof")
        return self


class DeployableFrontierEntry(FrozenModel):
    schema_version: str = "1.0.0"
    candidate_id: str
    decision_id: str
    validation_evidence_refs: tuple[str, ...] = Field(min_length=1)
    accepted: bool = True
    requires_full_s7_for_children: bool = True

    @model_validator(mode="after")
    def accepted_only(self) -> DeployableFrontierEntry:
        if not self.accepted:
            raise ValueError("deployable frontier accepts Gate-accepted candidates only")
        return self


PoolEntryT = TypeVar("PoolEntryT", bound=FrozenModel)


class _PoolStore(Generic[PoolEntryT]):
    table: str
    model: type[PoolEntryT]
    snapshot_schema_version: str = "1.0.0"

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            f"CREATE TABLE IF NOT EXISTS {self.table} ("
            "candidate_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.connection.commit()
        self._after_table_created()

    def close(self) -> None:
        self.connection.commit()
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        self.connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def add(self, entry: PoolEntryT) -> bool:
        candidate_id = str(entry.model_dump()["candidate_id"])
        row = self.connection.execute(
            f"SELECT payload FROM {self.table} WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row:
            if self._decode_payload(str(row["payload"])) != entry:
                raise ValueError("candidate pool identity reused with different payload")
            return False
        self._insert(candidate_id, entry)
        return True

    def all(self) -> list[PoolEntryT]:
        rows = self.connection.execute(
            f"SELECT payload FROM {self.table} ORDER BY rowid"
        ).fetchall()
        return [self._decode_payload(str(row["payload"])) for row in rows]

    def _after_table_created(self) -> None:
        return

    def _decode_payload(self, payload: str) -> PoolEntryT:
        return self.model.model_validate_json(payload)

    def _insert(self, candidate_id: str, entry: PoolEntryT) -> None:
        with self.connection:
            self.connection.execute(
                f"INSERT INTO {self.table}(candidate_id, payload) VALUES (?, ?)",
                (candidate_id, entry.model_dump_json()),
            )

    def snapshot(self, path: Path) -> None:
        atomic_write(
            path,
            canonical_json_bytes(
                {
                    "schema_version": self.snapshot_schema_version,
                    "pool": self.table,
                    "entries": [item.model_dump(mode="json") for item in self.all()],
                }
            ),
        )


class EvolutionPoolStore(_PoolStore[EvolutionPoolEntry]):
    table = "evolution_pool"
    model = EvolutionPoolEntry
    snapshot_schema_version = EVOLUTION_POOL_SCHEMA_V2

    def _after_table_created(self) -> None:
        ensure_evolution_pool_v2(self.connection, table=self.table)

    def _decode_payload(self, payload: str) -> EvolutionPoolEntry:
        return EvolutionPoolEntry.model_validate(migrate_evolution_pool_json(payload))

    def _insert(self, candidate_id: str, entry: EvolutionPoolEntry) -> None:
        with self.connection:
            self.connection.execute(
                f"INSERT INTO {self.table}("
                "candidate_id, payload, payload_schema_version, merge_eligibility"
                ") VALUES (?, ?, ?, ?)",
                (
                    candidate_id,
                    entry.model_dump_json(),
                    entry.schema_version,
                    entry.merge_eligibility.value,
                ),
            )


class DeployableFrontierStore(_PoolStore[DeployableFrontierEntry]):
    table = "deployable_frontier"
    model = DeployableFrontierEntry


def pool_separation_audit(run_dir: Path) -> dict[str, object]:
    with EvolutionPoolStore(run_dir / "evolution-pool.sqlite3") as evolution:
        evolution_rows = evolution.all()
    with DeployableFrontierStore(run_dir / "deployable-frontier.sqlite3") as deployable:
        deployable_rows = deployable.all()
    evolution_ids = {item.candidate_id for item in evolution_rows}
    deployable_ids = {item.candidate_id for item in deployable_rows}
    validation_breeding_refs = sum(
        _looks_held_out(ref) for item in evolution_rows for ref in item.train_evidence_refs
    )
    return {
        "schema_version": "1.0.0",
        "valid": validation_breeding_refs == 0,
        "evolution_pool_count": len(evolution_rows),
        "deployable_frontier_count": len(deployable_rows),
        "overlap_count": len(evolution_ids & deployable_ids),
        "validation_evidence_in_breeding": validation_breeding_refs,
        "evolution_all_not_deployable": all(item.not_deployable for item in evolution_rows),
        "merge_children_require_full_s7": all(
            item.requires_full_s7_for_children for item in deployable_rows
        ),
    }
