import json
from pathlib import Path

import pytest

from gepase.evals.errors import PairNotComparable
from gepase.evals.evidence import EvaluationRecord
from gepase.evals.paired import compare_pair, require_comparable


def test_pair_only_allows_variant_and_candidate_snapshot_difference() -> None:
    original = EvaluationRecord.model_validate_json(
        Path("tests/fixtures/evidence/e1.json").read_text(encoding="utf-8")
    )
    baseline = original.model_copy(
        update={
            "record_id": "e1-baseline",
            "work_id": "work-e1-baseline",
            "variant": "no-skill",
            "candidate_snapshot_hash": "f" * 64,
        }
    )
    assert compare_pair(baseline, original)["pair_comparable"] is True
    require_comparable(baseline, original)

    incompatible_payload = json.loads(original.model_dump_json())
    incompatible_payload["host_model_snapshot"] = "different"
    incompatible = EvaluationRecord.model_validate(incompatible_payload)
    with pytest.raises(PairNotComparable):
        require_comparable(baseline, incompatible)
