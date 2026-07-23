from __future__ import annotations

import gepase
from gepase.evals.scores import TaskScoreVector
from gepase.evals.work_items import ExecutionBundle, WorkSubmission


def test_execution_bundle_has_one_authoritative_class() -> None:
    assert WorkSubmission is ExecutionBundle
    assert gepase.ExecutionBundle is ExecutionBundle


def test_task_score_vector_preserves_six_separate_objectives() -> None:
    vector = TaskScoreVector(
        task_id="case-1",
        pair_id="pair-1",
        variant="candidate",
        candidate_snapshot_hash="a" * 64,
        task_correctness=0.9,
        output_quality=0.8,
        skill_gain=0.2,
        reliability=0.95,
        efficiency=0.7,
        package_quality=0.85,
        evidence_refs=("grading/case-1.json",),
        scoring_policy_ref="eval-plan-v1#functional-scoring",
    )
    assert set(vector.objectives) == {
        "task_correctness",
        "output_quality",
        "skill_gain",
        "reliability",
        "efficiency",
        "package_quality",
    }
    assert "aggregate" not in TaskScoreVector.model_fields


def test_public_core_exports_one_model_per_boundary() -> None:
    expected = {
        "EvalWorkItem",
        "EvolutionPoolEntry",
        "ExecutionBundle",
        "GateDecision",
        "MergeParentSetSnapshot",
        "PackageCandidate",
        "PackageGraph",
        "PackagePatch",
        "TaskScoreVector",
    }
    assert expected <= set(gepase.__all__)
