from __future__ import annotations

from gepase.evals.statistics import PairedScore, paired_statistics
from gepase.evals.variance import VarianceAction, VariancePolicy, variance_decision


def _row(index: int, parent: float, candidate: float) -> PairedScore:
    return PairedScore(
        task_id=f"validation-{index}",
        category="category",
        risk_level="low",
        parent_score=parent,
        candidate_score=candidate,
        evidence_tier="E3",
        minimum_acceptance_tier="E3",
        parent_record_id=f"parent-{index}",
        candidate_record_id=f"candidate-{index}",
    )


def test_paired_bootstrap_and_mcnemar_are_deterministic() -> None:
    rows = (_row(0, 0, 1), _row(1, 0, 1), _row(2, 1, 1), _row(3, 1, 0))
    first = paired_statistics(rows, seed=17, bootstrap_samples=1_000)
    second = paired_statistics(rows, seed=17, bootstrap_samples=1_000)
    assert first == second
    assert first.mean_delta == 0.25
    assert first.mcnemar is not None
    assert first.mcnemar.discordant_candidate_only == 2
    assert first.mcnemar.discordant_parent_only == 1


def test_variance_budget_exhaustion_never_becomes_acceptance() -> None:
    rows = tuple(
        _row(index, 0 if index % 2 == 0 else 1, 1 if index % 2 == 0 else 0) for index in range(8)
    )
    statistics = paired_statistics(rows, bootstrap_samples=1_000)
    policy = VariancePolicy(max_reevaluations=1)
    assert (
        variance_decision(
            statistics, mean_uncertainty=0.5, reevaluations_used=0, policy=policy
        ).action
        is VarianceAction.REEVALUATE
    )
    assert (
        variance_decision(
            statistics, mean_uncertainty=0.5, reevaluations_used=1, policy=policy
        ).action
        is VarianceAction.EXHAUSTED_INCONCLUSIVE
    )
