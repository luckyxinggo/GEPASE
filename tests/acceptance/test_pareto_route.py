from __future__ import annotations

from gepase.evals.statistics import PairedScore
from gepase.optimizer.acceptance.models import GateOutcome
from gepase.optimizer.acceptance.validation import ValidationPolicy, run_validation_gate


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
