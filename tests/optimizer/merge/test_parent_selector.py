from __future__ import annotations

import json
from pathlib import Path

import pytest

from gepase.optimizer.evolution.models import (
    EvolutionCandidateIdentity,
    ExclusiveContribution,
    MergeParentCandidate,
)
from gepase.optimizer.evolution.parent_sets import enumerate_parent_sets
from gepase.optimizer.evolution.selection_lock import build_breeding_snapshot
from gepase.optimizer.merge.parent_selector import select_merge_parent_sets


def _handoff_payload() -> dict[str, object]:
    root = EvolutionCandidateIdentity(
        candidate_id="package-root",
        package_id="package-demo",
        source_package_ref="skills/package-demo",
        source_snapshot_hash="a" * 64,
        lineage_root_candidate_id="package-root",
        generation=0,
        operator="seed",
        content_hash="1" * 64,
    )
    identities = tuple(
        EvolutionCandidateIdentity(
            candidate_id=f"candidate-{index}",
            package_id="package-demo",
            source_package_ref="skills/package-demo",
            source_snapshot_hash="a" * 64,
            lineage_root_candidate_id="package-root",
            parent_ids=("package-root",),
            branch_id=f"branch-{index}",
            branch_root_candidate_id=f"candidate-{index}",
            generation=1,
            operator="reflective_mutation",
            content_hash=f"{index + 2:064x}",
            failure_cluster_ids=(f"cluster-{index}",),
        )
        for index in range(2)
    )
    parents = tuple(
        MergeParentCandidate(
            identity=identity,
            patch_id=f"patch-{index}",
            ancestor_chain=("package-root", identity.candidate_id),
            contribution=ExclusiveContribution(
                task_keys=(f"task-{index}",),
                component_ids=(f"component-{index}",),
                closure_ids=(f"closure-{index}",),
            ),
            train_evidence_refs=(f"train/evidence-{index}",),
            gate_0_1_passed=True,
            train_floor_satisfied=True,
        )
        for index, identity in enumerate(identities)
    )
    snapshot = build_breeding_snapshot(
        parents,
        (root, *identities),
        selection_config_hash="f" * 64,
        selection_feature_names=("train_mean", "exclusive_contribution"),
    )
    report = enumerate_parent_sets(snapshot)
    return {
        "schema_version": "1.0.0",
        "deployable_selection_features": [],
        "held_out_selection_features": [],
        "parent_sets": [
            row.parent_set.model_dump(mode="json") for row in report.ranked_parent_sets
        ],
    }


def _write_handoff(path: Path, payload: dict[str, object] | None = None) -> Path:
    path.write_text(json.dumps(payload or _handoff_payload()), encoding="utf-8")
    return path


def test_fixture_handoff_selection_is_train_only_and_contract_valid(tmp_path: Path) -> None:
    selected = select_merge_parent_sets(
        _write_handoff(tmp_path / "handoff.json"),
        package_allowlist={"package-demo"},
        limit=1,
    )
    assert {item.score.package_id for item in selected} == {"package-demo"}
    assert all(item.score.contract_revalidated for item in selected)
    assert all(item.score.held_out_features_read == 0 for item in selected)


def test_selector_rejects_heldout_breeding_features(tmp_path: Path) -> None:
    payload = _handoff_payload()
    payload["held_out_selection_features"] = ["validation_score"]
    with pytest.raises(ValueError, match=r"Held-out|held-out"):
        select_merge_parent_sets(_write_handoff(tmp_path / "handoff.json", payload))
