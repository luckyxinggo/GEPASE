from __future__ import annotations

import pytest

from gepase.optimizer.merge.models import MergeOutcome, MergeOutcomeStatus


def test_no_eligible_parent_set_is_a_typed_terminal() -> None:
    outcome = MergeOutcome(
        status=MergeOutcomeStatus.NO_ELIGIBLE_PARENT_SET,
        considered_parent_candidate_ids=("a", "b", "c"),
        considered_parent_set_count=3,
        eligible_parent_set_count=0,
        rejected_parent_set_count=3,
        rejection_reason_counts={"contribution_not_distinct": 2, "ancestor_relation": 1},
        cross_package_pair_count=0,
        enumeration_ref="artifacts/runs/run/merge/parent-set-enumeration.json",
    )
    assert outcome.status is MergeOutcomeStatus.NO_ELIGIBLE_PARENT_SET
    assert outcome.merge_candidate_id is None


def test_materialized_merge_requires_evaluation_before_terminal() -> None:
    pending = MergeOutcome(
        status=MergeOutcomeStatus.MATERIALIZED_PENDING_EVALUATION,
        considered_parent_candidate_ids=("a", "b", "c"),
        considered_parent_set_count=3,
        eligible_parent_set_count=1,
        rejected_parent_set_count=2,
        rejection_reason_counts={"not_distinct": 2},
        cross_package_pair_count=0,
        selected_parent_set_id="set-a-b",
        merge_candidate_id="merge-child",
        enumeration_ref="artifacts/runs/run/merge/enumeration.json",
        build_record_ref="artifacts/runs/run/merge/build.json",
    )
    assert not pending.evaluation_complete
    invalid = pending.model_dump(mode="json")
    invalid["status"] = MergeOutcomeStatus.MATERIALIZED_AND_EVALUATED.value
    with pytest.raises(ValueError, match="status is inconsistent"):
        MergeOutcome.model_validate(invalid)


def test_merge_outcome_rejects_partial_set_accounting_and_cross_package_ref() -> None:
    with pytest.raises(ValueError, match="considered set count"):
        MergeOutcome(
            status=MergeOutcomeStatus.NO_ELIGIBLE_PARENT_SET,
            considered_parent_candidate_ids=("a", "b"),
            considered_parent_set_count=2,
            eligible_parent_set_count=0,
            rejected_parent_set_count=1,
            rejection_reason_counts={"not_distinct": 1},
            cross_package_pair_count=0,
            enumeration_ref="artifacts/runs/run/merge/enumeration.json",
        )
    with pytest.raises(ValueError, match="repository-relative"):
        MergeOutcome(
            status=MergeOutcomeStatus.NO_ELIGIBLE_PARENT_SET,
            considered_parent_candidate_ids=("a", "b"),
            considered_parent_set_count=1,
            eligible_parent_set_count=0,
            rejected_parent_set_count=1,
            rejection_reason_counts={"cross_package": 1},
            cross_package_pair_count=1,
            enumeration_ref="../other-package/enumeration.json",
        )
