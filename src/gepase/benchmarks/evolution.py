"""Verification for a transparent public evolution/repair development track."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def verify_evolution_track(
    project_root: Path,
    freeze_lock: Path,
    track_root: Path,
) -> dict[str, object]:
    root = project_root.resolve()
    lock = json.loads(freeze_lock.read_text(encoding="utf-8"))
    mismatches: list[str] = []
    for relative, expected in lock.get("files", {}).items():
        path = root / str(relative)
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if actual != expected:
            mismatches.append(str(relative))
    track_path = track_root / "track.json"
    track: dict[str, Any] = (
        json.loads(track_path.read_text(encoding="utf-8")) if track_path.is_file() else {}
    )
    faults = track.get("fault_families", [])
    provenance_missing = sum(
        not all(
            item.get(key)
            for key in (
                "fault_id",
                "origin_kind",
                "base_skill_ref",
                "changed_component",
                "oracle_ref",
            )
        )
        for item in faults
        if isinstance(item, dict)
    )
    provenance_missing += int(not faults)
    invalid_origin = sum(
        item.get("origin_kind")
        not in {"historical_version", "sanitized_real_failure", "preregistered_realistic_fault"}
        for item in faults
        if isinstance(item, dict)
    )
    oracle_errors = sum(not (track_root / str(item.get("oracle_ref"))).is_file() for item in faults)
    leakage_errors = int(bool(track.get("test_case_ids"))) + int(
        track.get("test_access_policy") != "forbidden"
    )
    valid = not any((mismatches, provenance_missing, invalid_origin, oracle_errors, leakage_errors))
    return {
        "schema_version": "1.0.0",
        "valid": valid,
        "v1_hash_mismatch": len(mismatches),
        "v1_hash_mismatch_files": mismatches,
        "repair_provenance_missing": provenance_missing,
        "fault_family_errors": invalid_origin,
        "oracle_errors": oracle_errors,
        "leakage_errors": leakage_errors,
        "test_access": 0,
        "fault_family_count": len(faults),
    }
