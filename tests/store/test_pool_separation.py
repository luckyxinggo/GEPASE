from __future__ import annotations

import pytest

from gepase.store.evolution_pool import (
    DeployableFrontierEntry,
    EvolutionPoolEntry,
)


def test_evolution_candidate_is_train_only_and_not_deployable() -> None:
    entry = EvolutionPoolEntry(
        candidate_id="candidate-a",
        parent_candidate_id="parent",
        patch_id="patch",
        train_evidence_refs=("train-record-1",),
        exclusive_task_keys=("train-task-1",),
        train_mean_delta=0.1,
        train_floor_satisfied=True,
        gate_0_1_passed=True,
    )
    assert entry.not_deployable is True
    with pytest.raises(ValueError, match="held-out evidence"):
        EvolutionPoolEntry(
            candidate_id="candidate-b",
            parent_candidate_id="parent",
            patch_id="patch",
            train_evidence_refs=("validation-record-1",),
            exclusive_task_keys=("train-task-1",),
            train_mean_delta=0.1,
            train_floor_satisfied=True,
            gate_0_1_passed=True,
        )


def test_deployable_entry_requires_gate_acceptance_and_children_reenter_s7() -> None:
    entry = DeployableFrontierEntry(
        candidate_id="candidate-a",
        decision_id="decision",
        validation_evidence_refs=("validation-record-1",),
    )
    assert entry.accepted is True
    assert entry.requires_full_s7_for_children is True
    with pytest.raises(ValueError, match="accepted"):
        DeployableFrontierEntry(
            candidate_id="candidate-b",
            decision_id="decision",
            validation_evidence_refs=("validation-record-2",),
            accepted=False,
        )
