import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from gepase.evals.evidence import EvaluationRecord


def fixture(name: str) -> dict[str, object]:
    return json.loads(Path(f"tests/fixtures/evidence/{name}.json").read_text(encoding="utf-8"))


def test_e0_e1_cannot_claim_observed_actions() -> None:
    payload = fixture("e1")
    payload["observed_trace"] = [{"sequence": 0, "action": "execute", "outcome": "success"}]
    with pytest.raises(ValidationError, match="observed_trace must be empty"):
        EvaluationRecord.model_validate(payload)


def test_e2_requires_observed_artifact_and_provenance() -> None:
    payload = fixture("e2")
    payload["artifacts"] = []
    with pytest.raises(ValidationError, match="observed artifact evidence"):
        EvaluationRecord.model_validate(payload)

    payload = fixture("e2")
    payload["provenance"]["host_task_id"] = None  # type: ignore[index]
    with pytest.raises(ValidationError, match="host/model/task/submission provenance"):
        EvaluationRecord.model_validate(payload)


def test_e3_requires_source_record_and_assertions() -> None:
    payload = fixture("e3")
    payload["source_record_refs"] = []
    with pytest.raises(ValidationError, match="source record"):
        EvaluationRecord.model_validate(payload)
