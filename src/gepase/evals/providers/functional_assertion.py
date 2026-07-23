"""E3 provider for frozen FunctionalEvalCase task-native artifacts."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from gepase.evals.eval_plan import FunctionalEvalCase
from gepase.evals.evidence import (
    AssertionResult,
    EvaluationRecord,
    EvidenceProvenance,
    TraceCompleteness,
    record_score,
)
from gepase.evals.functional import (
    DeterministicGradingBundle,
    FunctionalScoringPolicy,
    GifInspection,
    stable_role_id,
)
from gepase.evals.schema import EvidenceTier
from gepase.evals.work_items import canonical_hash
from gepase.schemas.common import ArtifactRef
from gepase.store.artifacts import ArtifactStore, sha256_bytes

OracleFunction = Callable[[dict[str, Any], str, str, str], dict[str, Any]]


def _load_oracle(project_root: Path, reference: str) -> tuple[OracleFunction, Path, str]:
    module_ref, separator, function_name = reference.partition(":")
    if not separator or not function_name:
        raise ValueError("oracle_ref must use repository/path.py:function syntax")
    module_path = (project_root / module_ref).resolve(strict=True)
    if not module_path.is_relative_to(project_root) or module_path.suffix != ".py":
        raise ValueError("oracle module must be a repository-relative Python file")
    module_hash = sha256_bytes(module_path.read_bytes())
    spec = importlib.util.spec_from_file_location(
        f"gepase_canary_oracle_{module_hash[:12]}", module_path
    )
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load oracle module: {module_ref}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, function_name, None)
    if not callable(function):
        raise ValueError(f"oracle function is unavailable: {function_name}")
    return cast(OracleFunction, function), module_path, module_hash


def _artifact_for_case(source: EvaluationRecord, case: FunctionalEvalCase) -> ArtifactRef:
    matches = [
        reference
        for reference in source.artifacts
        if reference.path == case.requested_output.filename
        and reference.media_type == case.requested_output.media_type
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one task-native output {case.requested_output.filename}, "
            f"found {len(matches)}"
        )
    return matches[0]


class FunctionalAssertionProvider:
    """Derive E3 records without embedding canary-specific rules in Core."""

    provider_id = "functional-assertion-v1"

    def evaluate(
        self,
        project_root: Path,
        run_dir: Path,
        case: FunctionalEvalCase,
        source: EvaluationRecord,
        policy: FunctionalScoringPolicy,
    ) -> tuple[EvaluationRecord, DeterministicGradingBundle]:
        if source.evidence_tier is not EvidenceTier.E2_DELEGATED:
            raise ValueError("functional E3 requires an E2 source record")
        if source.failure_kind is not None:
            raise ValueError("failed E2 records cannot be upgraded to E3")
        if source.artifact_root is None:
            raise ValueError("E2 record has no artifact root")
        submission_id = source.provenance.submission_id
        if submission_id is None:
            raise ValueError("E2 record has no submission provenance")
        artifact_ref = _artifact_for_case(source, case)
        artifact_root = (project_root / source.artifact_root).resolve(strict=True)
        artifact_path = (artifact_root / artifact_ref.path).resolve(strict=True)
        if not artifact_path.is_relative_to(artifact_root):
            raise ValueError("task-native artifact escapes its workspace")
        oracle, _oracle_path, oracle_hash = _load_oracle(project_root, policy.oracle_ref)
        evidence_dir = run_dir / "derived" / source.work_id
        result = oracle(
            case.model_dump(mode="json"),
            artifact_path.as_posix(),
            project_root.as_posix(),
            evidence_dir.as_posix(),
        )
        raw_assertions = result.get("assertions")
        raw_inspection = result.get("inspection")
        if not isinstance(raw_assertions, list) or not isinstance(raw_inspection, dict):
            raise ValueError("oracle returned an invalid evidence payload")
        expected_ids = {item.expectation_id for item in case.expectations}
        returned_ids = {str(item.get("assertion_id")) for item in raw_assertions}
        if returned_ids != expected_ids or len(raw_assertions) != len(expected_ids):
            raise ValueError("oracle assertion IDs do not match the frozen case")

        store = ArtifactStore(run_dir)
        contact_path = Path(str(raw_inspection["contact_sheet_path"])).resolve(strict=True)
        measurement_path = Path(str(raw_inspection["measurements_path"])).resolve(strict=True)
        for path in (contact_path, measurement_path):
            if not path.is_relative_to(run_dir):
                raise ValueError("oracle evidence must remain inside the run directory")
        contact_ref = contact_path.relative_to(project_root).as_posix()
        measurement_ref = measurement_path.relative_to(project_root).as_posix()
        store.index_existing(
            contact_path.relative_to(run_dir).as_posix(),
            "image/png",
        )
        store.index_existing(
            measurement_path.relative_to(run_dir).as_posix(),
            "application/json",
        )
        assertions = tuple(
            AssertionResult(
                assertion_id=str(item["assertion_id"]),
                family=str(item["family"]),
                passed=bool(item["passed"]),
                weight=float(item["weight"]),
                detail=str(item.get("detail", "")),
                evidence_refs=(measurement_ref, contact_ref),
                measurements=dict(item.get("measurements", {})),
            )
            for item in raw_assertions
        )
        inspection = GifInspection(
            artifact_ref=(Path(source.artifact_root) / artifact_ref.path).as_posix(),
            artifact_sha256=str(raw_inspection["artifact_sha256"]),
            width=int(raw_inspection["width"]),
            height=int(raw_inspection["height"]),
            frame_count=int(raw_inspection["frame_count"]),
            unique_frame_count=int(raw_inspection["unique_frame_count"]),
            total_duration_ms=int(raw_inspection["total_duration_ms"]),
            frame_durations_ms=tuple(int(value) for value in raw_inspection["frame_durations_ms"]),
            effective_fps=float(raw_inspection["effective_fps"]),
            loop_count=(
                int(raw_inspection["loop_count"])
                if raw_inspection.get("loop_count") is not None
                else None
            ),
            file_size_bytes=int(raw_inspection["file_size_bytes"]),
            mean_adjacent_pixel_delta=float(raw_inspection["mean_adjacent_pixel_delta"]),
            first_last_pixel_delta=float(raw_inspection["first_last_pixel_delta"]),
            contact_sheet_ref=contact_ref,
            measurements_ref=measurement_ref,
        )
        payload = {
            "source": source.record_id,
            "oracle": oracle_hash,
            "assertions": [item.model_dump(mode="json") for item in assertions],
        }
        record = EvaluationRecord(
            record_id=f"record-{canonical_hash(payload)[:24]}",
            work_id=f"{source.work_id}-assertions",
            pair_id=source.pair_id,
            task_id=source.task_id,
            skill_id=source.skill_id,
            variant=source.variant,
            evidence_tier=EvidenceTier.E3_EXECUTABLE,
            candidate_snapshot_hash=source.candidate_snapshot_hash,
            prompt_hash=source.prompt_hash,
            fixture_hash=source.fixture_hash,
            policy_hash=source.policy_hash,
            provider_snapshot=f"{self.provider_id}:{oracle_hash}",
            host_model_snapshot=source.host_model_snapshot,
            seed=source.seed,
            planned_trace=source.planned_trace,
            observed_trace=source.observed_trace,
            trace_completeness=TraceCompleteness.COMPLETE,
            artifact_root=source.artifact_root,
            artifacts=source.artifacts,
            assertion_results=assertions,
            score=record_score(assertions),
            usage=source.usage,
            uncertainty=0,
            provenance=EvidenceProvenance(
                origin="assertion",
                provider_id=self.provider_id,
                host=source.provenance.host,
                model=source.provenance.model,
                host_task_id=source.provenance.host_task_id,
                submission_id=submission_id,
                generated_by="FunctionalAssertionProvider.evaluate",
            ),
            source_record_refs=(source.record_id,),
        )
        bundle = DeterministicGradingBundle(
            bundle_id=stable_role_id("deterministic", payload),
            task_id=source.task_id,
            pair_id=source.pair_id,
            variant=source.variant,
            source_record_id=source.record_id,
            e3_record_id=record.record_id,
            oracle_ref=policy.oracle_ref,
            oracle_sha256=oracle_hash,
            inspection=inspection,
            assertion_results=assertions,
            weighted_score=float(record.score or 0),
        )
        return record, bundle
