"""Recompute R3 paired functional-evaluation gates from durable artifacts."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from PIL import Image

from gepase.evals.engine import MultiFidelityEvalEngine
from gepase.evals.functional import IsolationAudit
from gepase.evals.schema import EvidenceTier
from gepase.store.artifacts import ArtifactStore, atomic_write, canonical_json_bytes, sha256_bytes


def _gate(gate_id: str, passed: bool, detail: str, evidence: list[str]) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "status": "passed" if passed else "failed",
        "detail": detail,
        "evidence": evidence,
    }


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--run-dir", type=Path, default=Path("artifacts/runs/r3-slack-gif-creator-paired")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/stages/R3/evidence/r3-gates.json")
    )
    args = parser.parse_args()
    repo = args.repo.resolve()
    run_dir = (repo / args.run_dir).resolve()
    output = (repo / args.output).resolve()

    with MultiFidelityEvalEngine(repo, run_dir) as engine:
        items = engine.ledger.work_items()
        records = engine.ledger.records()
        status = engine.ledger.status()

        pairs: dict[str, list[Any]] = {}
        for item in items:
            pairs.setdefault(item.pair_id, []).append(item)
        comparable = all(
            len(pair) == 2
            and {item.variant for item in pair} == {"no-skill", "original"}
            and pair[0].pairing == pair[1].pairing
            and pair[0].prompt == pair[1].prompt
            and pair[0].fixture_refs == pair[1].fixture_refs
            and pair[0].requested_output == pair[1].requested_output
            and pair[0].required_capabilities == pair[1].required_capabilities
            and pair[0].timeout_seconds == pair[1].timeout_seconds
            for pair in pairs.values()
        )

        real_outputs = 0
        execution_ok = True
        for item in items:
            submission = engine.ledger.submission_for_work(item.work_id)
            record = engine.ledger.record_for_work(item.work_id)
            if submission is None or record is None:
                execution_ok = False
                continue
            filename = item.requested_output["filename"]
            matches = [artifact for artifact in record.artifacts if artifact.path == filename]
            if len(matches) != 1 or record.artifact_root is None:
                execution_ok = False
                continue
            artifact_path = repo / record.artifact_root / filename
            try:
                with Image.open(artifact_path) as image:
                    image.seek(1)
                    valid_gif = image.format == "GIF" and image.size[0] > 0 and image.size[1] > 0
            except (OSError, EOFError):
                valid_gif = False
            reference = matches[0]
            valid_gif = bool(
                valid_gif
                and sha256_bytes(artifact_path.read_bytes()) == reference.sha256
                and artifact_path.stat().st_size == reference.size_bytes
            )
            if valid_gif:
                real_outputs += 1
            execution_ok = bool(
                execution_ok
                and valid_gif
                and submission.transcript is not None
                and submission.observed_trace
                and submission.usage.nonempty
                and submission.context_id
            )

        canonical_e3 = [
            engine.ledger.record_for_work(f"{item.work_id}-assertions") for item in items
        ]
        e3_ok = all(
            record is not None
            and record.evidence_tier is EvidenceTier.E3_EXECUTABLE
            and record.assertion_results
            and all(
                result.evidence_refs and result.measurements
                for result in record.assertion_results
            )
            for record in canonical_e3
        )

    record_payloads = [
        _load_json(path) for path in sorted((run_dir / "records").glob("*.json"))
    ]
    canonical_record_ids = {record.record_id for record in records}
    durable_record_ids = {str(record["record_id"]) for record in record_payloads}
    orphan_record_ids = durable_record_ids - canonical_record_ids
    superseded_manifest = _load_json(run_dir / "replay-superseded-e3.json")
    superseded_items = superseded_manifest.get("items", [])
    classified_orphans = {
        str(item["previous_e3_record_id"])
        for item in superseded_items
        if isinstance(item, dict) and item.get("previous_e3_record_id")
    }
    canonical_source_ids = {
        record.record_id
        for record in records
        if record.evidence_tier is EvidenceTier.E2_DELEGATED
    }
    record_inventory_ok = bool(
        orphan_record_ids == classified_orphans
        and all(
            isinstance(item, dict)
            and item.get("source_record_id") in canonical_source_ids
            and item.get("previous_e3_record_id") in orphan_record_ids
            and item.get("current_e3_record_id") in canonical_record_ids
            for item in superseded_items
        )
    )

    gates: list[dict[str, Any]] = []
    pair_ok = len(items) == 16 and len(pairs) == 8 and comparable
    gates.append(
        _gate(
            "R3-G01-paired-configuration",
            pair_ok,
            f"{len(pairs)} pairs preserve prompt, fixtures, policy, host/model, seed, tools, "
            "capabilities, and timeout; Skill availability is the condition difference.",
            ["run-metadata.json", "ledger-snapshot.json", "work-items/"],
        )
    )
    gates.append(
        _gate(
            "R3-G02-real-e2-execution",
            execution_ok and real_outputs == 16 and status.get("completed") == 16,
            f"{real_outputs}/16 E2 work items have a decodable multi-frame GIF, transcript, "
            "observed trace, usage, context identity, and verified artifact hash.",
            ["execution-submissions/", "workspaces/", "records/"],
        )
    )

    gif_outside_workspaces = [
        path.relative_to(run_dir).as_posix()
        for path in run_dir.rglob("*.gif")
        if "workspaces" not in path.relative_to(run_dir).parts
    ]
    result_jsons = [
        path.relative_to(run_dir).as_posix()
        for path in (run_dir / "workspaces").rglob("result.json")
    ]
    e1_records = sum(record.evidence_tier is EvidenceTier.E1_SIMULATED for record in records)
    gates.append(
        _gate(
            "R3-G03-no-framework-business-output",
            not gif_outside_workspaces and not result_jsons and e1_records == 0,
            f"Framework-created business GIFs={len(gif_outside_workspaces)}, uniform business "
            f"result.json={len(result_jsons)}, E1 records={e1_records}.",
            ["workspaces/", "run-metadata.json"],
        )
    )
    gates.append(
        _gate(
            "R3-G04-content-level-e3",
            e3_ok and len(canonical_e3) == 16 and record_inventory_ok,
            "All 16 canonical E3 records bind every PASS/FAIL to measurements and content or "
            f"metadata evidence; filename/existence alone is insufficient. All "
            f"{len(orphan_record_ids)} historical record files are explicitly classified as "
            "superseded and cannot be mistaken for canonical scoring inputs.",
            [
                "deterministic/",
                "derived/",
                "records/",
                "replay-superseded-e3.json",
            ],
        )
    )

    isolation = IsolationAudit.model_validate_json(
        (run_dir / "isolation-audit.json").read_text(encoding="utf-8")
    )
    role_counts = {
        "executor": len(isolation.executor_context_ids),
        "grader": len(isolation.grader_context_ids),
        "comparator": len(isolation.comparator_context_ids),
        "analyzer": len(isolation.analyzer_context_ids),
    }
    isolation_ok = isolation.valid and role_counts == {
        "executor": 16,
        "grader": 16,
        "comparator": 6,
        "analyzer": 8,
    }
    gates.append(
        _gate(
            "R3-G05-role-isolation",
            isolation_ok,
            f"Fresh role contexts={role_counts}; duplicate/oracle/sibling/candidate leaks "
            "are zero.",
            ["isolation-audit.json", "grader-work-items/", "comparator-work-items/"],
        )
    )

    access = _load_json(run_dir / "package-access-audit.json")
    access_items_ok = bool(
        access.get("valid")
        and len(access.get("items", [])) == 16
        and all(item.get("valid") for item in access.get("items", []))
    )
    metadata = _load_json(run_dir / "run-metadata.json")
    graph = _load_json(repo / str(metadata["package_graph_ref"]))
    graph_node_ids = {
        str(node["node_id"])
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and node.get("node_id")
    }
    analyzer_submissions = [
        _load_json(path)
        for path in sorted((run_dir / "analyzer-submissions").glob("*.json"))
    ]
    analyzer_graph_ok = bool(
        len(analyzer_submissions) == 8
        and graph_node_ids
        and all(
            any(analysis.get("variant") == "original" for analysis in item.get("analyses", []))
            for item in analyzer_submissions
        )
        and all(
            analysis.get("evidence_refs")
            and (
                analysis.get("variant") != "original"
                or analysis.get("target_node_ids")
            )
            and set(analysis.get("target_node_ids", [])).issubset(graph_node_ids)
            for item in analyzer_submissions
            for analysis in item.get("analyses", [])
        )
    )
    access_ok = access_items_ok and analyzer_graph_ok
    gates.append(
        _gate(
            "R3-G06-graph-traceability",
            access_ok,
            "All no-skill access is empty; all original reads/executions resolve to frozen Graph "
            "nodes with byte/token accounting; every task has an original failure analysis whose "
            "non-empty targets resolve to the same frozen Graph.",
            [
                "package-access-audit.json",
                "package-access/",
                "analyzer-submissions/",
                str(metadata["package_graph_ref"]),
            ],
        )
    )

    score_verification = _load_json(run_dir / "score-independent-verification.json")
    trigger = _load_json(run_dir / "trigger-eval-separation.json")
    vector_payloads = [
        _load_json(path)
        for path in sorted((run_dir / "task-score-vectors").glob("*.json"))
    ]
    dimensions = (
        "task_correctness",
        "output_quality",
        "skill_gain",
        "reliability",
        "efficiency",
        "package_quality",
    )
    vectors_well_formed = bool(
        len(vector_payloads) == 16
        and all(
            all(
                isinstance(vector.get(dimension), int | float)
                and math.isfinite(float(vector[dimension]))
                for dimension in dimensions
            )
            and vector.get("evidence_refs")
            and vector.get("variant") in {"no-skill", "original"}
            for vector in vector_payloads
        )
    )
    score_ok = bool(
        score_verification.get("valid")
        and score_verification.get("recomputed_vectors") == 16
        and not score_verification.get("trigger_mixed")
        and trigger.get("functional_run_includes_trigger_scores") is False
        and vectors_well_formed
    )
    gates.append(
        _gate(
            "R3-G07-raw-score-recomputation",
            score_ok,
            "16 six-dimensional vectors independently reproduce from raw E2/E3, grades, AB/BA "
            "comparison, and E0; Trigger Eval remains separate.",
            [
                "task-score-vectors/",
                "score-independent-verification.json",
                "trigger-eval-separation.json",
            ],
        )
    )

    usage = _load_json(run_dir / "usage-report.json")
    usage_text = json.dumps(usage, ensure_ascii=False)
    required_roles = {"executor", "independent_grader", "comparator", "analyzer"}
    usage_ok = required_roles == set(usage.get("roles", {})) and "cost" not in usage_text
    usage_ok = bool(
        usage_ok
        and all(
            value.get("calls", 0) > 0
            and value.get("usage", {}).get("duration_ms", 0) > 0
            and value.get("usage", {}).get("tool_calls", 0) > 0
            and value.get("usage", {}).get("token_count_kind")
            for value in usage["roles"].values()
        )
    )
    required_outputs = (
        run_dir / "functional-run-summary.json",
        run_dir / "asi-dataset.json",
        run_dir / "comparator-reconciliation",
        run_dir / "grader-submissions",
        run_dir / "analyzer-submissions",
    )
    artifact_verification = ArtifactStore(run_dir).verify()
    complete_outputs = all(path.exists() for path in required_outputs)
    gates.append(
        _gate(
            "R3-G08-usage-and-durable-outputs",
            usage_ok and complete_outputs and artifact_verification.valid,
            "Calls, explicit token-count kind, timing, tools, and failures are reported for every "
            "role without a cost field; required typed outputs are durable and hash-verifiable.",
            ["usage-report.json", "functional-run-summary.json", "asi-dataset.json"],
        )
    )

    result = {
        "schema_version": "1.0.0",
        "stage_id": "R3",
        "valid": all(item["status"] == "passed" for item in gates),
        "passed": sum(item["status"] == "passed" for item in gates),
        "total": len(gates),
        "gates": gates,
    }
    atomic_write(output, canonical_json_bytes(result))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
