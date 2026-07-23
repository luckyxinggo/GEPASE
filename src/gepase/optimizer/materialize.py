"""Path-safe package candidate materialization with reproducible manifests."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from pydantic import Field

from gepase.optimizer.candidate import PackageCandidate
from gepase.package.loader import load_package
from gepase.schemas.common import FrozenModel
from gepase.store.artifacts import atomic_write, canonical_json_bytes


class MaterializedFile(FrozenModel):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mode: int
    modified: bool


class MaterializationManifest(FrozenModel):
    schema_version: str = "1.0.0"
    candidate_id: str
    candidate_content_hash: str
    source_snapshot_hash: str
    materialized_snapshot_hash: str
    source_package_ref: str
    destination_ref: str
    file_set_equal: bool
    source_file_set_equal: bool = True
    content_hash_equal: bool
    permission_policy_equal: bool
    files: tuple[MaterializedFile, ...]


def _validate_destination(project_root: Path, destination: Path) -> Path:
    root = project_root.resolve()
    resolved = destination.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("materialization destination must be inside project root")
    if resolved == root:
        raise ValueError("materialization destination cannot be project root")
    if resolved.exists() and any(resolved.iterdir()):
        raise ValueError("materialization destination must not contain files")
    return resolved


def materialize_candidate(
    project_root: Path,
    candidate: PackageCandidate,
    destination: Path,
) -> MaterializationManifest:
    root = project_root.resolve()
    source = (root / candidate.source_package_ref).resolve(strict=True)
    if not source.is_relative_to(root) or not source.is_dir():
        raise ValueError("candidate source package is unavailable")
    source_snapshot = load_package(source)
    if source_snapshot.snapshot_hash != candidate.snapshot_hash:
        raise ValueError("source package drifted from candidate snapshot")
    target = _validate_destination(root, destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        temporary.rmdir()
        shutil.copytree(source, temporary, symlinks=True, copy_function=shutil.copy2)
        candidate_paths = set(candidate.file_map)
        for existing in sorted(
            (path for path in temporary.rglob("*") if path.is_file() or path.is_symlink()),
            reverse=True,
        ):
            relative = existing.relative_to(temporary).as_posix()
            if relative not in candidate_paths:
                existing.unlink()
        for component in candidate.components:
            path = (temporary / component.path).resolve()
            if not path.is_relative_to(temporary.resolve()):
                raise ValueError(f"component path escapes materialization: {component.path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = path.stat().st_mode if path.exists() else candidate.file_map[component.path].mode
            atomic_write(path, component.content.encode())
            os.chmod(path, mode)
        materialized = load_package(temporary)
        source_files = {item.path: item for item in source_snapshot.files}
        output_files = {item.path: item for item in materialized.files}
        candidate_files = candidate.file_map
        file_set_equal = set(output_files) == set(candidate_files)
        source_file_set_equal = set(source_files) == set(candidate_files)
        permission_equal = all(
            (temporary / path).stat().st_mode & 0o7777 == candidate_files[path].mode
            for path in candidate_files
        )
        content_equal = materialized.snapshot_hash == candidate.content_hash and all(
            output_files[path].sha256 == candidate_files[path].sha256 for path in output_files
        )
        if target.exists():
            target.rmdir()
        os.replace(temporary, target)
        files = tuple(
            MaterializedFile(
                path=path,
                sha256=output_files[path].sha256,
                mode=(target / path).stat().st_mode & 0o7777,
                modified=output_files[path].sha256 != source_files[path].sha256,
            )
            for path in sorted(output_files)
        )
        destination_ref = target.relative_to(root).as_posix()
        manifest = MaterializationManifest(
            candidate_id=candidate.candidate_id,
            candidate_content_hash=candidate.content_hash,
            source_snapshot_hash=candidate.snapshot_hash,
            materialized_snapshot_hash=materialized.snapshot_hash,
            source_package_ref=candidate.source_package_ref,
            destination_ref=destination_ref,
            file_set_equal=file_set_equal,
            source_file_set_equal=source_file_set_equal,
            content_hash_equal=content_equal,
            permission_policy_equal=permission_equal,
            files=files,
        )
        atomic_write(
            target.parent / f"{target.name}.materialization.json",
            canonical_json_bytes(manifest.model_dump(mode="json")),
        )
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
