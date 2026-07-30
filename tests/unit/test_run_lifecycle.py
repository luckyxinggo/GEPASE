from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gepase.run_lifecycle import (
    RunLifecycle,
    RunLifecycleMode,
    RunLifecycleStatus,
)


def _initialized(tmp_path, *, config_hash: str = "a" * 64) -> RunLifecycle:
    run = tmp_path / "run"
    lifecycle = RunLifecycle(
        run,
        run_id="run",
        owner="eval",
        expected_config_hash=config_hash,
    )
    lifecycle.prepare(RunLifecycleMode.CREATE_NEW)
    (run / "metadata.json").write_text("frozen", encoding="utf-8")
    lifecycle.checkpoint(
        config_hash=config_hash,
        status=RunLifecycleStatus.ACTIVE,
        critical_files=("metadata.json",),
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )
    return lifecycle


def test_create_open_resume_are_explicit_and_resume_is_idempotent(tmp_path) -> None:
    lifecycle = _initialized(tmp_path)
    with pytest.raises(FileExistsError):
        lifecycle.prepare(RunLifecycleMode.CREATE_NEW)
    lifecycle.prepare(
        RunLifecycleMode.OPEN_EXISTING, required_files=("metadata.json",)
    )
    first = lifecycle.prepare(
        RunLifecycleMode.RESUME, required_files=("metadata.json",)
    )
    second = lifecycle.prepare(
        RunLifecycleMode.RESUME, required_files=("metadata.json",)
    )
    assert first == second


def test_resume_rejects_tamper_wrong_config_and_terminal_state(tmp_path) -> None:
    lifecycle = _initialized(tmp_path)
    (lifecycle.run_dir / "metadata.json").write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        lifecycle.prepare(RunLifecycleMode.RESUME)

    clean = _initialized(tmp_path / "clean")
    wrong = RunLifecycle(
        clean.run_dir,
        run_id="run",
        owner="eval",
        expected_config_hash="b" * 64,
    )
    with pytest.raises(ValueError, match="config hash"):
        wrong.prepare(RunLifecycleMode.RESUME)

    terminal = _initialized(tmp_path / "terminal")
    terminal.checkpoint(
        config_hash="a" * 64,
        status=RunLifecycleStatus.COMPLETE,
        critical_files=("metadata.json",),
    )
    with pytest.raises(ValueError, match="terminal run"):
        terminal.prepare(RunLifecycleMode.RESUME)


def test_empty_and_half_initialized_directories_fail_closed(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    lifecycle = RunLifecycle(empty, run_id="empty", owner="eval")
    with pytest.raises(ValueError, match="missing required state"):
        lifecycle.prepare(
            RunLifecycleMode.OPEN_EXISTING, required_files=("metadata.json",)
        )

    half = tmp_path / "half"
    half_lifecycle = RunLifecycle(half, run_id="half", owner="eval")
    half_lifecycle.prepare(RunLifecycleMode.CREATE_NEW)
    (half / "metadata.json").write_text("value", encoding="utf-8")
    with pytest.raises(ValueError, match="half-initialized"):
        half_lifecycle.prepare(
            RunLifecycleMode.OPEN_EXISTING, required_files=("metadata.json",)
        )


def test_empty_initialization_recovery_is_exact_and_does_not_mutate_state(
    tmp_path,
) -> None:
    run = tmp_path / "run"
    lifecycle = RunLifecycle(run, run_id="run", owner="eval")
    initial = lifecycle.prepare(RunLifecycleMode.CREATE_NEW)
    (run / "ledger.sqlite3").write_bytes(b"ledger")
    (run / "artifact-index.json").write_text("{}", encoding="utf-8")

    recovered = lifecycle.recover_empty_initialization(
        required_files=("ledger.sqlite3", "artifact-index.json"),
        allowed_files=("run-lifecycle.json", "ledger.sqlite3", "artifact-index.json"),
    )
    assert recovered == initial
    assert lifecycle.record() == initial

    (run / "unexpected.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected durable state"):
        lifecycle.recover_empty_initialization(
            required_files=("ledger.sqlite3", "artifact-index.json"),
            allowed_files=("run-lifecycle.json", "ledger.sqlite3", "artifact-index.json"),
        )
