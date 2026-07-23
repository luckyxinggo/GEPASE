from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from gepase.optimizer.evolution.models import MergeEligibility
from gepase.store.evolution_pool import EvolutionPoolEntry, EvolutionPoolStore


def _legacy_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "candidate_id": "candidate-legacy",
        "parent_candidate_id": "parent-legacy",
        "patch_id": "patch-legacy",
        "train_evidence_refs": ["train-record-1"],
        "exclusive_task_keys": ["task-a"],
        "exclusive_objective_keys": [],
        "exclusive_component_ids": [],
        "train_mean_delta": 0.1,
        "train_floor_satisfied": True,
        "gate_0_1_passed": True,
        "not_deployable": True,
        "source_split": "train",
    }


def test_v1_pool_is_read_through_additive_v2_migration_without_lineage_inference(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evolution.sqlite3"
    original = json.dumps(_legacy_payload(), sort_keys=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE evolution_pool (candidate_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
    )
    connection.execute(
        "INSERT INTO evolution_pool(candidate_id, payload) VALUES (?, ?)",
        ("candidate-legacy", original),
    )
    connection.commit()
    connection.close()

    with EvolutionPoolStore(path) as store:
        entry = store.all()[0]
        assert entry.schema_version == "2.0.0"
        assert entry.migrated_from_schema_version == "1.0.0"
        assert entry.merge_eligibility is MergeEligibility.UNKNOWN_LEGACY
        assert entry.package_id is None
        assert entry.source_snapshot_hash is None
        assert entry.lineage_root_candidate_id is None
        assert entry.ancestor_candidate_ids == ()

    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT payload, payload_schema_version, merge_eligibility "
        "FROM evolution_pool WHERE candidate_id = ?",
        ("candidate-legacy",),
    ).fetchone()
    connection.close()
    assert row == (original, "1.0.0", "unknown_legacy")


def test_explicit_v2_identity_round_trips_as_merge_eligible(tmp_path: Path) -> None:
    entry = EvolutionPoolEntry(
        candidate_id="candidate-v2",
        parent_candidate_id="parent-v2",
        patch_id="patch-v2",
        package_id="package-demo",
        source_package_ref="skills/demo",
        source_snapshot_hash="1" * 64,
        lineage_root_candidate_id="root-v2",
        branch_id="branch-v2",
        branch_root_candidate_id="candidate-v2",
        failure_cluster_ids=("cluster-v2",),
        ancestor_candidate_ids=("root-v2", "parent-v2"),
        candidate_content_hash="2" * 64,
        train_evidence_refs=("train-record-v2",),
        exclusive_task_keys=("task-v2",),
        exclusive_closure_ids=("node-v2",),
        train_mean_delta=0.2,
        train_floor_satisfied=True,
        gate_0_1_passed=True,
        merge_eligibility=MergeEligibility.ELIGIBLE,
    )
    with EvolutionPoolStore(tmp_path / "evolution.sqlite3") as store:
        assert store.add(entry)
        assert store.all() == [entry]
