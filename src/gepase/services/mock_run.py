"""S0 vertical slice using formal config, schemas, and artifact store."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

from gepase.config.loader import load_project_config
from gepase.runtime import environment_summary, git_commit, source_tree_hash
from gepase.schemas.common import BudgetSpec
from gepase.schemas.run import RunManifest
from gepase.store.artifacts import ArtifactStore
from gepase.testing.mock_provider import MockEvidenceProvider, default_tasks


def run_mock(config_path: Path, output: Path, project_root: Path) -> dict[str, Any]:
    loaded = load_project_config(config_path)
    provider = MockEvidenceProvider()
    results = [provider.evaluate(task) for task in default_tasks()]
    tree_hash = source_tree_hash(project_root)
    lock = project_root / "uv.lock"
    lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else "0" * 64
    manifest = RunManifest(
        run_id=str(uuid.uuid4()),
        git_commit=git_commit(project_root),
        source_tree_hash=tree_hash,
        dependency_lock_hash=lock_hash,
        config_hash=loaded.config_hash,
        provider_kind=loaded.config.provider.kind,
        agent_host=None,
        model=loaded.config.provider.model,
        seed=loaded.config.optimizer.seed,
        budget=BudgetSpec(
            max_work_items=loaded.config.budget.max_work_items,
            max_high_fidelity_items=loaded.config.budget.max_high_fidelity_items,
            max_tokens=loaded.config.budget.max_tokens,
        ),
        environment=environment_summary(),
    )
    store = ArtifactStore(output)
    store.write_json("resolved-config.json", loaded.redacted)
    store.write_json("run-manifest.json", manifest.model_dump(mode="json"))
    result_payload = {
        "schema_version": "1.0.0",
        "config_hash": loaded.config_hash,
        "source_tree_hash": tree_hash,
        "tasks": [result.__dict__ for result in results],
        "summary": {"total": len(results), "passed": sum(result.passed for result in results)},
    }
    store.write_json("results.json", result_payload)
    store.append_event("events.jsonl", {"event": "mock_run_completed", "passed": len(results)})
    return result_payload
