"""Small shared helpers for reproducible stage Gate scripts."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from gepase.store.artifacts import ArtifactStore


def load_json_object(path: Path, *, root: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path.relative_to(root)}")
    return value


def tree_hash(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    if path.is_file():
        rows = [path]
        base = path.parent
    elif path.exists():
        rows = sorted(item for item in path.rglob("*") if item.is_file())
        base = path
    else:
        rows = []
        base = path
    for item in rows:
        digest.update(item.relative_to(base).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(item.read_bytes()).digest())
    return {"sha256": digest.hexdigest(), "files": len(rows), "exists": path.exists()}


def hash_named_paths(paths: Mapping[str, Path]) -> dict[str, object]:
    return {name: tree_hash(path) for name, path in paths.items()}


def protected_tree_hashes(
    root: Path,
    *,
    public_canary_source: Path,
    extra_stage_ids: Sequence[str] = (),
) -> dict[str, object]:
    """Hash the v0.1 roots that graph-hardening stages must never rewrite."""

    r5 = root / "artifacts/runs/r5-slack-gif-creator-report"
    paths = {
        "r2_run": root / "artifacts/runs/r2-slack-gif-creator-evalplan",
        "r3_run": root / "artifacts/runs/r3-slack-gif-creator-paired",
        "r4_run": root / "artifacts/runs/r4-slack-gif-creator-evolution",
        "r5_run": r5,
        "r2_stage": root / "artifacts/stages/R2",
        "r3_stage": root / "artifacts/stages/R3",
        "r4_stage": root / "artifacts/stages/R4",
        "r5_stage": root / "artifacts/stages/R5",
        "s10_stage": root / "artifacts/stages/S10",
        "public_canary_source": public_canary_source,
        "deployable_package": r5 / "deployable/package",
        "skills_test": root / "skills_test",
    }
    paths.update(
        {
            stage_id.lower().replace("-", "_") + "_stage": root
            / "artifacts/stages"
            / stage_id
            for stage_id in extra_stage_ids
        }
    )
    return hash_named_paths(paths)


def run_command(
    command: Sequence[str],
    *,
    root: Path,
    commands: list[str],
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    rendered = " ".join(command)
    commands.append(rendered)
    process_environment = dict(os.environ)
    if environment is not None:
        process_environment.update(environment)
    result = subprocess.run(
        tuple(command),
        cwd=root,
        env=process_environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    lines = result.stdout.strip().splitlines()
    return {
        "command": rendered,
        "exit_code": result.returncode,
        "ok": result.returncode == 0,
        "summary": lines[-1] if lines else "no output",
    }


def git_value(root: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args),
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    ).stdout.strip()


def verify_artifact_stores(
    paths: Mapping[str, Path],
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Verify named stores without modifying their indexes or evidence."""

    results = {name: ArtifactStore(path).verify().as_dict() for name, path in paths.items()}
    valid = all(
        item["valid"] is True
        and item["unindexed_files"] == 0
        and item["missing"] == 0
        and item["hash_mismatch"] == 0
        for item in results.values()
    )
    return results, valid


def verify_machine_gate_set(
    path: Path,
    *,
    root: Path,
    expected_gate_ids: Sequence[str],
) -> tuple[dict[str, Any], bool]:
    """Check one canonical machine-Gate projection and its exact Gate set."""

    payload = load_json_object(path, root=root)
    rows = payload.get("gates", [])
    observed = [str(item.get("gate_id")) for item in rows if isinstance(item, dict)]
    valid = (
        payload.get("status") == "passed"
        and payload.get("passed") == payload.get("total") == len(expected_gate_ids)
        and observed == list(expected_gate_ids)
        and all(item.get("status") == "passed" for item in rows)
    )
    return payload, valid


def verify_valid_json_refs(
    stage: Path,
    required: Mapping[str, Sequence[str]],
    *,
    root: Path,
) -> tuple[list[dict[str, Any]], bool]:
    """Check that every Gate's canonical JSON evidence exists and says valid."""

    results: list[dict[str, Any]] = []
    for gate_id, references in required.items():
        missing: list[str] = []
        invalid: list[str] = []
        for reference in references:
            path = stage / reference
            if not path.is_file():
                missing.append(reference)
                continue
            if load_json_object(path, root=root).get("valid") is not True:
                invalid.append(reference)
        passed = not missing and not invalid
        results.append(
            {
                "gate_id": gate_id,
                "passed": passed,
                "missing_refs": missing,
                "invalid_refs": invalid,
                "consumed_refs": [
                    (stage / reference).relative_to(root).as_posix()
                    for reference in references
                    if (stage / reference).is_file()
                ],
            }
        )
    return results, all(item["passed"] for item in results)
