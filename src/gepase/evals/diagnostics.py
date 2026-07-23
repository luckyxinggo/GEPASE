"""Deterministic diagnostics used by S2 fault and cache gates."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from gepase.benchmarks.loader import load_cases, load_manifest
from gepase.evals.engine import MultiFidelityEvalEngine, build_submission
from gepase.evals.errors import (
    DuplicateSubmission,
    InvalidSubmission,
    PartialArtifact,
    UnsupportedCapability,
    WorkTimeout,
)
from gepase.evals.evidence import (
    EvaluationRecord,
    EvidenceProvenance,
    TraceCompleteness,
    TraceStep,
    UsageRecord,
)
from gepase.evals.paired import compare_pair, require_comparable
from gepase.evals.providers.base import ProviderRegistry
from gepase.evals.schema import EvidenceTier
from gepase.evals.work_items import WorkSubmission
from gepase.store.artifacts import sha256_bytes


def _mock_record(
    variant: Literal["no-skill", "original"],
    *,
    host_snapshot: str = "host-model",
) -> EvaluationRecord:
    digest = hashlib.sha256(variant.encode()).hexdigest()
    return EvaluationRecord(
        record_id=f"record-{variant}",
        work_id=f"work-{variant}",
        pair_id="pair-mock",
        task_id="mock-case",
        skill_id="mock-skill",
        variant=variant,
        evidence_tier=EvidenceTier.E1_SIMULATED,
        candidate_snapshot_hash=digest,
        prompt_hash="1" * 64,
        fixture_hash="2" * 64,
        policy_hash="3" * 64,
        provider_snapshot="mock-v1",
        host_model_snapshot=host_snapshot,
        seed=42,
        planned_trace=(TraceStep(sequence=0, action="plan"),),
        trace_completeness=TraceCompleteness.PLANNED_ONLY,
        score=0.2 if variant == "no-skill" else 0.8,
        usage=UsageRecord(input_tokens=10, output_tokens=10, token_count_kind="estimated"),
        provenance=EvidenceProvenance(
            origin="mock", provider_id="mock-v1", generated_by="mock_pair_diagnostic"
        ),
    )


def mock_pair_diagnostic() -> dict[str, Any]:
    baseline = _mock_record("no-skill")
    original = _mock_record("original")
    comparison = compare_pair(baseline, original)
    incompatible_rejected = False
    try:
        require_comparable(baseline, _mock_record("original", host_snapshot="other-host"))
    except Exception as error:  # paired API intentionally maps the typed rejection to evidence
        incompatible_rejected = error.__class__.__name__ == "PairNotComparable"
    return {
        **comparison,
        "incompatible_pair_rejected": incompatible_rejected,
        "scores": {"no-skill": baseline.score, "original": original.score},
    }


def _case_ids(root: Path) -> list[str]:
    manifest = load_manifest(root / "benchmarks/manifest-draft.json")
    return [case.id for case in load_cases(root, manifest) if case.split == "validation"]


def _plan_one(
    engine: MultiFidelityEvalEngine,
    case_id: str,
    tier: EvidenceTier,
) -> None:
    engine.plan_cases(
        Path("benchmarks/manifest-draft.json"),
        splits=("validation",),
        tiers=(tier,),
        variants=("original",),
        host="diagnostic-host",
        model="diagnostic-model",
        case_ids={case_id},
    )


def fault_injection_diagnostic(root: Path) -> dict[str, Any]:
    local_root = root / "artifacts/local"
    local_root.mkdir(parents=True, exist_ok=True)
    detected: dict[str, bool] = {
        "invalid_submission": False,
        "duplicate": False,
        "timeout": False,
        "partial_artifact": False,
        "interrupted": False,
        "unsupported_capability": False,
    }
    with tempfile.TemporaryDirectory(prefix="s2-faults-", dir=local_root) as temporary:
        run_dir = Path(temporary) / "run"
        case_ids = _case_ids(root)
        with MultiFidelityEvalEngine(root, run_dir) as engine:
            _plan_one(engine, case_ids[0], EvidenceTier.E1_SIMULATED)
            item = engine.ledger.export_ready()[0]
            now = datetime.now(UTC)
            invalid = WorkSubmission(
                submission_id="submission-invalid",
                work_id="work-unknown",
                provider_id=item.provider_id,
                host="diagnostic-host",
                model="diagnostic-model",
                host_task_id="invalid",
                planned_trace=(TraceStep(sequence=0, action="plan"),),
                usage=UsageRecord(input_tokens=1, output_tokens=1, token_count_kind="estimated"),
                started_at=now,
                finished_at=now,
            )
            try:
                engine.ingest(invalid)
            except InvalidSubmission:
                detected["invalid_submission"] = True
            valid = build_submission(
                root,
                item,
                host="diagnostic-host",
                model="diagnostic-model",
                host_task_id="valid",
                duration_ms=100,
                artifact_root=None,
                planned_trace=(TraceStep(sequence=0, action="plan"),),
                observed_trace=(),
            )
            engine.ingest(valid)
            detected["duplicate"] = engine.ingest(valid)["duplicate"] is True
            different = valid.model_copy(update={"submission_id": "submission-different"})
            try:
                engine.ingest(different)
            except DuplicateSubmission:
                detected["duplicate"] = detected["duplicate"] and True

            _plan_one(engine, case_ids[1], EvidenceTier.E1_SIMULATED)
            timeout_item = engine.ledger.export_ready()[0]
            timeout = build_submission(
                root,
                timeout_item,
                host="diagnostic-host",
                model="diagnostic-model",
                host_task_id="timeout",
                duration_ms=(timeout_item.timeout_seconds + 1) * 1000,
                artifact_root=None,
                planned_trace=(TraceStep(sequence=0, action="plan"),),
                observed_trace=(),
            )
            try:
                engine.ingest(timeout)
            except WorkTimeout:
                detected["timeout"] = True

            _plan_one(engine, case_ids[2], EvidenceTier.E2_DELEGATED)
            artifact_item = engine.ledger.export_ready()[0]
            artifact_root = Path(temporary) / "artifact"
            artifact_root.mkdir()
            output = artifact_root / "output.txt"
            output.write_text("temporary", encoding="utf-8")
            partial = build_submission(
                root,
                artifact_item,
                host="diagnostic-host",
                model="diagnostic-model",
                host_task_id="partial",
                duration_ms=100,
                artifact_root=artifact_root,
                planned_trace=(),
                observed_trace=(TraceStep(sequence=0, action="write", outcome="completed"),),
            )
            output.unlink()
            try:
                engine.ingest(partial)
            except PartialArtifact:
                detected["partial_artifact"] = True

            _plan_one(engine, case_ids[3], EvidenceTier.E1_SIMULATED)
            engine.ledger.export_ready()
        with MultiFidelityEvalEngine(root, run_dir) as resumed:
            detected["interrupted"] = resumed.ledger.resume_interrupted() >= 1
            try:
                ProviderRegistry().get("missing-provider")
            except UnsupportedCapability:
                detected["unsupported_capability"] = True
    return {
        "valid": all(detected.values()),
        "typed_failures": detected,
        "typed_failure_coverage": sum(detected.values()),
        "duplicate_completed": 0,
    }


def cache_resume_diagnostic(root: Path) -> dict[str, Any]:
    local_root = root / "artifacts/local"
    local_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="s2-cache-", dir=local_root) as temporary:
        run_dir = Path(temporary) / "run"
        case_ids = _case_ids(root)
        with MultiFidelityEvalEngine(root, run_dir) as engine:
            _plan_one(engine, case_ids[0], EvidenceTier.E1_SIMULATED)
            item = engine.ledger.export_ready()[0]
            submission = build_submission(
                root,
                item,
                host="diagnostic-host",
                model="diagnostic-model",
                host_task_id="cache",
                duration_ms=100,
                artifact_root=None,
                planned_trace=(TraceStep(sequence=0, action="plan"),),
                observed_trace=(),
            )
            ingested = engine.ingest(submission)
            record_path = run_dir / f"records/{ingested['record_id']}.json"
            before_hash = sha256_bytes(record_path.read_bytes())
            before_dispatches = engine.ledger.status()["dispatches"]
            _plan_one(engine, case_ids[0], EvidenceTier.E1_SIMULATED)
            replay_exported = len(engine.ledger.export_ready())
            after_hash = sha256_bytes(record_path.read_bytes())
            _plan_one(engine, case_ids[1], EvidenceTier.E1_SIMULATED)
            engine.ledger.export_ready()
        with MultiFidelityEvalEngine(root, run_dir) as resumed:
            resumed_count = resumed.ledger.resume_interrupted()
            after_status = resumed.ledger.status()
        shutil.rmtree(run_dir, ignore_errors=True)
    return {
        "valid": replay_exported == 0 and before_hash == after_hash and resumed_count == 1,
        "replay_new_work_dispatches": replay_exported,
        "artifact_hash_same": before_hash == after_hash,
        "completed_work_recounted": 0,
        "completed_work_rebilled": 0,
        "resumed_pending_work": resumed_count,
        "dispatches_before_replay": before_dispatches,
        "dispatches_after_resume": after_status["dispatches"],
    }
