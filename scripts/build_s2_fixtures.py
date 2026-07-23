"""Build valid E0-E3 record fixtures and publish their JSON schemas."""

from __future__ import annotations

import json
from pathlib import Path

from gepase.evals.evidence import (
    AssertionResult,
    EvaluationRecord,
    EvidenceProvenance,
    TraceCompleteness,
    TraceStep,
    UsageRecord,
)
from gepase.evals.schema import EvidenceTier
from gepase.evals.work_items import EvalWorkItem
from gepase.schemas.common import ArtifactRef
from gepase.store.artifacts import canonical_json_bytes

ROOT = Path.cwd()


def base(tier: EvidenceTier, record_id: str) -> dict[str, object]:
    return {
        "record_id": record_id,
        "work_id": f"work-{record_id}",
        "pair_id": f"pair-{tier.value}",
        "task_id": "fixture-task",
        "skill_id": "fixture-skill",
        "variant": "original",
        "evidence_tier": tier,
        "candidate_snapshot_hash": "a" * 64,
        "prompt_hash": "b" * 64,
        "fixture_hash": "c" * 64,
        "policy_hash": "d" * 64,
        "provider_snapshot": f"provider-{tier.value}",
        "host_model_snapshot": "host-model",
        "seed": 42,
        "trace_completeness": TraceCompleteness.NONE,
        "provenance": EvidenceProvenance(
            origin="static", provider_id="fixture", generated_by="build_s2_fixtures"
        ),
    }


def execute() -> None:
    fixture_root = ROOT / "tests/fixtures/evidence"
    fixture_root.mkdir(parents=True, exist_ok=True)
    e0 = EvaluationRecord.model_validate(base(EvidenceTier.E0_STATIC, "e0"))
    e1_data = base(EvidenceTier.E1_SIMULATED, "e1")
    e1_data.update(
        {
            "planned_trace": (TraceStep(sequence=0, action="select_nodes"),),
            "trace_completeness": TraceCompleteness.PLANNED_ONLY,
            "provenance": EvidenceProvenance(
                origin="simulation",
                provider_id="fixture",
                host="fixture-host",
                model="fixture-model",
                host_task_id="fixture-e1",
                submission_id="fixture-submission-e1",
                generated_by="build_s2_fixtures",
            ),
        }
    )
    e1 = EvaluationRecord.model_validate(e1_data)
    reference = ArtifactRef(
        path="output.txt",
        sha256="e" * 64,
        media_type="text/plain",
        size_bytes=10,
    )
    usage = UsageRecord(
        input_tokens=20,
        output_tokens=30,
        tool_calls=1,
        duration_ms=100,
        token_count_kind="reported",
    )
    provenance = EvidenceProvenance(
        origin="agent-native",
        provider_id="fixture",
        host="fixture-host",
        model="fixture-model",
        host_task_id="fixture-e2",
        submission_id="fixture-submission-e2",
        generated_by="build_s2_fixtures",
    )
    e2_data = base(EvidenceTier.E2_DELEGATED, "e2")
    e2_data.update(
        {
            "observed_trace": (
                TraceStep(sequence=0, action="write", target="output.txt", outcome="completed"),
            ),
            "trace_completeness": TraceCompleteness.COMPLETE,
            "artifact_root": "tests/fixtures/evidence/artifacts/e2",
            "artifacts": (reference,),
            "usage": usage,
            "provenance": provenance,
        }
    )
    e2 = EvaluationRecord.model_validate(e2_data)
    e3_data = base(EvidenceTier.E3_EXECUTABLE, "e3")
    e3_data.update(
        {
            "observed_trace": e2.observed_trace,
            "trace_completeness": TraceCompleteness.COMPLETE,
            "artifact_root": e2.artifact_root,
            "artifacts": e2.artifacts,
            "assertion_results": (
                AssertionResult(
                    assertion_id="fixture-assertion",
                    family="file_exists",
                    passed=True,
                    weight=1.0,
                ),
            ),
            "score": 1.0,
            "usage": usage,
            "provenance": provenance.model_copy(
                update={"origin": "assertion", "provider_id": "assertion-v1"}
            ),
            "source_record_refs": (e2.record_id,),
        }
    )
    e3 = EvaluationRecord.model_validate(e3_data)
    for record in (e0, e1, e2, e3):
        (fixture_root / f"{record.record_id}.json").write_bytes(
            canonical_json_bytes(record.model_dump(mode="json"))
        )
    schemas = ROOT / "schemas"
    schemas.mkdir(exist_ok=True)
    (schemas / "evaluation_record.schema.json").write_text(
        json.dumps(EvaluationRecord.model_json_schema(), indent=2, sort_keys=True) + "\n"
    )
    (schemas / "eval_work_item.schema.json").write_text(
        json.dumps(EvalWorkItem.model_json_schema(), indent=2, sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    execute()
