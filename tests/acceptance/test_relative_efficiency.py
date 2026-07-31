from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest

from gepase.evals.evidence import (
    EvaluationRecord,
    EvidenceProvenance,
    TraceCompleteness,
    TraceStep,
    UsageRecord,
)
from gepase.evals.schema import EvidenceTier
from gepase.evals.scores import TaskScoreVector
from gepase.evals.statistics import PairedScore
from gepase.optimizer.acceptance.models import GateOutcome
from gepase.optimizer.acceptance.validation import (
    RelativeEfficiencyFrontierPoint,
    ValidationPolicy,
    build_relative_efficiency_policy,
    derive_relative_efficiency_evidence,
    rank_relative_efficiency_frontier,
    run_validation_gate,
)
from gepase.optimizer.evolution_controller import R4EvolutionController
from gepase.optimizer.runtime import ReferenceEvidenceKey
from gepase.schemas.common import ArtifactRef


def _write_execution(
    root: Path,
    run_name: str,
    *,
    task_id: str,
    variant: Literal["original", "candidate"],
    duration_ms: int,
    tool_calls: int,
    tokens: int,
    token_kind: Literal["reported", "estimated", "unavailable"],
    artifact_size: int,
) -> str:
    run_dir = root / run_name
    records_dir = run_dir / "records"
    vectors_dir = run_dir / "vectors"
    work_dir = run_dir / "work-items"
    records_dir.mkdir(parents=True, exist_ok=True)
    vectors_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    work_id = f"work-{run_name}-{task_id}"
    record_ref = f"{run_name}/records/{task_id}.json"
    record = EvaluationRecord(
        record_id=f"record-{run_name}-{task_id}",
        work_id=work_id,
        pair_id=f"pair-{run_name}-{task_id}",
        task_id=task_id,
        skill_id="fixture-package",
        variant=variant,
        evidence_tier=EvidenceTier.E2_DELEGATED,
        candidate_snapshot_hash=("a" if variant == "original" else "b") * 64,
        prompt_hash="c" * 64,
        fixture_hash="d" * 64,
        policy_hash="e" * 64,
        provider_snapshot="fixture-provider",
        host_model_snapshot="fixture-host/model",
        seed=7,
        observed_trace=(
            TraceStep(sequence=0, action="write", target="output.bin", outcome="ok"),
        ),
        trace_completeness=TraceCompleteness.COMPLETE,
        artifact_root=f"{run_name}/artifacts/{work_id}",
        artifacts=(
            ArtifactRef(
                path="output.bin",
                sha256="f" * 64,
                media_type="application/octet-stream",
                size_bytes=artifact_size,
            ),
        ),
        usage=UsageRecord(
            input_tokens=tokens,
            tool_calls=tool_calls,
            duration_ms=duration_ms,
            token_count_kind=token_kind,
        ),
        provenance=EvidenceProvenance(
            origin="agent-native",
            provider_id="fixture",
            host="fixture-host",
            model="fixture-model",
            host_task_id=f"host-{work_id}",
            context_id=f"context-{work_id}",
            submission_id=f"submission-{work_id}",
            generated_by="test",
        ),
    )
    (records_dir / f"{task_id}.json").write_text(record.model_dump_json(), encoding="utf-8")
    (work_dir / f"{work_id}.json").write_text(
        json.dumps({"requested_output": {"filename": "output.bin"}}),
        encoding="utf-8",
    )
    vector = TaskScoreVector(
        task_id=task_id,
        pair_id=f"vector-{run_name}-{task_id}",
        variant=variant,
        candidate_snapshot_hash=record.candidate_snapshot_hash,
        task_correctness=0.8,
        output_quality=0.8,
        skill_gain=0.0,
        reliability=1.0,
        efficiency=0.5,
        package_quality=0.8,
        evidence_refs=(record_ref,),
        scoring_policy_ref="scoring-policy.json",
    )
    vector_path = vectors_dir / f"{task_id}.json"
    vector_path.write_text(vector.model_dump_json(), encoding="utf-8")
    return vector_path.relative_to(root).as_posix()


def _derive(
    tmp_path: Path,
    *,
    duration: tuple[int, int] = (100, 50),
    tools: tuple[int, int] = (4, 2),
    tokens: tuple[int, int] = (100, 50),
    token_kinds: tuple[
        Literal["reported", "estimated", "unavailable"],
        Literal["reported", "estimated", "unavailable"],
    ] = ("reported", "reported"),
    artifact_sizes: tuple[int, int] = (10, 10_000_000),
):
    original_ref = _write_execution(
        tmp_path,
        "reference",
        task_id="validation-1",
        variant="original",
        duration_ms=duration[0],
        tool_calls=tools[0],
        tokens=tokens[0],
        token_kind=token_kinds[0],
        artifact_size=artifact_sizes[0],
    )
    candidate_ref = _write_execution(
        tmp_path,
        "candidate",
        task_id="validation-1",
        variant="candidate",
        duration_ms=duration[1],
        tool_calls=tools[1],
        tokens=tokens[1],
        token_kind=token_kinds[1],
        artifact_size=artifact_sizes[1],
    )
    row = PairedScore(
        task_id="validation-1",
        category="behavior",
        risk_level="low",
        parent_score=0.7,
        candidate_score=0.8,
        evidence_tier="E3",
        minimum_acceptance_tier="E3",
        parent_record_id=original_ref,
        candidate_record_id=candidate_ref,
    )
    policy = build_relative_efficiency_policy()
    evidence = derive_relative_efficiency_evidence(
        tmp_path,
        (row,),
        candidate_id="candidate-fixture",
        reference_run_ref="reference",
        reference_key_hash="1" * 64,
        policy=policy,
    )
    return policy, evidence, row


@pytest.mark.parametrize(
    ("candidate_fraction", "expected_score"),
    ((0.5, 2 / 3), (1.0, 0.5), (2.0, 1 / 3)),
)
def test_relative_efficiency_ratio_mapping(
    tmp_path: Path, candidate_fraction: float, expected_score: float
) -> None:
    candidate = int(100 * candidate_fraction)
    _, evidence, _ = _derive(
        tmp_path,
        duration=(100, candidate),
        tools=(100, candidate),
        tokens=(100, candidate),
    )
    assert evidence.relative_cost_ratio == pytest.approx(candidate_fraction)
    assert evidence.relative_efficiency_score == pytest.approx(expected_score)


def test_relative_efficiency_excludes_unavailable_or_mismatched_tokens(tmp_path: Path) -> None:
    _, unavailable, _ = _derive(
        tmp_path / "unavailable",
        token_kinds=("unavailable", "estimated"),
        tokens=(1, 50),
    )
    token = next(item for item in unavailable.tasks[0].axes if item.axis == "tokens")
    assert token.exclusion_reason == "unavailable"
    assert unavailable.relative_cost_ratio == pytest.approx(0.5)

    _, mismatch, _ = _derive(
        tmp_path / "mismatch",
        token_kinds=("reported", "estimated"),
    )
    token = next(item for item in mismatch.tasks[0].axes if item.axis == "tokens")
    assert token.exclusion_reason == "measurement_kind_mismatch"
    assert mismatch.relative_cost_ratio == pytest.approx(0.5)


def test_controller_v2_validation_uses_existing_relative_acceptance_inputs(
    tmp_path: Path,
) -> None:
    policy, _expected, row = _derive(tmp_path)
    run_dir = tmp_path / "evolution"
    run_dir.mkdir()
    (run_dir / "relative-efficiency-policy.json").write_text(
        policy.model_dump_json(), encoding="utf-8"
    )
    reference_key = ReferenceEvidenceKey(
        reference_run_ref="reference",
        reference_variant="original",
        reference_package_snapshot_hash="a" * 64,
        reference_package_content_hash="b" * 64,
        frozen_plan_hash="c" * 64,
        frozen_plan_artifact_hash="d" * 64,
        case_contract_hashes={"validation-1": "e" * 64},
        fixture_hashes={"validation-1": "f" * 64},
        scoring_policy_hash="1" * 64,
        provider_snapshot="fixture-provider",
        host="fixture-host",
        model="fixture-model",
        host_model_snapshot="2" * 64,
        runtime_environment_fingerprint="fixture-runtime",
        tool_policy_fingerprint="fixture-tools",
        seed=7,
        timeout_seconds=60,
        host_policy="isolated",
        source_run_artifact_index_hash="3" * 64,
        bound_artifact_hashes={
            "run-metadata.json": "4" * 64,
            "functional-run-summary.json": "5" * 64,
            "score-recomputation-audit.json": "6" * 64,
            "package-access-audit.json": "7" * 64,
            "isolation-audit.json": "8" * 64,
        },
    )
    (run_dir / "reference-evidence-key.json").write_text(
        reference_key.model_dump_json(), encoding="utf-8"
    )
    controller = cast(Any, object.__new__(R4EvolutionController))
    controller.project_root = tmp_path.resolve()
    controller.run_dir = run_dir.resolve()
    controller.config = SimpleNamespace(
        efficiency_policy_mode="relative_v2",
        relative_efficiency_policy=policy,
    )

    regression, secondary, refs, resolved_policy, evidence = (
        controller._validation_efficiency_inputs("candidate-fixture", (row,))
    )

    assert regression == 0.0
    assert secondary is None
    assert resolved_policy == policy
    assert evidence is not None
    assert evidence.reference_key_hash == reference_key.key_hash
    assert refs == (
        "evolution/relative-efficiency-policy.json",
        "evolution/relative-efficiency-evidence/candidate-fixture.json",
    )


def test_zero_original_and_artifact_size_are_not_double_counted(tmp_path: Path) -> None:
    _, evidence, _ = _derive(
        tmp_path,
        duration=(100, 100),
        tools=(0, 4),
        tokens=(100, 100),
        artifact_sizes=(1, 10_000_000),
    )
    tool = next(item for item in evidence.tasks[0].axes if item.axis == "tool_calls")
    artifact = next(
        item for item in evidence.tasks[0].axes if item.axis == "artifact_size_bytes"
    )
    assert tool.exclusion_reason == "zero_original"
    assert artifact.exclusion_reason == "report_only"
    assert {item.axis for item in evidence.axis_aggregates} == {
        "duration_ms",
        "tool_calls",
        "tokens",
    }
    assert evidence.relative_cost_ratio == pytest.approx(1.0)


def test_v2_extreme_cost_is_the_only_efficiency_hard_reject(tmp_path: Path) -> None:
    policy, evidence, row = _derive(
        tmp_path,
        duration=(100, 200),
        tools=(100, 200),
        tokens=(100, 200),
    )
    v2 = run_validation_gate(
        (row,),
        policy=ValidationPolicy(bootstrap_samples=500),
        relative_efficiency_policy=policy,
        relative_efficiency_evidence=evidence,
    )
    assert v2.gate.outcome is GateOutcome.FAILED
    assert v2.gate.reason_codes == ("extreme_relative_cost_regression",)
    relative = v2.gate.checks["relative_efficiency"]
    assert isinstance(relative, dict)
    assert relative["v1_absolute_efficiency_axis_used"] is False

    v1 = run_validation_gate(
        (row,),
        policy=ValidationPolicy(bootstrap_samples=500),
    )
    assert v1.gate.outcome is GateOutcome.PASSED


def test_relative_efficiency_frontier_ranking_is_stable_for_zero_one_and_many() -> None:
    assert rank_relative_efficiency_frontier(()).ranks == ()
    single = rank_relative_efficiency_frontier(
        (RelativeEfficiencyFrontierPoint(candidate_id="one", validation_primary_delta=0.1),)
    )
    assert single.ranks[0].display_rank == 1
    ranked = rank_relative_efficiency_frontier(
        (
            RelativeEfficiencyFrontierPoint(
                candidate_id="balanced",
                validation_primary_delta=0.2,
                relative_efficiency_score=0.6,
            ),
            RelativeEfficiencyFrontierPoint(
                candidate_id="efficient",
                validation_primary_delta=0.1,
                relative_efficiency_score=0.8,
            ),
            RelativeEfficiencyFrontierPoint(
                candidate_id="dominated",
                validation_primary_delta=0.1,
                relative_efficiency_score=0.5,
            ),
            RelativeEfficiencyFrontierPoint(
                candidate_id="unknown",
                validation_primary_delta=0.2,
            ),
        )
    )
    assert [item.candidate_id for item in ranked.ranks] == [
        "balanced",
        "unknown",
        "efficient",
        "dominated",
    ]
    assert [item.pareto_layer for item in ranked.ranks] == [1, 1, 1, 2]
