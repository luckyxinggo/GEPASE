from __future__ import annotations

import json
from typing import Literal

import pytest

from gepase.evals.scores import TaskScoreVector
from gepase.evals.statistics import PairedScore
from gepase.optimizer.acceptance.models import GateOutcome
from gepase.optimizer.acceptance.validation import (
    ValidationPolicy,
    derive_task_score_secondary_evidence,
    run_validation_gate,
)


def _row(parent: float, candidate: float) -> PairedScore:
    return PairedScore(
        task_id="validation-1",
        category="behavior",
        risk_level="medium",
        parent_score=parent,
        candidate_score=candidate,
        evidence_tier="E3",
        minimum_acceptance_tier="E3",
        parent_record_id="parent-record",
        candidate_record_id="candidate-record",
    )


def test_route_a_accepts_strict_primary_improvement() -> None:
    decision = run_validation_gate(
        (_row(0.7, 0.8),),
        policy=ValidationPolicy(bootstrap_samples=500),
    )
    assert decision.gate.outcome is GateOutcome.PASSED
    assert decision.gate.checks["pareto_route"] == "A"


def test_route_b_accepts_quality_tie_with_measured_secondary_win() -> None:
    decision = run_validation_gate(
        (_row(0.8, 0.8),),
        policy=ValidationPolicy(bootstrap_samples=500),
        secondary_objective_improvements={"tokens": 0.08, "complexity": 0.0},
        secondary_evidence_refs=("train-independent-token-measurement",),
    )
    assert decision.gate.outcome is GateOutcome.PASSED
    assert decision.gate.checks["pareto_route"] == "B"


def test_route_b_rejects_docs_bloat_and_unmeasured_clarity() -> None:
    bloated = run_validation_gate(
        (_row(0.8, 0.8),),
        policy=ValidationPolicy(bootstrap_samples=500),
        secondary_objective_improvements={"complexity": -0.2},
    )
    assert bloated.gate.outcome is GateOutcome.FAILED
    unmeasured = run_validation_gate(
        (_row(0.8, 0.8),),
        policy=ValidationPolicy(bootstrap_samples=500),
    )
    assert unmeasured.gate.outcome is GateOutcome.INCONCLUSIVE


def test_primary_regression_cannot_be_hidden_by_secondary_win() -> None:
    decision = run_validation_gate(
        (_row(0.8, 0.7),),
        policy=ValidationPolicy(bootstrap_samples=500),
        secondary_objective_improvements={"tokens": 0.5},
    )
    assert decision.gate.outcome is GateOutcome.FAILED


def _write_vector(
    tmp_path,
    name: str,
    *,
    variant: Literal["no-skill", "original", "candidate"],
    efficiency: float,
    pair_id: str = "pair-1",
) -> str:
    vector = TaskScoreVector(
        task_id="validation-1",
        pair_id=pair_id,
        variant=variant,
        candidate_snapshot_hash="a" * 64,
        task_correctness=0.8,
        output_quality=0.8,
        skill_gain=0.0,
        reliability=1.0,
        efficiency=efficiency,
        package_quality=0.8,
        evidence_refs=("evidence.json",),
        scoring_policy_ref="scoring.json",
    )
    path = tmp_path / name
    path.write_text(json.dumps(vector.model_dump(mode="json")), encoding="utf-8")
    return name


def test_task_score_efficiency_mapping_handles_improvement_and_regression(tmp_path) -> None:
    parent_ref = _write_vector(tmp_path, "parent.json", variant="original", efficiency=0.6)
    candidate_ref = _write_vector(
        tmp_path,
        "candidate.json",
        variant="candidate",
        efficiency=0.75,
        pair_id="candidate-execution-pair",
    )
    row = _row(0.8, 0.8).model_copy(
        update={"parent_record_id": parent_ref, "candidate_record_id": candidate_ref}
    )
    evidence = derive_task_score_secondary_evidence(tmp_path, (row,))
    assert evidence.improvements == {"task_score_efficiency": pytest.approx(0.15)}
    assert evidence.primary_axes == ("task_correctness", "output_quality")

    regressed_ref = _write_vector(
        tmp_path, "regressed.json", variant="candidate", efficiency=0.4
    )
    regressed = derive_task_score_secondary_evidence(
        tmp_path,
        (row.model_copy(update={"candidate_record_id": regressed_ref}),),
    )
    assert regressed.improvements["task_score_efficiency"] == pytest.approx(-0.2)


def test_task_score_efficiency_mapping_fails_closed_on_missing_evidence(tmp_path) -> None:
    row = _row(0.8, 0.8).model_copy(
        update={
            "parent_record_id": "missing-parent.json",
            "candidate_record_id": "missing-candidate.json",
        }
    )
    with pytest.raises(FileNotFoundError):
        derive_task_score_secondary_evidence(tmp_path, (row,))


def test_validation_rejects_unknown_secondary_objective() -> None:
    with pytest.raises(ValueError, match="unregistered secondary"):
        run_validation_gate(
            (_row(0.8, 0.8),),
            policy=ValidationPolicy(bootstrap_samples=500),
            secondary_objective_improvements={"quality_efficiency": 0.2},
        )
