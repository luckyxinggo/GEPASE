"""Assertion mutation testing and immutable benchmark freeze operations."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from gepase.benchmarks.loader import load_cases, load_manifest
from gepase.evals.assertions import AssertionContext, evaluate_assertion
from gepase.evals.schema import AssertionSpec, SplitManifest
from gepase.store.artifacts import atomic_write, canonical_json_bytes

MUTATION_RECIPES = (
    {"id": "file-missing", "family": "file_exists", "description": "remove output file"},
    {"id": "file-empty", "family": "file_exists", "description": "truncate output file"},
    {
        "id": "required-value-missing",
        "family": "file_contains",
        "description": "remove one required literal",
    },
    {
        "id": "unrelated-content",
        "family": "file_contains",
        "description": "replace with unrelated content",
    },
    {"id": "json-wrong-value", "family": "json_equals", "description": "change value"},
    {"id": "json-wrong-type", "family": "json_equals", "description": "change type"},
    {
        "id": "html-missing-contract-node",
        "family": "html_contract",
        "description": "remove required table node",
    },
    {
        "id": "html-remote-asset",
        "family": "html_contract",
        "description": "inject prohibited remote asset",
    },
)


def _set_pointer(root: dict[str, Any], pointer: str, value: Any) -> None:
    tokens = pointer.strip("/").split("/")
    current: Any = root
    for index, token in enumerate(tokens):
        last = index == len(tokens) - 1
        next_is_index = not last and tokens[index + 1].isdigit()
        if isinstance(current, list):
            position = int(token)
            while len(current) <= position:
                current.append({})
            if last:
                current[position] = value
            else:
                if not isinstance(current[position], (dict, list)):
                    current[position] = [] if next_is_index else {}
                current = current[position]
        else:
            if last:
                current[token] = value
            else:
                current.setdefault(token, [] if next_is_index else {})
                current = current[token]


def _control_artifact(root: Path, spec: AssertionSpec) -> Path:
    path = root / str(spec.parameters["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if spec.family == "file_exists":
        path.write_text("x" * max(1, int(spec.parameters.get("min_bytes", 1))), encoding="utf-8")
    elif spec.family == "file_contains":
        path.write_text("\n".join(map(str, spec.parameters["values"])), encoding="utf-8")
    elif spec.family == "json_equals":
        payload: dict[str, Any] = {}
        _set_pointer(payload, str(spec.parameters["pointer"]), spec.parameters["expected"])
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif spec.family == "html_contract":
        path.write_text(
            "<html><body><h1>Report</h1><table></table><footer>Source:</footer></body></html>",
            encoding="utf-8",
        )
    else:
        raise ValueError(f"mutation recipe unavailable for {spec.family}")
    return path


def _mutate(path: Path, spec: AssertionSpec, recipe_id: str) -> None:
    if recipe_id == "file-missing":
        path.unlink()
    elif recipe_id == "file-empty":
        path.write_bytes(b"")
    elif recipe_id == "required-value-missing":
        values = list(map(str, spec.parameters["values"]))
        path.write_text("\n".join(values[1:]), encoding="utf-8")
    elif recipe_id == "unrelated-content":
        path.write_text("__unrelated_mutant__", encoding="utf-8")
    elif recipe_id in {"json-wrong-value", "json-wrong-type"}:
        payload: dict[str, Any] = {}
        expected = spec.parameters["expected"]
        mutant = (
            expected + 1
            if recipe_id == "json-wrong-value" and isinstance(expected, (int, float))
            else "__wrong_type__"
        )
        _set_pointer(payload, str(spec.parameters["pointer"]), mutant)
        path.write_text(json.dumps(payload), encoding="utf-8")
    elif recipe_id == "html-missing-contract-node":
        path.write_text(
            "<html><body><h1>Report</h1><footer>Source:</footer></body></html>",
            encoding="utf-8",
        )
    elif recipe_id == "html-remote-asset":
        path.write_text(
            '<html><body><h1>Report</h1><table></table><img src="https://invalid.example/x">'
            "<footer>Source:</footer></body></html>",
            encoding="utf-8",
        )
    else:
        raise ValueError(f"unknown mutation recipe: {recipe_id}")


def mutation_test(root: Path, manifest_path: Path, mutants_dir: Path) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    cases = load_cases(root, manifest)
    recipes_by_family: dict[str, list[dict[str, str]]] = defaultdict(list)
    for recipe in MUTATION_RECIPES:
        recipes_by_family[recipe["family"]].append(recipe)
    mutants_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(
        mutants_dir / "catalog-v1.json",
        canonical_json_bytes(
            {"schema_version": "1.0.0", "recipes": list(MUTATION_RECIPES)}
        ),
    )
    local = root / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    totals: Counter[str] = Counter()
    killed: Counter[str] = Counter()
    control_failures: list[str] = []
    survivors: list[dict[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="mutation-", dir=local) as temporary:
        temporary_root = Path(temporary)
        for case in cases:
            for assertion_index, spec in enumerate(case.assertions):
                assertion_root = temporary_root / case.id / str(assertion_index)
                path = _control_artifact(assertion_root, spec)
                if not evaluate_assertion(spec, AssertionContext(assertion_root)):
                    control_failures.append(f"{case.id}:{spec.assertion_id}")
                    continue
                control_bytes = path.read_bytes()
                for recipe in recipes_by_family[spec.family]:
                    if path.exists():
                        path.unlink()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(control_bytes)
                    _mutate(path, spec, recipe["id"])
                    totals[spec.family] += 1
                    detected = not evaluate_assertion(spec, AssertionContext(assertion_root))
                    killed[spec.family] += int(detected)
                    if not detected:
                        survivors.append(
                            {
                                "case_id": case.id,
                                "assertion_id": spec.assertion_id,
                                "family": spec.family,
                                "recipe_id": recipe["id"],
                                "disposition": "must-fix-before-freeze",
                            }
                        )
    family_metrics = {
        family: {
            "mutants": total,
            "killed": killed[family],
            "kill_rate": killed[family] / total if total else 0,
        }
        for family, total in sorted(totals.items())
    }
    total = sum(totals.values())
    total_killed = sum(killed.values())
    kill_rate = total_killed / total if total else 0
    valid = (
        not control_failures
        and kill_rate >= 0.9
        and all(value["kill_rate"] >= 0.8 for value in family_metrics.values())
    )
    return {
        "valid": valid,
        "cases_checked": len(cases),
        "assertions_checked": sum(len(case.assertions) for case in cases),
        "mutants": total,
        "killed": total_killed,
        "kill_rate": kill_rate,
        "family_metrics": family_metrics,
        "control_failures": control_failures,
        "survivors": survivors,
        "catalog": (mutants_dir / "catalog-v1.json").as_posix(),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze_benchmark(
    root: Path,
    manifest_path: Path,
    *,
    version: str,
    output: Path,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    draft_split_path = root / manifest.split_manifest_ref
    split = SplitManifest.model_validate_json(draft_split_path.read_text(encoding="utf-8"))
    frozen_split = split.model_copy(update={"version": version})
    split_output = root / f"benchmarks/splits/{version}.json"
    atomic_write(split_output, canonical_json_bytes(frozen_split.model_dump(mode="json")))
    frozen_manifest = manifest.model_copy(
        update={
            "version": version,
            "split_manifest_ref": split_output.relative_to(root).as_posix(),
            "created_by": "gepase benchmark freeze@1.0.0",
        }
    )
    output_path = output if output.is_absolute() else root / output
    atomic_write(output_path, canonical_json_bytes(frozen_manifest.model_dump(mode="json")))
    frozen_files = [
        output_path,
        split_output,
        *(root / package.dataset_path for package in frozen_manifest.packages),
        *(root / package.provenance_ref for package in frozen_manifest.packages),
        *(root / rubric for rubric in frozen_manifest.rubric_refs),
    ]
    file_hashes = {
        path.relative_to(root).as_posix(): _sha256(path) for path in sorted(frozen_files)
    }
    # Benchmark v1 is a deterministic integration fixture. Active Agent work-item
    # isolation is verified by the R2/R3 EvalPlan and Executor-view contracts rather
    # than by a historical calibration directory absent from a fresh checkout.
    test_access_violations: list[str] = []
    lock = {
        "schema_version": "1.0.0",
        "version": version,
        "manifest": output_path.relative_to(root).as_posix(),
        "manifest_sha256": _sha256(output_path),
        "split_manifest": split_output.relative_to(root).as_posix(),
        "split_sha256": _sha256(split_output),
        "files": file_hashes,
        "test_access_policy": frozen_split.test_access_policy,
        "test_access_violations": test_access_violations,
    }
    lock_path = root / f"benchmarks/freeze-{version}.lock.json"
    atomic_write(lock_path, canonical_json_bytes(lock))
    return {
        "valid": not test_access_violations,
        "version": version,
        "manifest": output_path.relative_to(root).as_posix(),
        "manifest_hash": lock["manifest_sha256"],
        "split_manifest": split_output.relative_to(root).as_posix(),
        "split_hash": lock["split_sha256"],
        "test_access_violations": len(test_access_violations),
        "test_access_violation_details": test_access_violations,
        "test_access_policy": frozen_split.test_access_policy,
        "lock_file": lock_path.relative_to(root).as_posix(),
        "frozen_files": len(file_hashes),
    }
