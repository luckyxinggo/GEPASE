"""Recompute R5 completion Gates from sealed R2--R4 evidence and the static report."""

from __future__ import annotations

import argparse
import json
import math
import zipfile
from pathlib import Path
from typing import Any

from gepase.reporting.canary import CanaryReportBuilder, load_report_config
from gepase.store.artifacts import ArtifactStore, atomic_write, canonical_json_bytes, sha256_bytes


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _gate(gate_id: str, passed: bool, detail: str, evidence: list[str]) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "status": "passed" if passed else "failed",
        "detail": detail,
        "evidence": evidence,
    }


def _final_decisions(run: Path) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for path in sorted((run / "gate-decisions").glob("*.json")):
        row = _load(path)
        grouped.setdefault(str(row["candidate_id"]), []).append(row)
    return {
        candidate_id: max(
            rows,
            key=lambda row: (
                len(row.get("validation_pairs", [])),
                row.get("verdict") != "inconclusive",
            ),
        )
        for candidate_id, rows in grouped.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/canaries/slack-gif-creator-r5.json"),
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path("artifacts/runs/r5-slack-gif-creator-report"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/stages/R5/evidence/r5-gates.json"),
    )
    args = parser.parse_args()
    repo = args.repo.resolve()
    config_path = (repo / args.config).resolve()
    config = load_report_config(config_path)
    report_dir = (repo / args.report_dir).resolve()
    output = (repo / args.output).resolve()
    r2 = repo / config.r2_run_ref
    r3 = repo / config.r3_run_ref
    r4 = repo / config.r4_run_ref
    data = _load(report_dir / "report-data.json")
    manifest = _load(report_dir / "evidence-manifest.json")
    r4_audit = _load(r4 / "r4-audit.json")
    stages = {
        "R2": _load(repo / config.r2_stage_report_ref),
        "R3": _load(repo / config.r3_stage_report_ref),
        "R4": _load(repo / config.r4_stage_report_ref),
    }
    seals = {
        name: ArtifactStore(run).verify()
        for name, run in (("R2", r2), ("R3", r3), ("R4", r4))
    }

    gates: list[dict[str, Any]] = []
    end_to_end = bool(
        all(item.get("status") == "complete" for item in stages.values())
        and all(
            all(gate.get("status") == "passed" for gate in item.get("gate_results", []))
            for item in stages.values()
        )
        and r4_audit.get("valid") is True
        and data.get("success_gates", {}).get("end_to_end_complete") is True
        and manifest.get("agent_calls") == 0
        and manifest.get("headless_provider_calls") == 0
    )
    gates.append(
        _gate(
            "R5-G01-end-to-end-complete",
            end_to_end,
            "R2/R3/R4 are complete with passing upstream Gates; "
            "R5 consumed them with 0 Agent/API calls.",
            [
                config.r2_stage_report_ref,
                config.r3_stage_report_ref,
                config.r4_stage_report_ref,
                "evidence-manifest.json",
            ],
        )
    )

    gif_outputs = [
        item
        for item in manifest.get("copied_outputs", [])
        if item.get("media_type") == "image/gif"
    ]
    gif_valid = True
    for item in gif_outputs:
        destination = report_dir / str(item["path"])
        source = repo / str(item["source_ref"])
        gif_valid &= bool(
            destination.is_file()
            and source.is_file()
            and sha256_bytes(destination.read_bytes()) == item["sha256"]
            and sha256_bytes(source.read_bytes()) == item["source_sha256"]
        )
    real_artifacts = bool(
        len(data.get("validation_cases", [])) == 3
        and len(gif_outputs) == 9
        and gif_valid
        and all(item.valid and item.unindexed_files == 0 for item in seals.values())
        and not list(report_dir.rglob("result.json"))
        and data.get("success_gates", {}).get("real_artifacts_verified") is True
    )
    gates.append(
        _gate(
            "R5-G02-real-artifacts-verified",
            real_artifacts,
            f"Verified {len(gif_outputs)} copied task-native GIFs against sealed sources; "
            "upstream seals are clean.",
            ["assets/gifs/", "evidence-manifest.json", "report-data.json"],
        )
    )

    frontier = _load(r4 / "deployable-frontier.json")
    accepted = [item for item in frontier.get("entries", []) if item.get("accepted")]
    selected_id = str(data["deployable"]["candidate_id"])
    selected_summary = _load(
        r4 / "evals" / selected_id / "validation" / "candidate-run-summary.json"
    )
    deltas = [float(item["paired_delta"]) for item in selected_summary["pair_summaries"]]
    recomputed_mean = sum(deltas) / len(deltas)
    decisions = _final_decisions(r4)
    selected_decision = decisions[selected_id]
    gate3 = next(
        item
        for item in selected_decision["gates"]
        if item["level"] == "gate_3_validation"
    )
    strict = bool(
        len(accepted) == 1
        and accepted[0]["candidate_id"] == selected_id
        and selected_decision["verdict"] == "accepted"
        and selected_decision["frontier_eligible"] is True
        and gate3["outcome"] == "passed"
        and selected_summary["strict_wins"] == 3
        and selected_summary["ties"] == 0
        and selected_summary["losses"] == 0
        and recomputed_mean > 0
        and math.isclose(
            recomputed_mean,
            float(data["headline"]["validation_mean_delta"]),
            abs_tol=1e-12,
        )
        and all(
            value >= gate3["checks"]["category_regression_floor"]
            for value in gate3["checks"]["category_deltas"].values()
        )
    )
    gates.append(
        _gate(
            "R5-G03-strict-improvement-observed",
            strict,
            f"Candidate {selected_id} has recomputed held-out mean delta="
            f"{recomputed_mean:.8f}, 3/3 wins, and no floor violation.",
            [
                "../r4-slack-gif-creator-evolution/deployable-frontier.json",
                f"../r4-slack-gif-creator-evolution/evals/{selected_id}/validation/candidate-run-summary.json",
            ],
        )
    )

    merge = _load(r4 / "merge/build-record.json")
    conflicts = _load(r4 / "merge/conflict-report.json")
    merge_decision = decisions[str(merge["candidate_id"])]
    merge_exercised = bool(
        merge.get("status") == "materialized"
        and merge.get("same_package") is True
        and merge.get("same_snapshot") is True
        and merge.get("cross_package_parent_count") == 0
        and len(merge.get("parent_candidate_ids", [])) == 2
        and conflicts.get("unresolved") == 0
        and len(merge_decision.get("validation_pairs", [])) == 3
        and merge_decision.get("verdict") == "rejected"
        and data.get("success_gates", {}).get("merge_path_exercised") is True
    )
    gates.append(
        _gate(
            "R5-G04-merge-path-exercised",
            merge_exercised,
            "A same-package two-parent merge child was materialized, evaluated through "
            "held-out Gate, and honestly rejected.",
            [
                "../r4-slack-gif-creator-evolution/merge/build-record.json",
                "../r4-slack-gif-creator-evolution/merge/conflict-report.json",
            ],
        )
    )

    report_verification = CanaryReportBuilder(repo, config).verify(report_dir)
    report_seal = ArtifactStore(report_dir).verify()
    html = (report_dir / "index.html").read_text(encoding="utf-8")
    required_terms = (
        "Package Graph",
        "No-skill",
        "Original Skill",
        "Deployable",
        "GEPA",
        "Gate",
        "Provenance",
        "运行时间与角色使用量",
    )
    reproducible = bool(
        report_verification["valid"]
        and report_seal.valid
        and report_seal.unindexed_files == 0
        and all(term in html for term in required_terms)
        and data["commands"]["rebuild_report"]
        and data["commands"]["verify_report"]
        and data.get("success_gates", {}).get("report_reproducible") is True
    )
    gates.append(
        _gate(
            "R5-G05-report-reproducible",
            reproducible,
            f"Report seal checked={report_seal.checked}; report payload/manifests/copied "
            "outputs match independent recollection.",
            ["index.html", "report-data.json", "evidence-manifest.json", "artifact-index.json"],
        )
    )

    archive_path = report_dir / str(data["deployable"]["archive_path"])
    candidate = _load(r4 / "candidates" / selected_id / "candidate.json")
    archive_valid = archive_path.is_file()
    if archive_valid:
        with zipfile.ZipFile(archive_path) as archive:
            archive_valid = archive.namelist() == sorted(
                item["path"] for item in candidate["files"]
            )
            for item in candidate["files"]:
                archive_valid &= sha256_bytes(archive.read(item["path"])) == item["sha256"]
    first_five = all(item["status"] == "passed" for item in gates)
    release_ready = bool(
        first_five
        and archive_valid
        and all(data.get("success_gates", {}).values())
        and data.get("status") == "release_candidate_ready"
        and data["deployable"]["changed_files"] == ["SKILL.md"]
    )
    gates.append(
        _gate(
            "R5-G06-release-candidate-ready",
            release_ready,
            "All preceding R5 Gates passed; deployable archive contains "
            f"{len(candidate['files'])} hash-matched Package files.",
            [str(data["deployable"]["archive_path"]), "report-data.json"],
        )
    )

    payload = {
        "schema_version": "1.0.0",
        "valid": all(item["status"] == "passed" for item in gates),
        "report_id": data["report_id"],
        "gates": gates,
        "summary": {
            "passed": sum(item["status"] == "passed" for item in gates),
            "total": len(gates),
            "agent_calls": 0,
            "headless_provider_calls": 0,
            "validation_mean_delta": recomputed_mean,
            "deployable_candidate_id": selected_id,
            "report_artifacts_checked": report_seal.checked,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output, canonical_json_bytes(payload))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
