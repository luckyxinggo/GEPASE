from __future__ import annotations

import pytest

from gepase.evals.statistics import PairedScore
from gepase.optimizer.acceptance.diagnostics import (
    regression_floor_diagnostic,
    variance_policy_diagnostic,
)
from gepase.optimizer.acceptance.minibatch import MinibatchPolicy, run_minibatch_gate
from gepase.optimizer.acceptance.models import GateOutcome
from gepase.optimizer.acceptance.validation import ValidationPolicy, run_validation_gate


def _row(
    task: str,
    parent: float,
    candidate: float,
    *,
    category: str = "normal",
    risk: str = "low",
    tier: str = "E3",
    minimum: str = "E3",
) -> PairedScore:
    return PairedScore(
        task_id=task,
        category=category,
        risk_level=risk,
        parent_score=parent,
        candidate_score=candidate,
        evidence_tier=tier,
        minimum_acceptance_tier=minimum,
        parent_record_id=f"parent-{task}",
        candidate_record_id=f"candidate-{task}",
    )


def test_real_train_minibatch_can_promote_to_validation() -> None:
    decision = run_minibatch_gate(
        (
            _row("train-1", 0.5, 0.6, tier="E2", minimum="E2"),
            _row("train-2", 0.5, 0.5, tier="E2", minimum="E2"),
        ),
        policy=MinibatchPolicy(bootstrap_samples=500),
    )
    assert decision.gate.outcome is GateOutcome.PASSED
    assert decision.promote_to_validation
    assert decision.gate.checks["real_train_evidence_only"] is True


def test_e1_cannot_enter_train_acceptance_gate() -> None:
    with pytest.raises(ValueError, match="only real E2/E3"):
        run_minibatch_gate(
            (_row("train-1", 0.5, 0.6, tier="E1", minimum="E3"),),
            policy=MinibatchPolicy(bootstrap_samples=500),
        )


def test_validation_requires_minimum_tier_and_strict_gain() -> None:
    missing = run_validation_gate(
        (_row("validation-1", 0.5, 0.8, tier="E1", minimum="E3"),),
        policy=ValidationPolicy(bootstrap_samples=500),
    )
    assert missing.gate.outcome is GateOutcome.INCONCLUSIVE
    tied = run_validation_gate(
        (_row("validation-1", 1.0, 1.0),),
        policy=ValidationPolicy(bootstrap_samples=500),
    )
    assert tied.gate.outcome is GateOutcome.INCONCLUSIVE


def test_regression_floor_and_variance_diagnostics() -> None:
    assert regression_floor_diagnostic()["valid"] is True
    assert variance_policy_diagnostic()["valid"] is True


def test_category_floor_precedes_any_variance_route() -> None:
    decision = run_validation_gate(
        (
            _row("validation-risk", 0.9, 0.7, category="quality_efficiency"),
            _row("validation-win", 0.4, 0.9, category="behavior"),
        ),
        policy=ValidationPolicy(bootstrap_samples=500),
        secondary_objective_improvements={"task_score_efficiency": 0.2},
    )
    assert decision.statistics.mean_delta > 0
    assert decision.gate.outcome is GateOutcome.FAILED
    assert decision.gate.reason_codes == ("protected_objective_regression",)
