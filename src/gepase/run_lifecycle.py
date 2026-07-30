"""Fail-closed lifecycle primitives shared by Core-owned run directories.

The lifecycle record does not execute Agent work and does not own evaluation or
optimizer state.  It only makes create/open/resume intent explicit and binds a
typed checkpoint to the authoritative files owned by the existing subsystem.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from gepase.schemas.common import FrozenModel
from gepase.store.artifacts import atomic_write, canonical_json_bytes, sha256_bytes


class RunLifecycleMode(StrEnum):
    CREATE_NEW = "create_new"
    OPEN_EXISTING = "open_existing"
    RESUME = "resume"


class RunLifecycleStatus(StrEnum):
    INITIALIZING = "initializing"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETE = "complete"
    ABORTED = "aborted"


class RunLifecycleRecord(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    run_id: str = Field(min_length=1)
    owner: Literal["eval", "evolution"]
    status: RunLifecycleStatus
    config_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    checkpoint_id: str | None = None
    created_at: datetime
    updated_at: datetime


class RunIntegrityCheckpoint(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    checkpoint_id: str
    run_id: str = Field(min_length=1)
    owner: Literal["eval", "evolution"]
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: RunLifecycleStatus
    critical_artifact_hashes: dict[str, str] = Field(min_length=1)
    created_at: datetime

    @model_validator(mode="after")
    def hashes_are_sha256(self) -> RunIntegrityCheckpoint:
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in self.critical_artifact_hashes.values()
        ):
            raise ValueError("critical artifact hashes must be lowercase SHA-256")
        return self


class RunLifecycle:
    """Prepare one run directory without silently creating or repairing state."""

    RECORD_NAME = "run-lifecycle.json"
    CHECKPOINT_NAME = "lifecycle-checkpoint.json"

    def __init__(
        self,
        run_dir: Path,
        *,
        run_id: str,
        owner: Literal["eval", "evolution"],
        expected_config_hash: str | None = None,
    ) -> None:
        self.run_dir = run_dir.resolve()
        self.run_id = run_id
        self.owner: Literal["eval", "evolution"] = owner
        self.expected_config_hash = expected_config_hash

    @property
    def record_path(self) -> Path:
        return self.run_dir / self.RECORD_NAME

    @property
    def checkpoint_path(self) -> Path:
        return self.run_dir / self.CHECKPOINT_NAME

    def prepare(
        self,
        mode: RunLifecycleMode,
        *,
        required_files: tuple[str, ...] = (),
        allow_legacy_open: bool = False,
    ) -> RunLifecycleRecord | None:
        if mode is RunLifecycleMode.CREATE_NEW:
            if self.run_dir.exists():
                raise FileExistsError(
                    f"create_new requires a nonexistent run directory: {self.run_dir}"
                )
            self.run_dir.mkdir(parents=True)
            now = datetime.now(UTC)
            record = RunLifecycleRecord(
                run_id=self.run_id,
                owner=self.owner,
                status=RunLifecycleStatus.INITIALIZING,
                config_hash=self.expected_config_hash,
                created_at=now,
                updated_at=now,
            )
            self._write_record(record)
            return record

        if not self.run_dir.is_dir():
            raise FileNotFoundError(f"existing run directory is unavailable: {self.run_dir}")
        for relative in required_files:
            path = self.run_dir / relative
            if not path.is_file():
                raise ValueError(f"existing run is missing required state: {relative}")
        if not self.record_path.is_file():
            if allow_legacy_open and mode is RunLifecycleMode.OPEN_EXISTING:
                return None
            raise ValueError("existing run has no typed lifecycle metadata")
        record = self.record()
        self._validate_identity(record)
        if record.status is RunLifecycleStatus.INITIALIZING:
            raise ValueError("half-initialized run cannot be opened or resumed")
        if mode is RunLifecycleMode.RESUME:
            if record.status in {RunLifecycleStatus.COMPLETE, RunLifecycleStatus.ABORTED}:
                raise ValueError(f"terminal run cannot resume: {record.status.value}")
            self.verify_checkpoint()
        return record

    def recover_empty_initialization(
        self,
        *,
        required_files: tuple[str, ...],
        allowed_files: tuple[str, ...],
    ) -> RunLifecycleRecord:
        """Re-open one exact, typed initialization shell without deleting it.

        This is intentionally narrower than ``RESUME``: it accepts only an
        ``initializing`` record with no checkpoint and a caller-supplied exact
        file allowlist.  The owning subsystem must separately prove that its
        durable ledger is empty before continuing initialization.
        """

        if not self.run_dir.is_dir():
            raise FileNotFoundError(f"initializing run directory is unavailable: {self.run_dir}")
        for relative in required_files:
            path = self.run_dir / relative
            if not path.is_file():
                raise ValueError(f"initializing run is missing bootstrap state: {relative}")
        if not self.record_path.is_file():
            raise ValueError("initializing run has no typed lifecycle metadata")
        record = self.record()
        self._validate_identity(record)
        if record.status is not RunLifecycleStatus.INITIALIZING:
            raise ValueError("initialization recovery requires initializing status")
        if record.checkpoint_id is not None or self.checkpoint_path.exists():
            raise ValueError("initialization recovery cannot bypass a typed checkpoint")
        allowed = {Path(item).as_posix() for item in allowed_files}
        existing = {
            path.relative_to(self.run_dir).as_posix()
            for path in self.run_dir.rglob("*")
            if path.is_file()
        }
        unexpected = sorted(existing - allowed)
        if unexpected:
            raise ValueError(f"initializing run contains unexpected durable state: {unexpected}")
        return record

    def record(self) -> RunLifecycleRecord:
        return RunLifecycleRecord.model_validate_json(
            self.record_path.read_text(encoding="utf-8")
        )

    def checkpoint(
        self,
        *,
        config_hash: str,
        status: RunLifecycleStatus,
        critical_files: tuple[str, ...],
        now: datetime | None = None,
    ) -> RunIntegrityCheckpoint:
        record = self.record()
        self._validate_identity(record)
        if record.config_hash not in (None, config_hash):
            raise ValueError("run lifecycle config hash changed")
        hashes = self._hash_critical_files(critical_files)
        created_at = now or datetime.now(UTC)
        identity = {
            "run_id": self.run_id,
            "owner": self.owner,
            "config_hash": config_hash,
            "status": status.value,
            "critical_artifact_hashes": hashes,
        }
        checkpoint_id = f"checkpoint-{sha256_bytes(canonical_json_bytes(identity))[:24]}"
        checkpoint = RunIntegrityCheckpoint(
            checkpoint_id=checkpoint_id,
            run_id=self.run_id,
            owner=self.owner,
            config_hash=config_hash,
            status=status,
            critical_artifact_hashes=hashes,
            created_at=created_at,
        )
        atomic_write(
            self.checkpoint_path,
            canonical_json_bytes(checkpoint.model_dump(mode="json")),
        )
        self._write_record(
            record.model_copy(
                update={
                    "status": status,
                    "config_hash": config_hash,
                    "checkpoint_id": checkpoint_id,
                    "updated_at": created_at,
                }
            )
        )
        return checkpoint

    def verify_checkpoint(self) -> RunIntegrityCheckpoint:
        if not self.checkpoint_path.is_file():
            raise ValueError("resume requires a typed lifecycle checkpoint")
        checkpoint = RunIntegrityCheckpoint.model_validate_json(
            self.checkpoint_path.read_text(encoding="utf-8")
        )
        if checkpoint.run_id != self.run_id or checkpoint.owner != self.owner:
            raise ValueError("checkpoint belongs to another run")
        if (
            self.expected_config_hash is not None
            and checkpoint.config_hash != self.expected_config_hash
        ):
            raise ValueError("checkpoint config hash differs from requested config")
        record = self.record()
        self._validate_identity(record)
        if record.checkpoint_id != checkpoint.checkpoint_id:
            raise ValueError("lifecycle record points to a stale checkpoint")
        actual = self._hash_critical_files(tuple(checkpoint.critical_artifact_hashes))
        if actual != checkpoint.critical_artifact_hashes:
            raise ValueError("checkpoint critical artifact hash mismatch")
        return checkpoint

    def _hash_critical_files(self, relative_paths: tuple[str, ...]) -> dict[str, str]:
        result: dict[str, str] = {}
        for relative in sorted(set(relative_paths)):
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("critical artifact path must be run-relative")
            resolved = (self.run_dir / path).resolve(strict=True)
            if not resolved.is_relative_to(self.run_dir) or not resolved.is_file():
                raise ValueError(f"critical artifact is invalid: {relative}")
            result[path.as_posix()] = sha256_bytes(resolved.read_bytes())
        if not result:
            raise ValueError("lifecycle checkpoint requires critical artifacts")
        return result

    def _validate_identity(self, record: RunLifecycleRecord) -> None:
        if record.run_id != self.run_id or record.owner != self.owner:
            raise ValueError("run lifecycle identity mismatch")
        if (
            self.expected_config_hash is not None
            and record.config_hash not in (None, self.expected_config_hash)
        ):
            raise ValueError("run lifecycle config hash mismatch")

    def _write_record(self, record: RunLifecycleRecord) -> None:
        atomic_write(
            self.record_path,
            canonical_json_bytes(record.model_dump(mode="json")),
        )
