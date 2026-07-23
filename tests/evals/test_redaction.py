from pathlib import Path

import pytest
from pydantic import ValidationError

from gepase.evals.errors import PartialArtifact
from gepase.evals.evidence import TraceStep
from gepase.evals.providers.artifact import ArtifactProvider
from gepase.evals.work_items import WorkSubmission
from gepase.schemas.common import ArtifactRef
from gepase.store.artifacts import sha256_bytes


def test_trace_rejects_secret_and_private_user_path() -> None:
    fake_key = "sk-" + "canary012345678901234567890"
    with pytest.raises(ValidationError, match="api_key"):
        TraceStep(sequence=0, action="use", target=fake_key)
    private_path = "/Users/" + "example/private/input.txt"
    with pytest.raises(ValidationError, match="private_home_path"):
        TraceStep(sequence=0, action="read", target=private_path)


def test_artifact_provider_rejects_sensitive_text(tmp_path: Path) -> None:
    fake_key = "sk-" + "canary012345678901234567890"
    artifact = tmp_path / "output.txt"
    content = f"credential={fake_key}".encode()
    artifact.write_bytes(content)
    submission = WorkSubmission.model_validate(
        {
            "submission_id": "submission-sensitive",
            "work_id": "work-sensitive",
            "provider_id": "agent-delegated-v1",
            "host": "test",
            "model": "test",
            "host_task_id": "test",
            "artifact_root": tmp_path.name,
            "artifacts": [
                ArtifactRef(
                    path="output.txt",
                    sha256=sha256_bytes(content),
                    media_type="text/plain",
                    size_bytes=len(content),
                )
            ],
            "observed_trace": [{"sequence": 0, "action": "write"}],
            "usage": {"duration_ms": 1},
            "started_at": "2026-07-15T00:00:00Z",
            "finished_at": "2026-07-15T00:00:00Z",
        }
    )
    with pytest.raises(PartialArtifact, match="sensitive content"):
        ArtifactProvider().verify(tmp_path.parent, submission)
