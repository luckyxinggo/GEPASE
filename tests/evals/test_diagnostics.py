from pathlib import Path

from gepase.evals.diagnostics import cache_resume_diagnostic, fault_injection_diagnostic


def test_fault_injection_covers_typed_failures_and_idempotency() -> None:
    result = fault_injection_diagnostic(Path.cwd())
    assert result["valid"] is True
    assert result["typed_failure_coverage"] == 6
    assert result["duplicate_completed"] == 0


def test_cache_replay_and_resume_do_not_repeat_completed_work() -> None:
    result = cache_resume_diagnostic(Path.cwd())
    assert result["valid"] is True
    assert result["replay_new_work_dispatches"] == 0
    assert result["artifact_hash_same"] is True
    assert result["completed_work_recounted"] == 0
    assert result["completed_work_rebilled"] == 0
