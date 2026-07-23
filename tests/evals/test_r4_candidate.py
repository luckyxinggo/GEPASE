from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from gepase.evals.engine import MultiFidelityEvalEngine
from gepase.evals.statistics import PairedScore
from gepase.optimizer.acceptance.minibatch import MinibatchPolicy, run_minibatch_gate
from gepase.optimizer.evolution_controller import R4EvolutionController
from gepase.optimizer.runtime import (
    ReferenceEvidenceKey,
    audit_reference_cache,
    build_reference_evidence_key,
    load_r4_config,
)
from gepase.store.artifacts import atomic_write, canonical_json_bytes

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "configs/canaries/slack-gif-creator-r4.json"


def _reference_key() -> ReferenceEvidenceKey:
    _hash, config = load_r4_config(ROOT, CONFIG)
    return build_reference_evidence_key(ROOT, config)


def test_reference_anchor_rehashes_all_r3_artifacts_and_rejects_partial_match() -> None:
    key = _reference_key()
    audit = audit_reference_cache(ROOT, key)
    assert audit.hit
    assert len(audit.verified_artifacts) == 429
    changed = dict(key.bound_artifact_hashes)
    changed["run-metadata.json"] = "0" * 64
    stale = key.model_copy(update={"bound_artifact_hashes": changed})
    stale_audit = audit_reference_cache(ROOT, stale)
    assert not stale_audit.hit
    assert "run-metadata.json" in stale_audit.mismatches
    assert not stale_audit.partial_match_used
    assert not stale_audit.stale_evidence_used


def test_candidate_plan_exports_only_fresh_oracle_free_train_work() -> None:
    key = _reference_key()
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="r4-candidate-plan-", dir=local) as temporary:
        run_dir = Path(temporary)
        key_path = run_dir / "reference-key.json"
        atomic_write(key_path, canonical_json_bytes(key.model_dump(mode="json")))
        with MultiFidelityEvalEngine(ROOT, run_dir) as engine:
            result = engine.plan_frozen_candidate(
                ROOT / "artifacts/runs/r2-slack-gif-creator-evalplan/frozen-eval-plan.json",
                ROOT / "configs/canaries/slack-gif-creator-r3-scoring.json",
                key_path,
                candidate_id="candidate-r4-seed-smoke",
                candidate_content_hash=key.reference_package_content_hash,
                candidate_ref="benchmarks/canaries/slack-gif-creator/package",
                package_graph_ref=(
                    "artifacts/runs/r2-slack-gif-creator-evalplan/package/graph.json"
                ),
                split="train",
                host=key.host,
                model=key.model,
                seed=key.seed,
                timeout_seconds=key.timeout_seconds,
            )
            exported = engine.export_work(run_dir / "executor-work.json")
            items = engine.ledger.work_items()
        assert result["reference_cache_hit"]
        assert result["selected_cases"] == 5
        assert exported["exported"] == 5
        assert {item.variant for item in items} == {"candidate"}
        payload = json.loads((run_dir / "executor-work.json").read_text(encoding="utf-8"))
        forbidden = {
            "variant",
            "candidate_id",
            "candidate_snapshot_hash",
            "reference_key",
            "pairing",
            "rubric",
            "expectations",
            "expected_output_zh",
        }
        assert all(forbidden.isdisjoint(item) for item in payload["work_items"])


def test_candidate_plan_rejects_cross_model_reference_reuse() -> None:
    key = _reference_key()
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="r4-candidate-model-", dir=local) as temporary:
        run_dir = Path(temporary)
        key_path = run_dir / "reference-key.json"
        atomic_write(key_path, canonical_json_bytes(key.model_dump(mode="json")))
        with MultiFidelityEvalEngine(ROOT, run_dir) as engine:
            with pytest.raises(ValueError, match="host/model/seed/timeout"):
                engine.plan_frozen_candidate(
                    ROOT
                    / "artifacts/runs/r2-slack-gif-creator-evalplan/frozen-eval-plan.json",
                    ROOT / "configs/canaries/slack-gif-creator-r3-scoring.json",
                    key_path,
                    candidate_id="candidate-r4-cross-model",
                    candidate_content_hash=key.reference_package_content_hash,
                    candidate_ref="benchmarks/canaries/slack-gif-creator/package",
                    package_graph_ref=(
                        "artifacts/runs/r2-slack-gif-creator-evalplan/package/graph.json"
                    ),
                    split="train",
                    host=key.host,
                    model="another-model",
                    seed=key.seed,
                    timeout_seconds=key.timeout_seconds,
                )


def test_train_gate_rejects_equal_candidate() -> None:
    row = PairedScore(
        task_id="train-case",
        category="temporal",
        risk_level="medium",
        parent_score=0.75,
        candidate_score=0.75,
        evidence_tier="E3",
        minimum_acceptance_tier="E2",
        parent_record_id="reference-vector",
        candidate_record_id="candidate-vector",
    )
    decision = run_minibatch_gate(
        (row,), policy=MinibatchPolicy(minimum_mean_delta=0.005)
    )
    assert decision.gate.outcome.value == "failed"
    assert not decision.promote_to_validation
    assert decision.gate.reason_codes == ("train_no_strict_improvement",)


def test_r4_controller_initializes_two_distinct_train_only_graph_branches() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="r4-controller-test-", dir=local) as temporary:
        run_dir = Path(temporary) / "r4-slack-gif-creator-evolution"
        controller = R4EvolutionController(ROOT, run_dir, CONFIG)
        result = controller.initialize()
        assert result["created"]
        assert result["proposal_work_items"] == 2
        branch_plan = json.loads((run_dir / "branch-plan.json").read_text(encoding="utf-8"))
        assert branch_plan["train_feedback_only"]
        assert not branch_plan["held_out_feedback_read"]
        assert len(branch_plan["branches"]) == 2
        assert len(
            {
                node_id
                for branch in branch_plan["branches"]
                for node_id in branch["target_node_ids"]
            }
        ) == 2
        assert all(
            str(branch["task_id"]).startswith("functional-train-")
            for branch in branch_plan["branches"]
        )
        state = controller.state()
        assert state.phase.value == "proposal"
        assert state.budget_usage.cache_hits == 1
        assert state.budget_usage.cache_misses == 0
