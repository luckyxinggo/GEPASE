"""Additive store migrations that preserve immutable historical payloads."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

EVOLUTION_POOL_SCHEMA_V1 = "1.0.0"
EVOLUTION_POOL_SCHEMA_V2 = "2.0.0"
UNKNOWN_LEGACY = "unknown_legacy"

_V2_ADDITIVE_DEFAULTS: dict[str, Any] = {
    "package_id": None,
    "source_package_ref": None,
    "source_snapshot_hash": None,
    "lineage_root_candidate_id": None,
    "branch_id": None,
    "branch_root_candidate_id": None,
    "failure_cluster_ids": [],
    "ancestor_candidate_ids": [],
    "candidate_content_hash": None,
    "exclusive_closure_ids": [],
    "merge_eligibility": UNKNOWN_LEGACY,
}


def migrate_evolution_pool_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a v2 read model without mutating or inferring from a v1 payload."""

    source = dict(payload)
    version = str(source.get("schema_version", EVOLUTION_POOL_SCHEMA_V1))
    if version == EVOLUTION_POOL_SCHEMA_V2:
        return source
    if version != EVOLUTION_POOL_SCHEMA_V1:
        raise ValueError(f"unsupported evolution pool schema_version: {version}")
    migrated = dict(source)
    migrated["schema_version"] = EVOLUTION_POOL_SCHEMA_V2
    migrated["migrated_from_schema_version"] = EVOLUTION_POOL_SCHEMA_V1
    for key, value in _V2_ADDITIVE_DEFAULTS.items():
        migrated[key] = list(value) if isinstance(value, list) else value
    return migrated


def migrate_evolution_pool_json(payload: str) -> dict[str, Any]:
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        raise ValueError("evolution pool payload must be a JSON object")
    return migrate_evolution_pool_payload(raw)


def ensure_evolution_pool_v2(
    connection: sqlite3.Connection,
    *,
    table: str = "evolution_pool",
) -> None:
    """Add v2 metadata columns; existing JSON payload bytes remain untouched."""

    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if "payload_schema_version" not in columns:
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN payload_schema_version "
            f"TEXT NOT NULL DEFAULT '{EVOLUTION_POOL_SCHEMA_V1}'"
        )
    if "merge_eligibility" not in columns:
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN merge_eligibility "
            f"TEXT NOT NULL DEFAULT '{UNKNOWN_LEGACY}'"
        )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS pool_schema_migrations ("
        "store_name TEXT NOT NULL, schema_version TEXT NOT NULL, "
        "PRIMARY KEY(store_name, schema_version))"
    )
    connection.execute(
        "INSERT OR IGNORE INTO pool_schema_migrations(store_name, schema_version) VALUES (?, ?)",
        (table, EVOLUTION_POOL_SCHEMA_V2),
    )
    connection.commit()
