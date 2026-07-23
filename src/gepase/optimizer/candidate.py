"""Immutable package candidate, content identity, and lineage construction."""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from pydantic import Field, model_validator

from gepase.optimizer.components import PackageComponent, register_components
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.ir import FileKind
from gepase.schemas.common import FrozenModel

OPTIMIZER_SCHEMA_VERSION = "1.0.0"


class CandidateFile(FrozenModel):
    path: str
    kind: FileKind
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binary: bool
    mutable: bool
    mode: int = Field(ge=0, le=0o7777)

    @model_validator(mode="after")
    def safe_path(self) -> CandidateFile:
        path = Path(self.path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("candidate file path must be package-relative")
        return self


def package_content_hash(files: tuple[CandidateFile, ...]) -> str:
    """Use the exact S3 PackageSnapshot inventory hash algorithm."""

    inventory = [
        {
            "path": item.path,
            "sha256": item.sha256,
            "kind": item.kind.value,
            "binary": item.binary,
        }
        for item in sorted(files, key=lambda value: value.path)
    ]
    return hashlib.sha256(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class PackageCandidate(FrozenModel):
    schema_version: str = OPTIMIZER_SCHEMA_VERSION
    candidate_id: str
    package_id: str
    source_package_ref: str
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    components: tuple[PackageComponent, ...]
    files: tuple[CandidateFile, ...]
    parent_ids: tuple[str, ...] = ()
    operator: str
    generation: int = Field(ge=0)
    created_from_run: str

    @model_validator(mode="after")
    def candidate_invariants(self) -> PackageCandidate:
        source = Path(self.source_package_ref)
        if source.is_absolute() or ".." in source.parts:
            raise ValueError("source_package_ref must be repository-relative")
        component_ids = [item.component_id for item in self.components]
        paths = [item.path for item in self.components]
        file_paths = {item.path for item in self.files}
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("candidate contains duplicate component_id")
        if len(paths) != len(set(paths)):
            raise ValueError("candidate components overlap the same file")
        if not set(paths) <= file_paths:
            raise ValueError("candidate component has no file inventory entry")
        if len(file_paths) != len(self.files):
            raise ValueError("candidate file inventory contains duplicate paths")
        if package_content_hash(self.files) != self.content_hash:
            raise ValueError("candidate content_hash does not match file inventory")
        expected_id = candidate_id_for(
            self.content_hash,
            self.parent_ids,
            self.operator,
            self.generation,
            self.created_from_run,
        )
        if self.candidate_id != expected_id:
            raise ValueError("candidate_id does not match immutable identity")
        if self.generation == 0 and self.parent_ids:
            raise ValueError("seed candidate cannot have parents")
        if self.generation > 0 and not self.parent_ids:
            raise ValueError("derived candidate requires parent lineage")
        return self

    @property
    def component_map(self) -> Mapping[str, PackageComponent]:
        return MappingProxyType({item.component_id: item for item in self.components})

    @property
    def file_map(self) -> Mapping[str, CandidateFile]:
        return MappingProxyType({item.path: item for item in self.files})


def candidate_id_for(
    content_hash: str,
    parent_ids: tuple[str, ...],
    operator: str,
    generation: int,
    created_from_run: str,
) -> str:
    payload = json.dumps(
        {
            "content_hash": content_hash,
            "parents": list(parent_ids),
            "operator": operator,
            "generation": generation,
            "run": created_from_run,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"candidate-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


def build_seed_candidate(
    project_root: Path,
    package_ref: str,
    *,
    run_id: str,
) -> PackageCandidate:
    root = project_root.resolve()
    relative = Path(package_ref)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("package_ref must be repository-relative")
    package_root = (root / relative).resolve(strict=True)
    if not package_root.is_relative_to(root):
        raise ValueError("package_ref escapes project root")
    analysis = PackageAnalyzer().analyze(package_root)
    components = register_components(package_root, analysis.package_ir)
    files = tuple(
        CandidateFile(
            path=item.path,
            kind=item.kind,
            sha256=item.sha256,
            binary=item.binary,
            mutable=item.mutable,
            mode=stat.S_IMODE((package_root / item.path).stat().st_mode),
        )
        for item in analysis.snapshot.files
    )
    content_hash = package_content_hash(files)
    return PackageCandidate(
        candidate_id=candidate_id_for(content_hash, (), "seed", 0, run_id),
        package_id=analysis.snapshot.package_id,
        source_package_ref=relative.as_posix(),
        snapshot_hash=analysis.snapshot.snapshot_hash,
        content_hash=content_hash,
        components=components,
        files=files,
        operator="seed",
        generation=0,
        created_from_run=run_id,
    )


def derive_candidate(
    parent: PackageCandidate,
    replacements: Mapping[str, str],
    *,
    operator: str,
    run_id: str,
) -> PackageCandidate:
    if not replacements:
        raise ValueError("candidate derivation requires at least one replacement")
    unknown = set(replacements) - set(parent.component_map)
    if unknown:
        raise ValueError(f"unknown component replacements: {sorted(unknown)}")
    components: list[PackageComponent] = []
    changed_paths: dict[str, str] = {}
    for component in parent.components:
        content = replacements.get(component.component_id, component.content)
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"replacement is empty: {component.component_id}")
        if content != component.content:
            changed_paths[component.path] = hashlib.sha256(content.encode()).hexdigest()
        components.append(
            component.model_copy(
                update={
                    "content": content,
                    "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                }
            )
        )
    if not changed_paths:
        raise ValueError("candidate derivation produced a no-op")
    files = tuple(
        item.model_copy(update={"sha256": changed_paths.get(item.path, item.sha256)})
        for item in parent.files
    )
    content_hash = package_content_hash(files)
    parents = (parent.candidate_id,)
    generation = parent.generation + 1
    return PackageCandidate(
        candidate_id=candidate_id_for(content_hash, parents, operator, generation, run_id),
        package_id=parent.package_id,
        source_package_ref=parent.source_package_ref,
        snapshot_hash=parent.snapshot_hash,
        content_hash=content_hash,
        components=tuple(components),
        files=files,
        parent_ids=parents,
        operator=operator,
        generation=generation,
        created_from_run=run_id,
    )


def build_candidate_from_package(
    project_root: Path,
    parent: PackageCandidate,
    package_root: Path,
    *,
    operator: str,
    run_id: str,
    parent_ids: tuple[str, ...] | None = None,
    generation: int | None = None,
) -> PackageCandidate:
    """Build a derived candidate after a typed patch changed content or topology."""

    root = project_root.resolve()
    resolved = package_root.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_dir():
        raise ValueError("derived package root must be inside the project workspace")
    analysis = PackageAnalyzer().analyze(resolved)
    if analysis.snapshot.package_id != parent.package_id:
        raise ValueError("typed patch changed package identity")
    components = register_components(resolved, analysis.package_ir)
    files = tuple(
        CandidateFile(
            path=item.path,
            kind=item.kind,
            sha256=item.sha256,
            binary=item.binary,
            mutable=item.mutable,
            mode=stat.S_IMODE((resolved / item.path).stat().st_mode),
        )
        for item in analysis.snapshot.files
    )
    content_hash = package_content_hash(files)
    if content_hash == parent.content_hash:
        raise ValueError("typed patch produced a no-op package")
    parent_binary = {item.path: item.sha256 for item in parent.files if item.binary}
    child_binary = {item.path: item.sha256 for item in files if item.binary}
    if child_binary != parent_binary:
        raise ValueError("typed patch cannot add, remove, or modify binary files")
    parents = parent_ids or (parent.candidate_id,)
    if len(parents) != len(set(parents)):
        raise ValueError("derived candidate parent_ids must be unique")
    resolved_generation = generation if generation is not None else parent.generation + 1
    if resolved_generation <= parent.generation:
        raise ValueError("derived candidate generation must exceed its materialized base")
    return PackageCandidate(
        candidate_id=candidate_id_for(
            content_hash,
            parents,
            operator,
            resolved_generation,
            run_id,
        ),
        package_id=parent.package_id,
        source_package_ref=parent.source_package_ref,
        snapshot_hash=parent.snapshot_hash,
        content_hash=content_hash,
        components=components,
        files=files,
        parent_ids=parents,
        operator=operator,
        generation=resolved_generation,
        created_from_run=run_id,
    )
