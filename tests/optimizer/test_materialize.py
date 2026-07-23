from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from gepase.optimizer.candidate import build_seed_candidate, derive_candidate
from gepase.optimizer.materialize import materialize_candidate
from gepase.package.loader import load_package


def test_seed_roundtrip_preserves_file_set_hash_and_permissions() -> None:
    root = Path.cwd()
    local = root / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    candidate = build_seed_candidate(
        root,
        "benchmarks/skills/structured-report-builder",
        run_id="materialize-test",
    )
    with tempfile.TemporaryDirectory(prefix="materialize-test-", dir=local) as temporary:
        destination = Path(temporary) / "package"
        manifest = materialize_candidate(root, candidate, destination)

        assert manifest.file_set_equal is True
        assert manifest.content_hash_equal is True
        assert manifest.permission_policy_equal is True
        assert load_package(destination).snapshot_hash == candidate.content_hash


def test_derived_candidate_materializes_cross_component_changes() -> None:
    root = Path.cwd()
    local = root / "artifacts/local"
    candidate = build_seed_candidate(
        root,
        "benchmarks/skills/structured-report-builder",
        run_id="materialize-test",
    )
    instruction, reference = candidate.components[:2]
    child = derive_candidate(
        candidate,
        {
            instruction.component_id: instruction.content + "\nCross-component instruction.\n",
            reference.component_id: reference.content + "\nCross-component contract.\n",
        },
        operator="reflective_mutation",
        run_id="materialize-test",
    )
    with tempfile.TemporaryDirectory(prefix="materialize-derived-", dir=local) as temporary:
        destination = Path(temporary) / "package"
        manifest = materialize_candidate(root, child, destination)

        assert manifest.content_hash_equal is True
        assert sum(item.modified for item in manifest.files) == 2
        assert "Cross-component instruction" in (destination / "SKILL.md").read_text()
        assert "Cross-component contract" in (
            destination / "references/report-contract.md"
        ).read_text()


def test_materialization_rejects_destination_escape_and_source_drift() -> None:
    root = Path.cwd()
    candidate = build_seed_candidate(
        root,
        "benchmarks/skills/structured-report-builder",
        run_id="materialize-test",
    )
    with tempfile.TemporaryDirectory(prefix="outside-project-") as temporary:
        with pytest.raises(ValueError, match="inside project"):
            materialize_candidate(root, candidate, Path(temporary) / "package")

    drifted = candidate.model_copy(update={"snapshot_hash": "0" * 64})
    with pytest.raises(ValueError, match="drifted"):
        materialize_candidate(root, drifted, root / "artifacts/local/drifted/package")
