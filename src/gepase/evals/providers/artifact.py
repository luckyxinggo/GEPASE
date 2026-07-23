"""Artifact hash and containment checks for delegated evidence."""

from __future__ import annotations

from pathlib import Path

from gepase.evals.errors import PartialArtifact
from gepase.evals.redaction import sensitive_kinds
from gepase.evals.work_items import WorkSubmission
from gepase.store.artifacts import sha256_bytes


class ArtifactProvider:
    def verify(self, project_root: Path, submission: WorkSubmission) -> Path:
        if not submission.artifact_root:
            raise PartialArtifact("submission has no artifact_root")
        root = (project_root / submission.artifact_root).resolve()
        if not root.is_relative_to(project_root.resolve()) or not root.is_dir():
            raise PartialArtifact("artifact_root is missing or escapes project root")
        if not submission.artifacts:
            raise PartialArtifact("submission contains no artifacts")
        for reference in submission.artifacts:
            path = (root / reference.path).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise PartialArtifact(f"missing artifact: {reference.path}")
            content = path.read_bytes()
            if len(content) != reference.size_bytes or sha256_bytes(content) != reference.sha256:
                raise PartialArtifact(f"artifact hash mismatch: {reference.path}")
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = ""
            if text and sensitive_kinds(text):
                raise PartialArtifact(f"sensitive content in artifact: {reference.path}")
        return root
