import pytest
from pydantic import ValidationError

from gepase.evals.schema import AssertionSpec, CaseProvenance, EvidenceTier, TaskCase


def valid_case() -> dict[str, object]:
    return {
        "id": "case-1",
        "skill_id": "skill-a",
        "capability_manifest_ref": "capability.json",
        "prompt": "produce output",
        "input": {},
        "fixture_ref": "fixture.json",
        "fixture_sha256": "0" * 64,
        "allowed_evidence_tiers": ["E1", "E3"],
        "minimum_acceptance_tier": "E3",
        "assertions": [
            AssertionSpec(
                assertion_id="exists",
                family="file_exists",
                parameters={"path": "output.txt"},
            )
        ],
        "category": "test",
        "difficulty": "medium",
        "risk_level": "low",
        "required_capability": ["write_file"],
        "leakage_group": "group-1",
        "split": "train",
        "deterministic_weight": 0.8,
        "judge_weight": 0.2,
        "provenance": CaseProvenance(
            kind="handcrafted", reference="unit-test", license="Apache-2.0"
        ),
    }


def test_task_case_enforces_minimum_tier_and_score_composition() -> None:
    case = TaskCase.model_validate(valid_case())
    assert case.minimum_acceptance_tier is EvidenceTier.E3_EXECUTABLE

    invalid_tier = valid_case()
    invalid_tier["allowed_evidence_tiers"] = ["E1"]
    with pytest.raises(ValidationError, match="minimum_acceptance_tier"):
        TaskCase.model_validate(invalid_tier)

    invalid_weight = valid_case()
    invalid_weight["judge_weight"] = 0.3
    with pytest.raises(ValidationError, match="must sum"):
        TaskCase.model_validate(invalid_weight)
