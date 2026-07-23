import json
from pathlib import Path

from gepase.evals.evidence import EvaluationRecord
from gepase.evals.grader import blind_grader_work


def test_blind_grader_payload_excludes_variant_and_lineage() -> None:
    record = EvaluationRecord.model_validate_json(
        Path("tests/fixtures/evidence/e2.json").read_text(encoding="utf-8")
    )
    rubric = json.loads(
        Path("benchmarks/rubrics/blind-quality-v1.json").read_text(encoding="utf-8")
    )
    payload = blind_grader_work(record, task_prompt="Build the requested artifact.", rubric=rubric)
    keys = payload.model_dump(mode="json")
    assert "variant" not in keys
    assert "candidate_snapshot_hash" not in keys
    assert "parents" not in keys
