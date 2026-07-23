import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from gepase.evals.work_items import EvalWorkItem, WorkSubmission


def test_work_and_submission_reject_absolute_or_parent_paths() -> None:
    local = Path("artifacts/local")
    local.mkdir(parents=True, exist_ok=True)
    from gepase.evals.engine import MultiFidelityEvalEngine
    from gepase.evals.schema import EvidenceTier

    with tempfile.TemporaryDirectory(prefix="security-", dir=local) as temporary:
        with MultiFidelityEvalEngine(Path.cwd(), Path(temporary) / "run") as engine:
            engine.plan_cases(
                Path("benchmarks/manifest-draft.json"),
                splits=("validation",),
                tiers=(EvidenceTier.E1_SIMULATED,),
                variants=("original",),
                host="test",
                model="test",
                case_ids={"policy-evidence-06-00"},
            )
            item = engine.ledger.export_ready()[0]
    invalid_work = item.model_dump(mode="json")
    invalid_work["fixture_ref"] = "/private/fixture.json"
    with pytest.raises(ValidationError, match="repository-relative"):
        EvalWorkItem.model_validate(invalid_work)

    now = "2026-07-15T00:00:00Z"
    with pytest.raises(ValidationError, match="repository-relative"):
        WorkSubmission.model_validate(
            {
                "submission_id": "submission-x",
                "work_id": item.work_id,
                "provider_id": item.provider_id,
                "host": "test",
                "model": "test",
                "host_task_id": "test",
                "artifact_root": "../escape",
                "usage": {"duration_ms": 1},
                "started_at": now,
                "finished_at": now,
            }
        )

    valid = {
        "submission_id": "submission-proxy",
        "work_id": item.work_id,
        "provider_id": item.provider_id,
        "host": "test",
        "model": "test",
        "host_task_id": "test",
        "usage": {"duration_ms": 1},
        "started_at": now,
        "finished_at": now,
    }
    with pytest.raises(ValidationError, match="must be provided together"):
        WorkSubmission.model_validate({**valid, "proxy_score": 0.7})
