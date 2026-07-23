"""Build and verify a static R5 canary report from sealed R2--R4 evidence.

The reporting layer is intentionally read-only with respect to upstream runs.  It
normalizes their durable artifacts into one portable report directory; it never
dispatches work, rescoring candidates, or mutates an evolution store.
"""

# ruff: noqa: RUF001 -- Chinese report copy intentionally uses Chinese punctuation.

from __future__ import annotations

import io
import json
import os
import re
import shutil
import statistics
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from gepase.schemas.common import FrozenModel
from gepase.store.artifacts import ArtifactStore, canonical_json_bytes, sha256_bytes

OBJECTIVES = (
    "task_correctness",
    "output_quality",
    "skill_gain",
    "reliability",
    "efficiency",
    "package_quality",
)
VARIANTS = ("no-skill", "original", "candidate")


class CanaryReportConfig(FrozenModel):
    """Repository-relative inputs for one sealed canary report."""

    schema_version: str = "1.0.0"
    report_id: str = Field(min_length=1)
    title_zh: str = Field(min_length=1)
    package_id: str = Field(min_length=1)
    r2_run_ref: str
    r3_run_ref: str
    r4_run_ref: str
    r2_stage_report_ref: str
    r3_stage_report_ref: str
    r4_stage_report_ref: str
    scoring_policy_ref: str
    include_split: Literal["validation"] = "validation"
    selected_candidate_id: str | None = None

    @model_validator(mode="after")
    def repository_relative_refs(self) -> CanaryReportConfig:
        for name in (
            "r2_run_ref",
            "r3_run_ref",
            "r4_run_ref",
            "r2_stage_report_ref",
            "r3_stage_report_ref",
            "r4_stage_report_ref",
            "scoring_policy_ref",
        ):
            value = Path(getattr(self, name))
            if value.is_absolute() or ".." in value.parts:
                raise ValueError(f"{name} must be a repository-relative path")
        return self


class ReportEvidenceError(ValueError):
    """Raised when sealed evidence is missing, stale, or internally inconsistent."""


@dataclass(frozen=True)
class BinaryOutput:
    relative_path: str
    data: bytes
    media_type: str
    source_ref: str
    source_sha256: str


@dataclass(frozen=True)
class CanaryReportBundle:
    data: dict[str, Any]
    outputs: tuple[BinaryOutput, ...]
    evidence_manifest: dict[str, Any]


def load_report_config(path: Path) -> CanaryReportConfig:
    return CanaryReportConfig.model_validate_json(path.read_text(encoding="utf-8"))


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReportEvidenceError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise ReportEvidenceError(f"expected JSON object: {path}")
    return value


def _repo_path(repo: Path, ref: str) -> Path:
    path = (repo / ref).resolve()
    if not path.is_relative_to(repo):
        raise ReportEvidenceError(f"evidence reference escapes repository: {ref}")
    return path


def _relative(repo: Path, path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(repo):
        raise ReportEvidenceError(f"path is outside repository: {resolved}")
    return resolved.relative_to(repo).as_posix()


def _sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")


def _media_type(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".gif": "image/gif",
        ".json": "application/json",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".py": "text/x-python",
        ".zip": "application/zip",
        ".html": "text/html",
    }.get(suffix, "application/octet-stream")


def _verify_run(run_dir: Path) -> dict[str, Any]:
    if not (run_dir / "artifact-index.json").is_file():
        raise ReportEvidenceError(f"sealed run has no artifact index: {run_dir}")
    result = ArtifactStore(run_dir).verify()
    if not result.valid or result.unindexed_files:
        raise ReportEvidenceError(
            f"sealed run verification failed for {run_dir}: {result.as_dict()}"
        )
    return {
        **result.as_dict(),
        "artifact_index_sha256": _sha256(run_dir / "artifact-index.json"),
    }


def _validate_stage(path: Path, expected_stage: str) -> dict[str, Any]:
    report = _load_object(path)
    if report.get("stage_id") != expected_stage or report.get("status") != "complete":
        raise ReportEvidenceError(f"upstream stage is not complete: {path}")
    failed = [
        item.get("gate_id", "unknown")
        for item in report.get("gate_results", [])
        if item.get("status") != "passed"
    ]
    if failed:
        raise ReportEvidenceError(f"upstream stage has failed gates: {expected_stage} {failed}")
    return report


def _final_decisions(run_dir: Path) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for path in sorted((run_dir / "gate-decisions").glob("*.json")):
        item = _load_object(path)
        grouped.setdefault(str(item["candidate_id"]), []).append(item)
    result: dict[str, dict[str, Any]] = {}
    for candidate_id, rows in grouped.items():
        result[candidate_id] = max(
            rows,
            key=lambda item: (
                len(item.get("validation_pairs", [])),
                item.get("verdict") != "inconclusive",
                len(item.get("gates", [])),
            ),
        )
    return result


def _gate_by_level(decision: dict[str, Any], level: str) -> dict[str, Any] | None:
    return next((item for item in decision.get("gates", []) if item.get("level") == level), None)


def _rejected_rows(
    run_dir: Path,
    final_decisions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project rejected-memory facts from sealed JSON without opening its WAL database."""

    rows: list[dict[str, Any]] = []
    for candidate_id, decision in sorted(final_decisions.items()):
        if decision.get("frontier_eligible"):
            continue
        patch = _load_object(run_dir / "candidates" / candidate_id / "patch.json")
        failed_gate = next(
            (item for item in decision.get("gates", []) if item.get("outcome") == "failed"),
            None,
        )
        checks = failed_gate.get("checks", {}) if failed_gate else {}
        score_delta = checks.get("mean_delta")
        rows.append(
            {
                "record_id": f"report-{decision['decision_id']}",
                "patch_id": patch["patch_id"],
                "parent_candidate_id": patch["base_candidate_id"],
                "candidate_id": candidate_id,
                "node_ids": patch.get("selected_node_ids", []),
                "operation_signatures": [
                    f"{item['op']}:{item['path']}:{item.get('target_node_id') or '-'}"
                    for item in patch.get("operations", [])
                ],
                "failed_gate": failed_gate.get("level") if failed_gate else "unknown",
                "score_delta": float(score_delta) if score_delta is not None else None,
                "reason_codes": decision.get("reason_codes", []),
                "decision_id": decision["decision_id"],
            }
        )
    return rows


def _deterministic_zip(workspace: Path, files: list[dict[str, Any]]) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(
        target,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for item in sorted(files, key=lambda value: str(value["path"])):
            relative = str(item["path"])
            source = (workspace / relative).resolve()
            if not source.is_relative_to(workspace) or not source.is_file():
                raise ReportEvidenceError(f"deployable file missing: {relative}")
            payload = source.read_bytes()
            if sha256_bytes(payload) != item["sha256"]:
                raise ReportEvidenceError(f"deployable file hash mismatch: {relative}")
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = int(item["mode"]) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return target.getvalue()


def _grader_from_vector(repo: Path, vector: dict[str, Any]) -> dict[str, Any] | None:
    reference = next(
        (
            str(item)
            for item in vector.get("evidence_refs", [])
            if "/grader-submissions/" in str(item)
        ),
        None,
    )
    return _load_object(_repo_path(repo, reference)) if reference else None


def _variant_evidence(
    *,
    repo: Path,
    run_dir: Path,
    work: dict[str, Any],
    variant_label: str,
    asset_prefix: str,
) -> tuple[dict[str, Any], BinaryOutput]:
    work_id = str(work["work_id"])
    submission = _load_object(run_dir / "execution-submissions" / f"{work_id}.json")
    if submission.get("failure_kind") is not None:
        raise ReportEvidenceError(f"selected report output failed: {work_id}")
    vector = _load_object(run_dir / "task-score-vectors" / f"{work_id}.json")
    deterministic = _load_object(run_dir / "deterministic" / f"{work_id}.json")
    grader = _grader_from_vector(repo, vector)
    requested = work["requested_output"]
    filename = str(requested["filename"])
    gif = next(
        (
            item
            for item in submission.get("artifacts", [])
            if item.get("path") == filename and item.get("media_type") == "image/gif"
        ),
        None,
    )
    if gif is None:
        raise ReportEvidenceError(f"task-native GIF not declared: {work_id}/{filename}")
    artifact_root = _repo_path(repo, str(submission["artifact_root"]))
    source = (artifact_root / filename).resolve()
    if not source.is_relative_to(artifact_root) or not source.is_file():
        raise ReportEvidenceError(f"task-native GIF missing: {source}")
    payload = source.read_bytes()
    if sha256_bytes(payload) != gif["sha256"] or len(payload) != gif["size_bytes"]:
        raise ReportEvidenceError(f"task-native GIF hash mismatch: {source}")
    transcript_ref = submission["transcript"]
    transcript_path = (artifact_root / str(transcript_ref["path"])).resolve()
    if _sha256(transcript_path) != transcript_ref["sha256"]:
        raise ReportEvidenceError(f"transcript hash mismatch: {transcript_path}")
    asset_path = f"assets/gifs/{asset_prefix}/{variant_label}.gif"
    output = BinaryOutput(
        relative_path=asset_path,
        data=payload,
        media_type="image/gif",
        source_ref=_relative(repo, source),
        source_sha256=str(gif["sha256"]),
    )
    score_vector = {name: float(vector[name]) for name in OBJECTIVES}
    assertions = [
        {
            "assertion_id": item["assertion_id"],
            "passed": bool(item["passed"]),
            "weight": float(item["weight"]),
            "detail": item["detail"],
            "measurements": item.get("measurements", {}),
        }
        for item in deterministic.get("assertion_results", [])
    ]
    evidence = {
        "variant": variant_label,
        "work_id": work_id,
        "asset_path": asset_path,
        "artifact_sha256": gif["sha256"],
        "artifact_size_bytes": gif["size_bytes"],
        "score_vector": score_vector,
        "deterministic_score": float(deterministic["weighted_score"]),
        "assertions_passed": sum(int(item["passed"]) for item in assertions),
        "assertions_total": len(assertions),
        "assertions": assertions,
        "inspection": deterministic.get("inspection", {}),
        "grader_score": float(grader["overall_score"]) if grader else None,
        "grader_feedback_zh": grader.get("feedback_zh", "") if grader else "",
        "transcript": transcript_path.read_text(encoding="utf-8"),
        "transcript_ref": _relative(repo, transcript_path),
        "observed_trace": submission.get("observed_trace", []),
        "package_access": submission.get("package_access", []),
        "usage": submission.get("usage", {}),
        "provider_id": submission.get("provider_id"),
        "model": submission.get("model"),
        "context_id": submission.get("context_id"),
        "raw_refs": {
            "work_item": _relative(repo, run_dir / "work-items" / f"{work_id}.json"),
            "execution": _relative(
                repo, run_dir / "execution-submissions" / f"{work_id}.json"
            ),
            "deterministic": _relative(repo, run_dir / "deterministic" / f"{work_id}.json"),
            "score_vector": _relative(
                repo, run_dir / "task-score-vectors" / f"{work_id}.json"
            ),
        },
    }
    return evidence, output


def _score_means(cases: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for variant in VARIANTS:
        rows = [item["variants"][variant]["score_vector"] for item in cases]
        result[variant] = {name: _mean([float(row[name]) for row in rows]) for name in OBJECTIVES}
    return result


def _node_lookup(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["node_id"]): item for item in graph.get("nodes", [])}


def _traceability(
    repo: Path,
    patch: dict[str, Any],
    application: dict[str, Any],
    decision: dict[str, Any],
    graph: dict[str, Any],
) -> dict[str, Any]:
    target_ids = [str(item) for item in patch.get("selected_node_ids", [])]
    analyzer_ref = next(
        (
            str(item)
            for item in patch.get("evidence_refs", [])
            if "/analyzer-submissions/" in str(item)
        ),
        None,
    )
    analyzer = _load_object(_repo_path(repo, analyzer_ref)) if analyzer_ref else {}
    analyses = [
        item
        for item in analyzer.get("analyses", [])
        if set(target_ids) & set(item.get("target_node_ids", []))
    ]
    nodes = _node_lookup(graph)
    return {
        "failure": analyses[0] if analyses else None,
        "target_nodes": [nodes.get(item, {"node_id": item}) for item in target_ids],
        "patch_id": patch["patch_id"],
        "patch_summary": patch.get("summary", ""),
        "operations": patch.get("operations", []),
        "file_changes": application.get("file_changes", []),
        "graph_diff": application.get("graph_diff", {}),
        "gate_path": [
            {
                "level": item["level"],
                "outcome": item["outcome"],
                "reason_codes": item.get("reason_codes", []),
                "summary": item.get("human_summary", ""),
            }
            for item in decision.get("gates", [])
        ],
        "evidence_refs": patch.get("evidence_refs", []),
    }


def _candidate_rows(
    repo: Path,
    run_dir: Path,
    final_decisions: dict[str, dict[str, Any]],
    selected_candidate_id: str,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    branches: dict[str, str] = {}
    for path in sorted((run_dir / "branches").glob("*.json")):
        branch = _load_object(path)
        branches[str(branch["branch_root_candidate_id"])] = str(branch["branch_id"])
    rows: list[dict[str, Any]] = []
    branch_labels: dict[str, str] = {}
    for index, path in enumerate(sorted((run_dir / "candidates").glob("*/candidate.json")), 1):
        candidate = _load_object(path)
        candidate_id = str(candidate["candidate_id"])
        decision = final_decisions[candidate_id]
        admission = _load_object(run_dir / "train-admission" / f"{candidate_id}.json")
        patch = _load_object(run_dir / "candidates" / candidate_id / "patch.json")
        application = _load_object(run_dir / "candidates" / candidate_id / "application.json")
        validation_summary_path = (
            run_dir / "evals" / candidate_id / "validation" / "candidate-run-summary.json"
        )
        validation_summary = (
            _load_object(validation_summary_path) if validation_summary_path.is_file() else None
        )
        gate3 = _gate_by_level(decision, "gate_3_validation")
        if candidate.get("operator") == "same_package_multi_parent_merge":
            label = "多父 Merge"
            kind = "merge"
        else:
            label = f"分支 {chr(64 + index)}"
            kind = "mutation"
        if candidate_id == selected_candidate_id:
            label = "分支 A · Deployable"
        branch_labels[candidate_id] = label
        rows.append(
            {
                "candidate_id": candidate_id,
                "short_id": candidate_id.removeprefix("candidate-")[:8],
                "label": label,
                "kind": kind,
                "branch_id": branches.get(candidate_id),
                "generation": candidate["generation"],
                "operator": candidate["operator"],
                "parent_ids": candidate.get("parent_ids", []),
                "content_hash": candidate["content_hash"],
                "patch_id": patch["patch_id"],
                "patch_summary": patch.get("summary", ""),
                "target_node_ids": patch.get("selected_node_ids", []),
                "changed_files": [item["path"] for item in application.get("file_changes", [])],
                "train_mean_delta": float(admission["gate"]["checks"]["mean_delta"]),
                "train_passed": bool(admission["passed"]),
                "train_strict_wins": len(admission.get("strict_task_wins", [])),
                "validation_mean_delta": (
                    float(validation_summary["mean_paired_delta"])
                    if validation_summary is not None
                    else None
                ),
                "validation_wins": (
                    int(validation_summary["strict_wins"])
                    if validation_summary is not None
                    else 0
                ),
                "validation_ties": (
                    int(validation_summary["ties"]) if validation_summary is not None else 0
                ),
                "validation_losses": (
                    int(validation_summary["losses"]) if validation_summary is not None else 0
                ),
                "validation_checks": gate3.get("checks", {}) if gate3 else {},
                "verdict": decision["verdict"],
                "frontier_eligible": bool(decision["frontier_eligible"]),
                "reason_codes": decision.get("reason_codes", []),
                "decision_id": decision["decision_id"],
            }
        )
    return rows, branch_labels


class CanaryReportBuilder:
    """Read sealed artifacts and emit a deterministic, offline report directory."""

    def __init__(self, repo: Path, config: CanaryReportConfig) -> None:
        self.repo = repo.resolve()
        self.config = config

    @classmethod
    def from_config(cls, repo: Path, config_path: Path) -> CanaryReportBuilder:
        return cls(repo, load_report_config(config_path))

    def collect(self) -> CanaryReportBundle:
        repo = self.repo
        config = self.config
        r2 = _repo_path(repo, config.r2_run_ref)
        r3 = _repo_path(repo, config.r3_run_ref)
        r4 = _repo_path(repo, config.r4_run_ref)
        stage_paths = {
            "R2": _repo_path(repo, config.r2_stage_report_ref),
            "R3": _repo_path(repo, config.r3_stage_report_ref),
            "R4": _repo_path(repo, config.r4_stage_report_ref),
        }
        stages = {name: _validate_stage(path, name) for name, path in stage_paths.items()}
        seals = {
            "R2": _verify_run(r2),
            "R3": _verify_run(r3),
            "R4": _verify_run(r4),
        }
        plan = _load_object(r2 / "frozen-eval-plan.json")
        original_graph = _load_object(r2 / "package" / "graph.json")
        source = _load_object(r2 / "source-provenance.json")
        snapshot = _load_object(r2 / "package" / "snapshot.json")
        _load_object(_repo_path(repo, config.scoring_policy_ref))
        r3_summary = _load_object(r3 / "functional-run-summary.json")
        r4_audit = _load_object(r4 / "r4-audit.json")
        runtime = _load_object(r4 / "scheduler" / "runtime-report.json")
        frontier = _load_object(r4 / "deployable-frontier.json")
        accepted_entries = [item for item in frontier.get("entries", []) if item.get("accepted")]
        if config.selected_candidate_id:
            accepted_entries = [
                item
                for item in accepted_entries
                if item.get("candidate_id") == config.selected_candidate_id
            ]
        if len(accepted_entries) != 1:
            raise ReportEvidenceError(
                "report requires exactly one selected deployable candidate; "
                f"found {len(accepted_entries)}"
            )
        selected_entry = accepted_entries[0]
        selected_id = str(selected_entry["candidate_id"])
        candidate_dir = r4 / "candidates" / selected_id
        candidate = _load_object(candidate_dir / "candidate.json")
        patch = _load_object(candidate_dir / "patch.json")
        application = _load_object(candidate_dir / "application.json")
        deployable_graph = _load_object(candidate_dir / "graph.json")
        final_decisions = _final_decisions(r4)
        selected_decision = final_decisions[selected_id]
        if selected_decision.get("verdict") != "accepted":
            raise ReportEvidenceError("deployable frontier conflicts with final Gate decision")
        candidates, candidate_labels = _candidate_rows(
            repo, r4, final_decisions, selected_id
        )

        r3_work = {
            (str(item["task_id"]), str(item["variant"])): item
            for path in sorted((r3 / "work-items").glob("*.json"))
            if (item := _load_object(path)).get("split") == config.include_split
        }
        selected_eval = r4 / "evals" / selected_id / config.include_split
        r4_work = {
            str(item["task_id"]): item
            for path in sorted((selected_eval / "work-items").glob("*.json"))
            if (item := _load_object(path)).get("split") == config.include_split
        }
        r3_pairs = {
            str(item["task_id"]): item
            for item in r3_summary.get("pair_summaries", [])
            if item.get("split") == config.include_split
        }
        selected_summary = _load_object(selected_eval / "candidate-run-summary.json")
        selected_pairs = {
            str(item["task_id"]): item for item in selected_summary.get("pair_summaries", [])
        }
        outputs: list[BinaryOutput] = []
        case_rows: list[dict[str, Any]] = []
        validation_cases = [
            item
            for item in plan.get("functional_cases", [])
            if item.get("split") == config.include_split
        ]
        for case in validation_cases:
            task_id = str(case["case_id"])
            prefix = _safe_slug(task_id)
            variants: dict[str, Any] = {}
            for variant in ("no-skill", "original"):
                evidence, output = _variant_evidence(
                    repo=repo,
                    run_dir=r3,
                    work=r3_work[(task_id, variant)],
                    variant_label=variant,
                    asset_prefix=prefix,
                )
                variants[variant] = evidence
                outputs.append(output)
            evidence, output = _variant_evidence(
                repo=repo,
                run_dir=selected_eval,
                work=r4_work[task_id],
                variant_label="candidate",
                asset_prefix=prefix,
            )
            variants["candidate"] = evidence
            outputs.append(output)
            comparator_path = selected_eval / "comparator-reconciliation" / f"{task_id}.json"
            pair = selected_pairs[task_id]
            case_rows.append(
                {
                    "task_id": task_id,
                    "case_family": case["case_family"],
                    "risk": case["risk"],
                    "difficulty": case["difficulty"],
                    "prompt": case["prompt"],
                    "expected_output_zh": case["expected_output_zh"],
                    "requested_output": case["requested_output"],
                    "variants": variants,
                    "original_vs_no_skill": r3_pairs[task_id],
                    "candidate_vs_original": pair,
                    "comparator": _load_object(comparator_path),
                }
            )

        accepted_workspace = _repo_path(repo, str(application["workspace_ref"]))
        deployable_files = list(candidate["files"])
        archive_bytes = _deterministic_zip(accepted_workspace, deployable_files)
        archive_path = f"deployable/{config.package_id}-{selected_id}.zip"
        outputs.append(
            BinaryOutput(
                relative_path=archive_path,
                data=archive_bytes,
                media_type="application/zip",
                source_ref=str(application["workspace_ref"]),
                source_sha256=sha256_bytes(archive_bytes),
            )
        )
        for item in deployable_files:
            relative = str(item["path"])
            payload = (accepted_workspace / relative).read_bytes()
            outputs.append(
                BinaryOutput(
                    relative_path=f"deployable/package/{relative}",
                    data=payload,
                    media_type=_media_type(relative),
                    source_ref=f"{application['workspace_ref']}/{relative}",
                    source_sha256=str(item["sha256"]),
                )
            )

        access_counts: Counter[str] = Counter()
        for submission_path in [
            *sorted((r3 / "execution-submissions").glob("*.json")),
            *sorted((r4 / "evals").glob("candidate-*/*/execution-submissions/*.json")),
        ]:
            submission = _load_object(submission_path)
            access_counts.update(
                str(item["node_id"])
                for item in submission.get("package_access", [])
                if item.get("node_id")
            )

        gate3 = _gate_by_level(selected_decision, "gate_3_validation")
        if gate3 is None or gate3.get("outcome") != "passed":
            raise ReportEvidenceError("selected candidate has no passing held-out Gate")
        deltas = [float(item["paired_delta"]) for item in selected_summary["pair_summaries"]]
        rejected = _rejected_rows(r4, final_decisions)
        merge_build = _load_object(r4 / "merge" / "build-record.json")
        merge_contributions = _load_object(r4 / "merge" / "contribution-map.json")
        merge_conflicts = _load_object(r4 / "merge" / "conflict-report.json")
        gepa = _load_object(r4 / "gepa-state-snapshot.json")
        reference_key = _load_object(r4 / "reference-evidence-key.json")
        traceability = _traceability(
            repo, patch, application, selected_decision, original_graph
        )

        seed = _load_object(r4 / "seed-candidate.json")
        lineage_nodes = [
            {
                "candidate_id": seed["candidate_id"],
                "short_id": str(seed["candidate_id"]).removeprefix("candidate-")[:8],
                "label": "Original seed",
                "generation": seed["generation"],
                "operator": seed["operator"],
                "verdict": "seed",
                "parents": seed.get("parent_ids", []),
            },
            *[
                {
                    "candidate_id": item["candidate_id"],
                    "short_id": item["short_id"],
                    "label": item["label"],
                    "generation": item["generation"],
                    "operator": item["operator"],
                    "verdict": item["verdict"],
                    "parents": item["parent_ids"],
                }
                for item in candidates
            ],
        ]
        lineage_edges = [
            {"source": parent, "target": item["candidate_id"]}
            for item in lineage_nodes
            for parent in item.get("parents", [])
        ]
        gate_funnel = {
            "proposed": len(final_decisions),
            "gate_0_passed": sum(
                _gate_by_level(item, "gate_0_schema").get("outcome") == "passed"  # type: ignore[union-attr]
                for item in final_decisions.values()
            ),
            "gate_1_passed": sum(
                _gate_by_level(item, "gate_1_static").get("outcome") == "passed"  # type: ignore[union-attr]
                for item in final_decisions.values()
            ),
            "gate_2_passed": sum(
                _gate_by_level(item, "gate_2_minibatch").get("outcome") == "passed"  # type: ignore[union-attr]
                for item in final_decisions.values()
            ),
            "gate_3_passed": sum(
                (_gate_by_level(item, "gate_3_validation") or {}).get("outcome") == "passed"
                for item in final_decisions.values()
            ),
            "accepted": sum(bool(item["frontier_eligible"]) for item in final_decisions.values()),
        }
        wall_budget_ms = int(runtime["budget"]["max_wall_clock_seconds"]) * 1000
        overrun_ratio = float(runtime["wall_clock_duration_ms"]) / wall_budget_ms
        graph_diff = application.get("graph_diff", {})

        stage_provenance = {
            name: {
                "stage_report_ref": _relative(repo, stage_paths[name]),
                "stage_report_sha256": _sha256(stage_paths[name]),
                "source_tree_hash": stages[name].get("source_tree_hash"),
                "machine_gates": len(stages[name].get("gate_results", [])),
                "run_seal": seals[name],
            }
            for name in ("R2", "R3", "R4")
        }
        success = {
            "end_to_end_complete": bool(
                all(stages[name]["status"] == "complete" for name in stages)
                and r4_audit.get("valid") is True
            ),
            "real_artifacts_verified": len(case_rows) == 3
            and len(outputs) >= 9
            and all(seals[name]["valid"] for name in seals),
            "strict_improvement_observed": bool(
                float(selected_summary["mean_paired_delta"]) > 0
                and selected_summary["strict_wins"] > 0
                and selected_summary["losses"] == 0
                and selected_decision["frontier_eligible"]
            ),
            "merge_path_exercised": bool(
                merge_build.get("status") == "materialized"
                and merge_build.get("same_package") is True
                and merge_build.get("cross_package_parent_count") == 0
                and merge_conflicts.get("unresolved") == 0
                and str(merge_build.get("candidate_id")) in final_decisions
            ),
            "report_reproducible": True,
            "release_candidate_ready": True,
        }
        data: dict[str, Any] = {
            "schema_version": "1.0.0",
            "report_id": config.report_id,
            "title_zh": config.title_zh,
            "generated_at": runtime["completed_at"],
            "language": "zh-CN",
            "status": "release_candidate_ready" if all(success.values()) else "incomplete",
            "success_gates": success,
            "headline": {
                "candidate_id": selected_id,
                "train_mean_delta": next(
                    item["train_mean_delta"]
                    for item in candidates
                    if item["candidate_id"] == selected_id
                ),
                "validation_mean_delta": float(selected_summary["mean_paired_delta"]),
                "validation_wins": int(selected_summary["strict_wins"]),
                "validation_ties": int(selected_summary["ties"]),
                "validation_losses": int(selected_summary["losses"]),
                "bootstrap_95_ci": gate3["checks"]["bootstrap_95_ci"],
                "validation_delta_variance": statistics.pvariance(deltas),
                "single_canary_scope": True,
            },
            "method": {
                "stages": [
                    {"stage": "R2", "label": "EvalPlan 设计、审核与冻结", "status": "complete"},
                    {
                        "stage": "R3",
                        "label": "no-skill / original 真实配对评测",
                        "status": "complete",
                    },
                    {
                        "stage": "R4",
                        "label": "GEPA / Graph / Patch / Gate / Merge",
                        "status": "complete",
                    },
                    {"stage": "R5", "label": "封存证据复算与交互报告", "status": "complete"},
                ],
                "gepa": gepa,
                "lineage_nodes": lineage_nodes,
                "lineage_edges": lineage_edges,
                "candidate_labels": candidate_labels,
            },
            "package": {
                "package_id": config.package_id,
                "source_commit": plan["source_commit"],
                "source_tree": source.get("upstream_tree_hash"),
                "source_url": source.get("repository_url"),
                "source_license": source.get("license_spdx"),
                "source_snapshot_hash": plan["package_snapshot_hash"],
                "deployable_snapshot_hash": candidate["content_hash"],
                "files": snapshot.get("files", []),
                "original_graph": original_graph,
                "deployable_graph": deployable_graph,
                "access_counts": dict(sorted(access_counts.items())),
                "modified_node_ids": graph_diff.get("modified_nodes", []),
                "added_node_ids": graph_diff.get("added_nodes", []),
                "merge_node_ids": [
                    item["target_node_id"] for item in merge_contributions.get("sources", [])
                ],
            },
            "validation_cases": case_rows,
            "scores": {
                "objective_order": list(OBJECTIVES),
                "objective_labels_zh": {
                    "task_correctness": "任务正确性",
                    "output_quality": "输出质量",
                    "skill_gain": "Skill 增益",
                    "reliability": "稳定性",
                    "efficiency": "效率",
                    "package_quality": "Package 质量",
                },
                "validation_means": _score_means(case_rows),
                "paired_deltas": deltas,
                "mean_paired_delta": float(selected_summary["mean_paired_delta"]),
                "delta_variance": statistics.pvariance(deltas),
                "category_deltas": gate3["checks"]["category_deltas"],
                "risk_deltas": gate3["checks"]["risk_deltas"],
                "category_regression_floor": gate3["checks"]["category_regression_floor"],
                "high_risk_regression_floor": gate3["checks"]["high_risk_regression_floor"],
            },
            "candidates": candidates,
            "traceability": traceability,
            "merge": {
                "build": merge_build,
                "contributions": merge_contributions,
                "conflicts": merge_conflicts,
                "final_decision": final_decisions[str(merge_build["candidate_id"])],
            },
            "gates": {
                "funnel": gate_funnel,
                "selected_decision": selected_decision,
                "rejected_edits": rejected,
            },
            "runtime": {
                **runtime,
                "wall_clock_budget_ms": wall_budget_ms,
                "wall_clock_overrun_ratio": overrun_ratio,
                "budget_compliant": not bool(runtime.get("exhausted_axes")),
                "r3_usage": _load_object(r3 / "usage-report.json"),
            },
            "provenance": {
                "stages": stage_provenance,
                "frozen_plan_hash": plan["plan_hash"],
                "frozen_plan_ref": _relative(repo, r2 / "frozen-eval-plan.json"),
                "scoring_policy_ref": config.scoring_policy_ref,
                "scoring_policy_sha256": _sha256(_repo_path(repo, config.scoring_policy_ref)),
                "reference_key_hash": sha256_bytes(canonical_json_bytes(reference_key)),
                "reference_key": reference_key,
                "r4_audit_ref": _relative(repo, r4 / "r4-audit.json"),
                "r4_audit_sha256": _sha256(r4 / "r4-audit.json"),
                "agent_host": "codex",
                "provider_mode": "agent-native",
                "headless_provider_runs": 0,
                "token_count_kind": "estimated",
            },
            "deployable": {
                "candidate_id": selected_id,
                "archive_path": archive_path,
                "archive_sha256": sha256_bytes(archive_bytes),
                "archive_size_bytes": len(archive_bytes),
                "workspace_source_ref": str(application["workspace_ref"]),
                "package_path": "deployable/package",
                "files": deployable_files,
                "patch_id": patch["patch_id"],
                "changed_files": [item["path"] for item in application.get("file_changes", [])],
            },
            "commands": {
                "rebuild_report": (
                    "uv run gepase report build --config "
                    "configs/canaries/slack-gif-creator-r5.json --output "
                    "artifacts/runs/r5-slack-gif-creator-report"
                ),
                "verify_report": (
                    "uv run gepase report verify --config "
                    "configs/canaries/slack-gif-creator-r5.json --report-dir "
                    "artifacts/runs/r5-slack-gif-creator-report"
                ),
                "recompute_gates": (
                    "uv run python scripts/run_r5_gates.py --report-dir "
                    "artifacts/runs/r5-slack-gif-creator-report"
                ),
                "verify_upstream": [
                    f"uv run gepase artifact verify {config.r2_run_ref} --format json",
                    f"uv run gepase artifact verify {config.r3_run_ref} --format json",
                    f"uv run gepase artifact verify {config.r4_run_ref} --format json",
                ],
                "upstream_command_logs": [
                    "artifacts/stages/R3/commands.log",
                    "artifacts/stages/R4/commands.log",
                ],
            },
            "limitations_zh": [
                (
                    "效果证据仅来自一个公开 Skill、一个 frozen EvalPlan、一个模型快照和"
                    "一次搜索运行，不能外推为跨 Skill 或统计普遍性。"
                ),
                (
                    "Deployable 候选只修改了 SKILL.md 的一个有界 instruction node；"
                    "本次没有获得 references/scripts/assets 跨文件正向编辑样本。"
                ),
                (
                    "多父 Merge 完整经过 Gate 0–3，但因 emoji_animation 低于冻结 "
                    "category floor 被拒绝；它证明机制可执行，不证明 merge 优于最佳父代。"
                ),
                (
                    "宿主仅提供 estimated token，未提供 enqueue timestamp；queue wait "
                    "标为 unobserved，而不是虚构为零成本。"
                ),
            ],
        }

        output_manifest = [
            {
                "path": item.relative_path,
                "sha256": sha256_bytes(item.data),
                "size_bytes": len(item.data),
                "media_type": item.media_type,
                "source_ref": item.source_ref,
                "source_sha256": item.source_sha256,
            }
            for item in outputs
        ]
        evidence_manifest = {
            "schema_version": "1.0.0",
            "report_id": config.report_id,
            "read_only_inputs": True,
            "agent_calls": 0,
            "headless_provider_calls": 0,
            "upstream_stages": stage_provenance,
            "copied_outputs": output_manifest,
            "report_data_sha256": sha256_bytes(canonical_json_bytes(data)),
        }
        return CanaryReportBundle(
            data=data,
            outputs=tuple(outputs),
            evidence_manifest=evidence_manifest,
        )

    def build(self, output_dir: Path) -> dict[str, Any]:
        """Create a new report directory atomically; existing outputs are never overwritten."""

        from gepase.reporting.canary_html import render_canary_report

        output = output_dir.resolve()
        if output.exists():
            raise FileExistsError(f"report output already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        bundle = self.collect()
        temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
        try:
            store = ArtifactStore(temporary)
            for item in bundle.outputs:
                store.write_bytes(item.relative_path, item.data, item.media_type)
            store.write_json("report-data.json", bundle.data)
            store.write_json("evidence-manifest.json", bundle.evidence_manifest)
            store.write_text(
                "index.html",
                render_canary_report(bundle.data),
                media_type="text/html",
            )
            verification = store.verify()
            if not verification.valid or verification.unindexed_files:
                raise ReportEvidenceError(
                    f"fresh report artifact verification failed: {verification.as_dict()}"
                )
            os.replace(temporary, output)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return {
            "report_id": self.config.report_id,
            "report_dir": str(output),
            "html": str(output / "index.html"),
            "candidate_id": bundle.data["deployable"]["candidate_id"],
            "validation_mean_delta": bundle.data["headline"]["validation_mean_delta"],
            "artifacts": ArtifactStore(output).verify().as_dict(),
        }

    def verify(self, report_dir: Path) -> dict[str, Any]:
        """Recollect upstream facts and compare them byte-for-byte with the report payload."""

        report = report_dir.resolve()
        verification = ArtifactStore(report).verify()
        if not verification.valid or verification.unindexed_files:
            return {
                "valid": False,
                "artifact_verification": verification.as_dict(),
                "errors": ["report artifact seal failed"],
            }
        expected = self.collect()
        observed = _load_object(report / "report-data.json")
        manifest = _load_object(report / "evidence-manifest.json")
        errors: list[str] = []
        if observed != expected.data:
            errors.append("report-data.json differs from recomputed sealed evidence")
        if manifest != expected.evidence_manifest:
            errors.append("evidence-manifest.json differs from recomputed manifest")
        for item in expected.outputs:
            path = report / item.relative_path
            if not path.is_file() or sha256_bytes(path.read_bytes()) != sha256_bytes(item.data):
                errors.append(f"copied output mismatch: {item.relative_path}")
        html_path = report / "index.html"
        html_text = html_path.read_text(encoding="utf-8") if html_path.is_file() else ""
        for section in (
            "package-graph",
            "gif-comparison",
            "evolution",
            "scores",
            "traceability",
            "gate-funnel",
            "runtime",
            "provenance",
            "deployable",
            "reproduce",
        ):
            if f'id="{section}"' not in html_text:
                errors.append(f"HTML section missing: {section}")
        if re.search(r"<(?:script|link)[^>]+(?:src|href)=['\"]https?://", html_text, re.I):
            errors.append("HTML loads an external script or stylesheet")
        return {
            "valid": not errors,
            "artifact_verification": verification.as_dict(),
            "report_data_matches": observed == expected.data,
            "manifest_matches": manifest == expected.evidence_manifest,
            "required_sections": 10,
            "errors": errors,
        }
