"""Content-addressed, atomic artifact storage and verification."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gepase.schemas.common import ArtifactRef

INDEX_NAME = "artifact-index.json"
CANDIDATE_BUNDLE_SEAL_NAME = "candidate-bundle-seal.json"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


@dataclass(frozen=True)
class VerificationResult:
    missing: int
    hash_mismatch: int
    schema_errors: int
    checked: int
    unindexed_files: int

    @property
    def valid(self) -> bool:
        return self.missing == self.hash_mismatch == self.schema_errors == 0

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "valid": self.valid,
            "missing": self.missing,
            "hash_mismatch": self.hash_mismatch,
            "schema_errors": self.schema_errors,
            "checked": self.checked,
            "unindexed_files": self.unindexed_files,
        }


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._refs: dict[str, ArtifactRef] = {}
        index = self.root / INDEX_NAME
        if index.exists():
            raw = json.loads(index.read_text(encoding="utf-8"))
            self._refs = {
                item["path"]: ArtifactRef.model_validate(item) for item in raw.get("artifacts", [])
            }

    def write_json(self, relative_path: str, value: Any) -> ArtifactRef:
        return self.write_bytes(relative_path, canonical_json_bytes(value), "application/json")

    def write_json_append_only(self, relative_path: str, value: Any) -> ArtifactRef:
        """Write a new JSON artifact without permitting historical replacement.

        Stage progress projections legitimately extend an existing store, but a
        recovery audit must never replace evidence from an earlier execution.
        Content-identical retries are idempotent; a different payload at the
        same path fails closed.
        """

        return self.write_bytes_append_only(
            relative_path,
            canonical_json_bytes(value),
            "application/json",
        )

    def write_text(
        self,
        relative_path: str,
        text: str,
        media_type: str = "text/plain",
    ) -> ArtifactRef:
        return self.write_bytes(relative_path, text.encode(), media_type)

    def write_bytes(self, relative_path: str, data: bytes, media_type: str) -> ArtifactRef:
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("artifact path escapes store root")
        atomic_write(path, data)
        ref = ArtifactRef(
            path=relative_path,
            sha256=sha256_bytes(data),
            media_type=media_type,
            size_bytes=len(data),
        )
        self._refs[relative_path] = ref
        self._write_index()
        return ref

    def write_bytes_append_only(
        self,
        relative_path: str,
        data: bytes,
        media_type: str,
    ) -> ArtifactRef:
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("artifact path escapes store root")
        if path.exists():
            if not path.is_file() or path.read_bytes() != data:
                raise FileExistsError(
                    f"append-only artifact already exists with different content: {relative_path}"
                )
            existing = self._refs.get(relative_path)
            expected = ArtifactRef(
                path=relative_path,
                sha256=sha256_bytes(data),
                media_type=media_type,
                size_bytes=len(data),
            )
            if existing is not None and existing != expected:
                raise ValueError("append-only artifact index disagrees with existing content")
            if existing is None:
                self._refs[relative_path] = expected
                self._write_index()
            return expected
        return self.write_bytes(relative_path, data, media_type)

    def index_existing(
        self,
        relative_path: str,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        """Add an already-written file to the immutable artifact inventory.

        Agent hosts and SQLite create files outside ``ArtifactStore``. Rewriting those
        files merely to index them would be unsafe (and can replace an open database
        inode), so sealing computes their content references in place.
        """
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("artifact path escapes store root")
        if not path.is_file():
            raise FileNotFoundError(path)
        data = path.read_bytes()
        ref = ArtifactRef(
            path=relative_path,
            sha256=sha256_bytes(data),
            media_type=media_type,
            size_bytes=len(data),
        )
        self._refs[relative_path] = ref
        self._write_index()
        return ref

    def index_existing_append_only(
        self,
        relative_path: str,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        """Index an immutable existing artifact without permitting hash replacement."""

        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("artifact path escapes store root")
        if not path.is_file():
            raise FileNotFoundError(path)
        data = path.read_bytes()
        expected = ArtifactRef(
            path=relative_path,
            sha256=sha256_bytes(data),
            media_type=media_type,
            size_bytes=len(data),
        )
        existing = self._refs.get(relative_path)
        if existing is not None:
            if existing != expected:
                raise ValueError(
                    f"append-only artifact index disagrees with existing content: {relative_path}"
                )
            return existing
        self._refs[relative_path] = expected
        self._write_index()
        return expected

    def verify_complete(self) -> VerificationResult:
        """Require a fully indexed, hash-matched artifact scope."""

        result = self.verify()
        if not result.valid or result.unindexed_files != 0:
            raise ValueError(
                "artifact store verification failed: "
                f"missing={result.missing}, hash_mismatch={result.hash_mismatch}, "
                f"schema_errors={result.schema_errors}, "
                f"unindexed_files={result.unindexed_files}"
            )
        return result

    def prune_missing(self) -> int:
        """Remove stale index entries after an explicitly curated artifact cleanup."""
        missing = [relative for relative in self._refs if not (self.root / relative).is_file()]
        for relative in missing:
            del self._refs[relative]
        if missing:
            self._write_index()
        return len(missing)

    def append_event(self, relative_path: str, value: Any) -> None:
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("event path escapes store root")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")

    def _write_index(self) -> None:
        references = [ref.model_dump() for ref in self._refs.values()]
        data = canonical_json_bytes({"schema_version": "1.0.0", "artifacts": references})
        atomic_write(self.root / INDEX_NAME, data)

    def verify(self) -> VerificationResult:
        missing = 0
        mismatch = 0
        schema_errors = 0
        checked = 0
        for ref in self._refs.values():
            path = (self.root / ref.path).resolve()
            if not path.is_relative_to(self.root):
                schema_errors += 1
                continue
            if not path.exists():
                missing += 1
                continue
            checked += 1
            data = path.read_bytes()
            if sha256_bytes(data) != ref.sha256 or len(data) != ref.size_bytes:
                mismatch += 1
        indexed = set(self._refs) | {INDEX_NAME}
        existing = {
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        return VerificationResult(
            missing=missing,
            hash_mismatch=mismatch,
            schema_errors=schema_errors,
            checked=checked,
            unindexed_files=len(existing - indexed),
        )


def resolve_scoped_artifact_index(run_dir: Path) -> tuple[Path, dict[str, str]]:
    """Resolve a complete local or ancestor seal for one nested artifact scope."""

    run = run_dir.resolve()
    for owner in (run, *run.parents):
        index_path = owner / INDEX_NAME
        if not index_path.is_file():
            continue
        verification = ArtifactStore(owner).verify()
        if not verification.valid or verification.unindexed_files:
            continue
        raw = json.loads(index_path.read_text(encoding="utf-8"))
        hashes = {str(item["path"]): str(item["sha256"]) for item in raw["artifacts"]}
        if owner == run:
            scoped = hashes
        else:
            prefix = f"{run.relative_to(owner).as_posix()}/"
            scoped = {
                path[len(prefix) :]: digest
                for path, digest in hashes.items()
                if path.startswith(prefix)
            }
        if scoped.get("run-metadata.json"):
            return index_path, scoped
    raise ValueError("artifact scope has no complete local or ancestor seal")


def resolve_verified_artifact(artifact_path: Path) -> tuple[Path, str]:
    """Resolve one artifact through a complete local or ancestor ArtifactStore."""

    artifact = artifact_path.resolve(strict=True)
    digest = sha256_bytes(artifact.read_bytes())
    for owner in artifact.parents:
        index_path = owner / INDEX_NAME
        if not index_path.is_file():
            continue
        try:
            ArtifactStore(owner).verify_complete()
        except (ValueError, json.JSONDecodeError):
            continue
        raw = json.loads(index_path.read_text(encoding="utf-8"))
        artifacts = raw.get("artifacts")
        if not isinstance(artifacts, list):
            continue
        relative = artifact.relative_to(owner).as_posix()
        refs = [
            item
            for item in artifacts
            if isinstance(item, dict) and item.get("path") == relative
        ]
        if len(refs) == 1 and refs[0].get("sha256") == digest:
            return owner, relative
    raise ValueError("artifact is not covered by a complete hash-matched ArtifactStore")


def verify_candidate_bundle_artifact(
    artifact_path: Path,
    *,
    expected_candidate_id: str,
    expected_package_id: str,
    expected_content_hash: str,
) -> tuple[Path, str, dict[str, Any]]:
    """Verify an artifact against one immutable Candidate-level bundle seal."""

    artifact = artifact_path.resolve(strict=True)
    owner = artifact.parent
    relative = artifact.relative_to(owner).as_posix()
    binding_path = owner / CANDIDATE_BUNDLE_SEAL_NAME
    if not binding_path.is_file() or not (owner / INDEX_NAME).is_file():
        raise ValueError("Candidate bundle seal is missing")
    ArtifactStore(owner).verify_complete()
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    if not isinstance(binding, dict):
        raise ValueError("Candidate bundle seal must be an object")
    if (
        binding.get("schema_version") != "1.0.0"
        or binding.get("binding_kind") != "package_candidate_bundle"
        or binding.get("candidate_id") != expected_candidate_id
        or binding.get("package_id") != expected_package_id
        or binding.get("content_hash") != expected_content_hash
        or binding.get("workspace_snapshot_hash") != expected_content_hash
    ):
        raise ValueError("Candidate bundle seal identity mismatch")
    artifact_hashes = binding.get("artifact_hashes")
    if not isinstance(artifact_hashes, dict):
        raise ValueError("Candidate bundle seal lacks artifact hashes")
    required = {"candidate.json", "application.json", "patch.json", "graph.json"}
    if not required <= set(artifact_hashes):
        raise ValueError("Candidate bundle seal lacks required immutable artifacts")
    digest = sha256_bytes(artifact.read_bytes())
    if artifact_hashes.get(relative) != digest:
        raise ValueError("Candidate bundle artifact hash mismatch")
    raw_index = json.loads((owner / INDEX_NAME).read_text(encoding="utf-8"))
    rows = raw_index.get("artifacts")
    if not isinstance(rows, list):
        raise ValueError("Candidate bundle artifact index is malformed")
    matches = [row for row in rows if isinstance(row, dict) and row.get("path") == relative]
    if len(matches) != 1 or matches[0].get("sha256") != digest:
        raise ValueError("Candidate bundle artifact index binding mismatch")
    return owner, relative, binding
