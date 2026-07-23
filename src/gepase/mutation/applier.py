"""Atomic PackagePatch validation, application, reparse, and candidate construction."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from gepase.mutation.impact import assess_impact
from gepase.mutation.schema import (
    AddFile,
    DeleteFile,
    FileChange,
    InsertReference,
    PackagePatch,
    PatchApplication,
    PatchApplicationStatus,
    ReplaceMarkdownBlock,
    ReplacePythonFunction,
    ReplaceTextFile,
    RollbackRecord,
    UpdateFrontmatter,
    application_id_for,
)
from gepase.optimizer.candidate import (
    PackageCandidate,
    build_candidate_from_package,
)
from gepase.optimizer.materialize import materialize_candidate
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.diff import graph_diff
from gepase.package.ir import IRNode, NodeKind, PackageGraph
from gepase.package.loader import load_package
from gepase.store.artifacts import atomic_write, canonical_json_bytes


class PatchApplyError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _target(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise PatchApplyError("path_escape", f"patch path escapes package: {relative}")
    return path


def _node_for(graph: PackageGraph, node_id: str, path: str) -> IRNode:
    try:
        node = next(item for item in graph.nodes if item.node_id == node_id)
    except StopIteration as error:
        raise PatchApplyError("unknown_target", f"unknown target node: {node_id}") from error
    if node.path != path:
        raise PatchApplyError("target_path_mismatch", f"target node does not belong to {path}")
    return node


def _verify_precondition(node: IRNode, expected: str) -> None:
    if node.content_hash != expected:
        raise PatchApplyError(
            "stale_precondition",
            f"node {node.node_id} hash is {node.content_hash}, expected {expected}",
        )


def _replace_span(path: Path, node: IRNode, replacement: str) -> None:
    if node.span is None:
        raise PatchApplyError("missing_source_span", f"target has no bounded span: {node.node_id}")
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    start = node.span.start_line - 1
    end = node.span.end_line
    if start < 0 or end > len(lines):
        raise PatchApplyError("invalid_source_span", f"source span is outside {node.path}")
    suffix = "\n" if lines[start:end] and lines[end - 1].endswith("\n") else ""
    normalized = replacement.rstrip("\n") + suffix
    lines[start:end] = [normalized]
    path.write_text("".join(lines), encoding="utf-8")


def _validate_operations(graph: PackageGraph, package_root: Path, patch: PackagePatch) -> None:
    target_ids: set[str] = set()
    paths_with_addition: set[str] = set()
    for operation in patch.operations:
        path = _target(package_root, operation.path)
        target_id = getattr(operation, "target_node_id", None)
        if target_id is not None:
            if target_id in target_ids:
                raise PatchApplyError("duplicate_target", f"multiple operations target {target_id}")
            target_ids.add(target_id)
        if isinstance(operation, (AddFile, InsertReference)):
            if path.exists():
                raise PatchApplyError(
                    "stale_precondition", f"add target already exists: {operation.path}"
                )
            paths_with_addition.add(operation.path)
            continue
        if not path.is_file():
            raise PatchApplyError("missing_target", f"patch target is missing: {operation.path}")
        assert target_id is not None
        node = _node_for(graph, target_id, operation.path)
        _verify_precondition(node, operation.precondition_hash)
        if isinstance(operation, ReplaceMarkdownBlock) and node.kind not in {
            NodeKind.SECTION,
            NodeKind.INSTRUCTION,
            NodeKind.REFERENCE_CHUNK,
        }:
            raise PatchApplyError("wrong_target_kind", "markdown replacement requires a block node")
        if isinstance(operation, UpdateFrontmatter) and node.kind is not NodeKind.FRONTMATTER:
            raise PatchApplyError(
                "wrong_target_kind", "frontmatter update requires frontmatter node"
            )
        if isinstance(operation, ReplacePythonFunction) and node.kind is not NodeKind.FUNCTION:
            raise PatchApplyError("wrong_target_kind", "python replacement requires function node")
        if isinstance(operation, ReplaceTextFile) and node.kind is not NodeKind.FILE:
            raise PatchApplyError("wrong_target_kind", "text-file replacement requires a file node")
        if isinstance(operation, DeleteFile):
            if node.kind is not NodeKind.FILE:
                raise PatchApplyError("wrong_target_kind", "delete_file requires a file node")
            orphan_ids = {
                node_id
                for diagnostic in graph.diagnostics
                if diagnostic.kind == "orphan_node"
                for node_id in diagnostic.related_node_ids
            }
            if node.node_id not in orphan_ids:
                raise PatchApplyError(
                    "not_proven_orphan", "delete_file target lacks orphan evidence"
                )
            if not operation.orphan_evidence_ref:
                raise PatchApplyError(
                    "missing_orphan_evidence", "delete_file lacks orphan evidence"
                )
    non_addition_paths = {
        item.path for item in patch.operations if item.path not in paths_with_addition
    }
    if paths_with_addition & non_addition_paths:
        raise PatchApplyError("conflicting_path_operations", "new file path has another operation")


def _apply_operations(
    graph: PackageGraph,
    package_root: Path,
    patch: PackagePatch,
    *,
    fail_after_operations: int | None,
) -> None:
    replacements: list[
        tuple[
            int,
            ReplaceMarkdownBlock | UpdateFrontmatter | ReplacePythonFunction,
            IRNode,
        ]
    ] = []
    other: list[AddFile | InsertReference | DeleteFile | ReplaceTextFile] = []
    for operation in patch.operations:
        if isinstance(operation, (ReplaceMarkdownBlock, UpdateFrontmatter, ReplacePythonFunction)):
            node = _node_for(graph, operation.target_node_id, operation.path)
            assert node.span is not None
            replacements.append((node.span.start_line, operation, node))
        else:
            other.append(operation)
    applied = 0
    for _, operation, node in sorted(
        replacements, key=lambda item: (item[1].path, -item[0], item[1].operation_id)
    ):
        _replace_span(_target(package_root, operation.path), node, operation.replacement)
        applied += 1
        if fail_after_operations is not None and applied >= fail_after_operations:
            raise PatchApplyError("injected_failure", "fault injected after bounded operation")
    for operation in other:
        path = _target(package_root, operation.path)
        if isinstance(operation, (AddFile, InsertReference)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(operation.content, encoding="utf-8")
            if isinstance(operation, AddFile) and operation.executable:
                os.chmod(path, 0o755)
        elif isinstance(operation, DeleteFile):
            path.unlink()
        elif isinstance(operation, ReplaceTextFile):
            path.write_text(operation.replacement, encoding="utf-8")
        applied += 1
        if fail_after_operations is not None and applied >= fail_after_operations:
            raise PatchApplyError("injected_failure", "fault injected after bounded operation")


def _file_changes(before_root: Path, after_root: Path) -> tuple[FileChange, ...]:
    before = {item.path: item for item in load_package(before_root).files}
    after = {item.path: item for item in load_package(after_root).files}
    rows: list[FileChange] = []
    for path in sorted(set(before) | set(after)):
        if path not in before:
            rows.append(FileChange(path=path, change="added", after_hash=after[path].sha256))
        elif path not in after:
            rows.append(FileChange(path=path, change="deleted", before_hash=before[path].sha256))
        elif before[path].sha256 != after[path].sha256:
            rows.append(
                FileChange(
                    path=path,
                    change="modified",
                    before_hash=before[path].sha256,
                    after_hash=after[path].sha256,
                )
            )
    return tuple(rows)


def apply_package_patch(
    project_root: Path,
    parent: PackageCandidate,
    patch: PackagePatch,
    workspace_root: Path,
    *,
    run_id: str,
    fail_after_operations: int | None = None,
    candidate_parent_ids: tuple[str, ...] | None = None,
    candidate_generation: int | None = None,
    candidate_operator: str = "graph_guided_package_patch",
) -> tuple[PatchApplication, PackageCandidate | None]:
    root = project_root.resolve()
    workspace = workspace_root.resolve()
    if not workspace.is_relative_to(root) or workspace == root:
        raise ValueError("patch workspace must be inside the project root")
    workspace.mkdir(parents=True, exist_ok=True)
    application_id = application_id_for(patch.patch_id, parent.candidate_id)
    source_path = (root / parent.source_package_ref).resolve(strict=True)
    source_hash_before = load_package(source_path).snapshot_hash
    if (
        patch.base_candidate_id != parent.candidate_id
        or patch.base_content_hash != parent.content_hash
    ):
        application = PatchApplication(
            application_id=application_id,
            patch_id=patch.patch_id,
            parent_candidate_id=parent.candidate_id,
            parent_content_hash=parent.content_hash,
            status=PatchApplicationStatus.STALE_PARENT,
            error_code="stale_parent",
            error_detail="patch base candidate/content hash does not match parent",
        )
        return application, None
    if patch.base_snapshot_hash != parent.snapshot_hash:
        application = PatchApplication(
            application_id=application_id,
            patch_id=patch.patch_id,
            parent_candidate_id=parent.candidate_id,
            parent_content_hash=parent.content_hash,
            status=PatchApplicationStatus.STALE_PARENT,
            error_code="stale_parent",
            error_detail="patch source snapshot hash does not match parent",
        )
        return application, None
    temporary = Path(tempfile.mkdtemp(prefix=f".{application_id}.", dir=workspace))
    final_root = workspace / "applications" / application_id
    try:
        source_root_name = Path(parent.source_package_ref).name
        parent_root = temporary / "baseline" / source_root_name
        materialize_candidate(root, parent, parent_root)
        working = temporary / "candidate" / source_root_name
        working.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(parent_root, working, copy_function=shutil.copy2)
        before_analysis = PackageAnalyzer().analyze(parent_root)
        _validate_operations(before_analysis.graph, working, patch)
        _apply_operations(
            before_analysis.graph,
            working,
            patch,
            fail_after_operations=fail_after_operations,
        )
        after_analysis = PackageAnalyzer().analyze(working)
        child = build_candidate_from_package(
            root,
            parent,
            working,
            operator=candidate_operator,
            run_id=run_id,
            parent_ids=candidate_parent_ids,
            generation=candidate_generation,
        )
        difference = graph_diff(before_analysis.graph, after_analysis.graph)
        impact = assess_impact(before_analysis.graph, difference, patch)
        changes = _file_changes(parent_root, working)
        if final_root.exists():
            raise PatchApplyError(
                "application_exists", f"application already exists: {application_id}"
            )
        final_root.parent.mkdir(parents=True, exist_ok=True)
        os.replace(working, final_root)
        final_ref = final_root.relative_to(root).as_posix()
        application = PatchApplication(
            application_id=application_id,
            patch_id=patch.patch_id,
            parent_candidate_id=parent.candidate_id,
            parent_content_hash=parent.content_hash,
            status=PatchApplicationStatus.APPLIED,
            candidate_id=child.candidate_id,
            candidate_content_hash=child.content_hash,
            workspace_ref=final_ref,
            file_changes=changes,
            graph_diff=difference,
            post_apply_diagnostics=tuple(
                item.model_dump(mode="json") for item in after_analysis.graph.diagnostics
            ),
            affected_node_ids=impact.affected_node_ids,
            validation_intensity=impact.validation_intensity.value,
            original_workspace_hash_unchanged=(
                load_package(source_path).snapshot_hash
                == source_hash_before
                == parent.snapshot_hash
            ),
        )
        atomic_write(
            final_root.parent / f"{application_id}.json",
            canonical_json_bytes(application.model_dump(mode="json")),
        )
        atomic_write(
            final_root.parent / f"{application_id}.candidate.json",
            canonical_json_bytes(child.model_dump(mode="json")),
        )
        return application, child
    except PatchApplyError as error:
        application = PatchApplication(
            application_id=application_id,
            patch_id=patch.patch_id,
            parent_candidate_id=parent.candidate_id,
            parent_content_hash=parent.content_hash,
            status=(
                PatchApplicationStatus.STALE_PARENT
                if error.code in {"stale_precondition", "stale_parent"}
                else PatchApplicationStatus.INVALID
            ),
            error_code=error.code,
            error_detail=error.detail,
            original_workspace_hash_unchanged=(
                load_package(source_path).snapshot_hash == source_hash_before
            ),
        )
        return application, None
    except (OSError, UnicodeError, ValueError) as error:
        application = PatchApplication(
            application_id=application_id,
            patch_id=patch.patch_id,
            parent_candidate_id=parent.candidate_id,
            parent_content_hash=parent.content_hash,
            status=PatchApplicationStatus.INVALID,
            error_code="apply_error",
            error_detail=f"{type(error).__name__}: {error}",
            original_workspace_hash_unchanged=(
                load_package(source_path).snapshot_hash == source_hash_before
            ),
        )
        return application, None
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def rollback_application(
    project_root: Path,
    parent: PackageCandidate,
    application: PatchApplication,
) -> PatchApplication:
    if application.status is not PatchApplicationStatus.APPLIED or not application.workspace_ref:
        raise ValueError("only an applied patch can be rolled back")
    root = project_root.resolve()
    workspace = (root / application.workspace_ref).resolve(strict=True)
    if not workspace.is_relative_to(root):
        raise ValueError("rollback workspace escapes project root")
    shutil.rmtree(workspace)
    restored = load_package((root / parent.source_package_ref).resolve(strict=True))
    verified = restored.snapshot_hash == parent.snapshot_hash
    record = RollbackRecord(
        parent_candidate_id=parent.candidate_id,
        parent_content_hash=parent.content_hash,
        restored_content_hash=parent.content_hash if verified else restored.snapshot_hash,
        verified=verified,
        removed_workspace=application.workspace_ref,
    )
    return application.model_copy(
        update={
            "status": PatchApplicationStatus.ROLLED_BACK,
            "workspace_ref": None,
            "rollback": record,
        }
    )
