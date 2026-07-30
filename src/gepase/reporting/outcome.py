"""Multi-outcome evolution reports in the existing GEPASE reporting subsystem."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field, model_validator

from gepase.optimizer.merge.models import MergeOutcome, MergeOutcomeStatus
from gepase.optimizer.runtime import EvolutionPhase, EvolutionRunState
from gepase.optimizer.session_runtime import ActiveSessionState, RuntimeSessionStatus
from gepase.schemas.common import FrozenModel
from gepase.store.artifacts import ArtifactStore, atomic_write, canonical_json_bytes, sha256_bytes

OBJECTIVES = {
    "task_correctness",
    "output_quality",
    "skill_gain",
    "reliability",
    "efficiency",
    "package_quality",
}


class EffectOutcome(StrEnum):
    STRICT_IMPROVEMENT = "strict_improvement"
    NO_STRICT_IMPROVEMENT = "no_strict_improvement"
    BUDGET_INCOMPLETE = "budget_incomplete"


class EvolutionOutcomeReportConfig(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    report_mode: Literal["multi_outcome"] = "multi_outcome"
    report_id: str = Field(min_length=1)
    title_zh: str = Field(min_length=1)
    package_id: str = Field(min_length=1)
    outcome_input_ref: str | None = None
    outcome_input_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def relative_input(self) -> EvolutionOutcomeReportConfig:
        refs = self.input_refs
        if not refs:
            raise ValueError("report config requires at least one outcome input")
        if len(refs) != len(set(refs)):
            raise ValueError("outcome input refs must be unique")
        for value in refs:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("outcome input must be repository-relative")
        return self

    @property
    def input_refs(self) -> tuple[str, ...]:
        return (
            *((self.outcome_input_ref,) if self.outcome_input_ref is not None else ()),
            *self.outcome_input_refs,
        )


class CandidateOutcomeRow(FrozenModel):
    candidate_id: str = Field(min_length=1)
    parent_ids: tuple[str, ...]
    patch_refs: tuple[str, ...]
    graph_path_refs: tuple[str, ...]
    train_mean_delta: float | None = None
    validation_mean_delta: float | None = None
    train_objective_deltas: dict[str, float] | None = None
    validation_objective_deltas: dict[str, float] | None = None
    train_wins: int = Field(default=0, ge=0)
    train_ties: int = Field(default=0, ge=0)
    train_losses: int = Field(default=0, ge=0)
    validation_wins: int = Field(default=0, ge=0)
    validation_ties: int = Field(default=0, ge=0)
    validation_losses: int = Field(default=0, ge=0)
    gate_status: str = Field(min_length=1)
    rejection_reasons: tuple[str, ...] = ()

    @model_validator(mode="after")
    def complete_objective_vectors(self) -> CandidateOutcomeRow:
        for value in (self.train_objective_deltas, self.validation_objective_deltas):
            if value is not None and set(value) != OBJECTIVES:
                raise ValueError("candidate objective deltas must contain all six objectives")
        return self


class FrontierReportEntry(FrozenModel):
    candidate_id: str = Field(min_length=1)
    package_ref: str = Field(min_length=1)
    provisional: bool = False
    lineage_refs: tuple[str, ...]
    patch_refs: tuple[str, ...]
    validation_summary_ref: str
    gate_decision_ref: str

    @model_validator(mode="after")
    def relative_refs(self) -> FrontierReportEntry:
        for value in (
            self.package_ref,
            self.validation_summary_ref,
            self.gate_decision_ref,
            *self.lineage_refs,
            *self.patch_refs,
        ):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("frontier evidence refs must be repository-relative")
        return self


class ReportEvidenceAsset(FrozenModel):
    asset_id: str = Field(pattern=r"^[a-zA-Z0-9._-]+$")
    task_id: str = Field(min_length=1)
    split: Literal["train", "validation"]
    variant: Literal["no-skill", "original", "candidate"]
    candidate_id: str | None = None
    execution_status: Literal["completed", "typed_failed"]
    failure_kind: str | None = None
    source_ref: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: Literal["image/gif"]
    size_bytes: int = Field(ge=1)
    label_zh: str = Field(min_length=1)

    @model_validator(mode="after")
    def relative_source(self) -> ReportEvidenceAsset:
        path = Path(self.source_ref)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("report evidence source must be repository-relative")
        if self.variant == "candidate" and self.candidate_id is None:
            raise ValueError("candidate evidence requires candidate_id")
        if self.variant != "candidate" and self.candidate_id is not None:
            raise ValueError("reference evidence cannot claim candidate_id")
        if (self.failure_kind is None) != (self.execution_status == "completed"):
            raise ValueError("report evidence failure status and failure_kind disagree")
        return self


class EvolutionOutcomeReportInput(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    run_id: str = Field(min_length=1)
    package_id: str = Field(min_length=1)
    outcome: EffectOutcome
    search_complete: bool
    reference_summary: dict[str, Any]
    candidates: tuple[CandidateOutcomeRow, ...]
    deployable_frontier: tuple[FrontierReportEntry, ...]
    merge_outcome: MergeOutcome
    gate_funnel: dict[str, int]
    rejected_memory_refs: tuple[str, ...]
    runtime: dict[str, Any]
    budget_checkpoint_refs: tuple[str, ...] = ()
    continuation_decision_refs: tuple[str, ...] = ()
    pending_work_ids: tuple[str, ...] = ()
    evidence_gallery: tuple[ReportEvidenceAsset, ...] = ()
    process_evidence: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any]

    @model_validator(mode="after")
    def honest_outcome(self) -> EvolutionOutcomeReportInput:
        ids = tuple(item.candidate_id for item in self.deployable_frontier)
        if len(ids) != len(set(ids)):
            raise ValueError("deployable frontier entries must be unique")
        gallery_ids = tuple(item.asset_id for item in self.evidence_gallery)
        if len(gallery_ids) != len(set(gallery_ids)):
            raise ValueError("report evidence assets must be unique")
        if self.outcome is EffectOutcome.STRICT_IMPROVEMENT:
            if not self.search_complete or not ids:
                raise ValueError("strict improvement requires a complete non-empty frontier")
            if any(item.provisional for item in self.deployable_frontier):
                raise ValueError("complete strict frontier cannot contain provisional entries")
        elif self.outcome is EffectOutcome.NO_STRICT_IMPROVEMENT:
            if not self.search_complete or ids:
                raise ValueError("no strict improvement requires a complete empty frontier")
        else:
            if self.search_complete:
                raise ValueError("budget_incomplete cannot claim complete search")
            if any(not item.provisional for item in self.deployable_frontier):
                raise ValueError("incomplete-run verified entries must remain provisional")
            if not self.pending_work_ids:
                raise ValueError("budget_incomplete requires explicit pending work")
        for value in (
            *self.rejected_memory_refs,
            *self.budget_checkpoint_refs,
            *self.continuation_decision_refs,
        ):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("report evidence refs must be repository-relative")
        return self


@dataclass(frozen=True)
class _Output:
    path: str
    data: bytes
    media_type: str


def _deterministic_package_zip(package: Path) -> tuple[bytes, list[dict[str, Any]]]:
    files = [item for item in sorted(package.rglob("*")) if item.is_file()]
    manifest: list[dict[str, Any]] = []
    target = io.BytesIO()
    with zipfile.ZipFile(
        target,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in files:
            relative = path.relative_to(package).as_posix()
            payload = path.read_bytes()
            manifest.append(
                {
                    "path": relative,
                    "sha256": sha256_bytes(payload),
                    "size_bytes": len(payload),
                }
            )
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (path.stat().st_mode & 0o777) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return target.getvalue(), manifest


class EvolutionOutcomeReportBuilder:
    """Render 0/1/many frontier outcomes without changing Core evidence."""

    def __init__(self, repo: Path, config: EvolutionOutcomeReportConfig) -> None:
        self.repo = repo.resolve()
        self.config = config

    @classmethod
    def from_config(
        cls, repo: Path, config_path: Path
    ) -> EvolutionOutcomeReportBuilder:
        return cls(
            repo,
            EvolutionOutcomeReportConfig.model_validate_json(
                config_path.read_text(encoding="utf-8")
            ),
        )

    def collect(self) -> tuple[dict[str, Any], tuple[_Output, ...]]:
        existing = [
            (reference, self.repo / reference)
            for reference in self.config.input_refs
            if (self.repo / reference).is_file()
        ]
        if len(existing) != 1:
            raise ValueError(
                "exactly one pre-registered outcome input must exist for report build"
            )
        source_ref, unresolved = existing[0]
        input_path = unresolved.resolve(strict=True)
        if not input_path.is_relative_to(self.repo):
            raise ValueError("outcome input escapes repository")
        outcome = EvolutionOutcomeReportInput.model_validate_json(
            input_path.read_text(encoding="utf-8")
        )
        if outcome.package_id != self.config.package_id:
            raise ValueError("report config and outcome input disagree on package_id")
        gallery_assets = outcome.evidence_gallery
        process_evidence = outcome.process_evidence
        if outcome.search_complete and not gallery_assets:
            evolution_dir = input_path.parent
            if (evolution_dir / "evolution-state.json").is_file():
                compiler = EvolutionOutcomeCompiler(self.repo, evolution_dir)
                gallery_assets = compiler._accepted_gif_gallery()
                process_evidence = compiler._process_evidence()
        outputs: list[_Output] = []
        frontier_rows: list[dict[str, Any]] = []
        for entry in outcome.deployable_frontier:
            package = (self.repo / entry.package_ref).resolve(strict=True)
            if not package.is_relative_to(self.repo) or not package.is_dir():
                raise ValueError("frontier package reference is invalid")
            archive, files = _deterministic_package_zip(package)
            archive_path = f"packages/{entry.candidate_id}.zip"
            outputs.append(_Output(archive_path, archive, "application/zip"))
            frontier_rows.append(
                {
                    **entry.model_dump(mode="json"),
                    "archive_path": archive_path,
                    "archive_sha256": sha256_bytes(archive),
                    "files": files,
                }
            )
        gallery_rows: list[dict[str, Any]] = []
        for index, asset in enumerate(gallery_assets):
            source = (self.repo / asset.source_ref).resolve(strict=True)
            if not source.is_relative_to(self.repo) or not source.is_file():
                raise ValueError("report evidence asset reference is invalid")
            payload = source.read_bytes()
            if sha256_bytes(payload) != asset.sha256 or len(payload) != asset.size_bytes:
                raise ValueError("report evidence asset changed after outcome compilation")
            report_path = f"evidence/gifs/{index:02d}-{asset.asset_id}.gif"
            outputs.append(_Output(report_path, payload, asset.media_type))
            gallery_rows.append(
                {
                    **asset.model_dump(mode="json"),
                    "report_path": report_path,
                }
            )
        data = {
            "schema_version": "1.0.0",
            "report_id": self.config.report_id,
            "title_zh": self.config.title_zh,
            "package_id": self.config.package_id,
            "outcome_input_ref": source_ref,
            "outcome": outcome.outcome.value,
            "search_complete": outcome.search_complete,
            "frontier_count": len(frontier_rows),
            "deployable_frontier": frontier_rows,
            "reference": outcome.reference_summary,
            "candidates": [item.model_dump(mode="json") for item in outcome.candidates],
            "merge": outcome.merge_outcome.model_dump(mode="json"),
            "gate_funnel": outcome.gate_funnel,
            "rejected_memory_refs": list(outcome.rejected_memory_refs),
            "runtime": outcome.runtime,
            "budget_checkpoints": list(outcome.budget_checkpoint_refs),
            "continuation_decisions": list(outcome.continuation_decision_refs),
            "pending_work_ids": list(outcome.pending_work_ids),
            "evidence_gallery": gallery_rows,
            "process_evidence": process_evidence,
            "provenance": outcome.provenance,
            "claim_boundary_zh": (
                "本报告只陈述当前 sealed run 的完整证据。"
                if outcome.search_complete
                else "本轮预注册搜索尚未收口; 已验证条目仅作 provisional evidence。"
            ),
        }
        return data, tuple(outputs)

    def build(self, output_dir: Path) -> dict[str, Any]:
        from gepase.reporting.canary_html import render_outcome_report

        output = output_dir.resolve()
        if output.exists():
            raise FileExistsError(f"report output already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        data, outputs = self.collect()
        temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
        try:
            store = ArtifactStore(temporary)
            for item in outputs:
                store.write_bytes(item.path, item.data, item.media_type)
            store.write_json("report-data.json", data)
            store.write_json(
                "evidence-manifest.json",
                {
                    "schema_version": "1.0.0",
                    "read_only_inputs": True,
                    "agent_calls": 0,
                    "source_ref": data["outcome_input_ref"],
                    "source_sha256": sha256_bytes(
                        (self.repo / str(data["outcome_input_ref"])).read_bytes()
                    ),
                    "report_data_sha256": sha256_bytes(canonical_json_bytes(data)),
                    "outputs": [
                        {"path": item.path, "sha256": sha256_bytes(item.data)}
                        for item in outputs
                    ],
                },
            )
            store.write_text("index.html", render_outcome_report(data), media_type="text/html")
            verification = store.verify()
            if not verification.valid or verification.unindexed_files:
                raise ValueError("fresh outcome report failed artifact verification")
            os.replace(temporary, output)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return {
            "report_id": self.config.report_id,
            "outcome": data["outcome"],
            "frontier_count": data["frontier_count"],
            "report_dir": str(output),
            "artifacts": ArtifactStore(output).verify().as_dict(),
        }

    def verify(self, report_dir: Path) -> dict[str, Any]:
        report = report_dir.resolve()
        verification = ArtifactStore(report).verify()
        errors: list[str] = []
        if not verification.valid or verification.unindexed_files:
            errors.append("report artifact seal failed")
        expected, outputs = self.collect()
        observed = json.loads((report / "report-data.json").read_text(encoding="utf-8"))
        if observed != expected:
            errors.append("report-data differs from Core outcome input")
        for item in outputs:
            path = report / item.path
            if not path.is_file() or sha256_bytes(path.read_bytes()) != sha256_bytes(item.data):
                errors.append(f"package archive mismatch: {item.path}")
        html = (report / "index.html").read_text(encoding="utf-8")
        for section in (
            "outcome",
            "deployable",
            "evidence",
            "candidates",
            "merge",
            "process",
            "runtime",
        ):
            if f'id="{section}"' not in html:
                errors.append(f"HTML section missing: {section}")
        return {
            "valid": not errors,
            "outcome": expected["outcome"],
            "frontier_count": expected["frontier_count"],
            "errors": errors,
            "artifact_verification": verification.as_dict(),
        }


class EvolutionOutcomeCompiler:
    """Compile the typed report input from one existing Core evolution run."""

    def __init__(self, repo: Path, run_dir: Path) -> None:
        self.repo = repo.resolve()
        self.run_dir = run_dir.resolve()
        if not self.run_dir.is_relative_to(self.repo):
            raise ValueError("evolution outcome run must remain inside repository")

    def _ref(self, path: Path) -> str:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.repo):
            raise ValueError("outcome evidence escapes repository")
        return resolved.relative_to(self.repo).as_posix()

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"expected JSON object: {path}")
        return value

    def _final_gate(self, candidate_id: str) -> tuple[Path | None, dict[str, Any] | None]:
        rows: list[tuple[Path, dict[str, Any]]] = []
        for path in sorted((self.run_dir / "gate-decisions").glob("*.json")):
            value = self._load(path)
            if value.get("candidate_id") == candidate_id:
                rows.append((path, value))
        if not rows:
            return None, None
        return max(
            rows,
            key=lambda item: (
                len(item[1].get("validation_pairs", [])),
                item[1].get("verdict") != "inconclusive",
                len(item[1].get("gates", [])),
            ),
        )

    def _split_metrics(
        self, candidate_id: str, split: str
    ) -> tuple[float | None, dict[str, float] | None, tuple[int, int, int]]:
        pairs_path = self.run_dir / f"evals/{candidate_id}/{split}/paired-scores.json"
        summary_path = self.run_dir / f"evals/{candidate_id}/{split}/candidate-run-summary.json"
        if not pairs_path.is_file() or not summary_path.is_file():
            return None, None, (0, 0, 0)
        pairs = self._load(pairs_path).get("rows", [])
        pair_summaries = self._load(summary_path).get("pair_summaries", [])
        if pair_summaries:
            deltas = [
                float(item["paired_delta"])
                if "paired_delta" in item
                else float(item["candidate_score"]) - float(item["reference_score"])
                for item in pair_summaries
            ]
        else:
            deltas = [
                float(item["delta"])
                if "delta" in item
                else float(item["candidate_score"]) - float(item["parent_score"])
                for item in pairs
            ]
        wins = sum(value > 0 for value in deltas)
        ties = sum(value == 0 for value in deltas)
        losses = sum(value < 0 for value in deltas)
        objective_rows: list[dict[str, float]] = []
        for pair in pair_summaries:
            candidate_ref = self.repo / str(pair["candidate_vector_ref"])
            reference_ref = self.repo / str(pair["reference_vector_ref"])
            candidate = self._load(candidate_ref)
            reference = self._load(reference_ref)
            objective_rows.append(
                {
                    key: float(candidate[key]) - float(reference[key])
                    for key in OBJECTIVES
                }
            )
        objectives = (
            {
                key: sum(row[key] for row in objective_rows) / len(objective_rows)
                for key in sorted(OBJECTIVES)
            }
            if objective_rows
            else None
        )
        return (
            sum(deltas) / len(deltas) if deltas else None,
            objectives,
            (wins, ties, losses),
        )

    def _accepted_gif_gallery(self) -> tuple[ReportEvidenceAsset, ...]:
        resolved = self._load(self.run_dir / "resolved-config.json")
        reference_run = (self.repo / str(resolved["reference_run_ref"])).resolve(strict=True)
        if not reference_run.is_relative_to(self.repo):
            raise ValueError("reference run for report gallery escapes repository")
        vector_paths = [*sorted(reference_run.glob("task-score-vectors/*.json"))]
        vector_paths.extend(
            sorted(self.run_dir.glob("evals/candidate-*/*/task-score-vectors/*.json"))
        )
        assets: list[ReportEvidenceAsset] = []
        for vector_path in vector_paths:
            vector = self._load(vector_path)
            variant = str(vector["variant"])
            if variant not in {"no-skill", "original", "candidate"}:
                raise ValueError("unexpected TaskScoreVector variant in report gallery")
            typed_variant = cast(Literal["no-skill", "original", "candidate"], variant)
            task_id = str(vector["task_id"])
            split = "validation" if "-validation-" in task_id else "train"
            candidate_id: str | None = None
            if variant == "candidate":
                relative_parts = vector_path.relative_to(self.run_dir).parts
                if len(relative_parts) < 2 or relative_parts[0] != "evals":
                    raise ValueError("candidate gallery vector is outside candidate evals")
                candidate_id = relative_parts[1]
            eval_root = vector_path.parent.parent
            work = self._load(eval_root / "work-items" / f"{vector_path.stem}.json")
            requested_filename = str(work["requested_output"]["filename"])
            e2_records: list[dict[str, Any]] = []
            for reference in vector.get("evidence_refs", []):
                path = self.repo / str(reference)
                if "/records/" not in f"/{Path(str(reference)).as_posix()}" or not path.is_file():
                    continue
                record = self._load(path)
                if record.get("evidence_tier") == "E2":
                    e2_records.append(record)
            gif_rows = [
                (record, artifact)
                for record in e2_records
                for artifact in record.get("artifacts", [])
                if artifact.get("media_type") == "image/gif"
                and artifact.get("path") == requested_filename
            ]
            if not gif_rows:
                continue
            if len(gif_rows) != 1:
                raise ValueError("accepted TaskScoreVector must resolve to one task-native GIF")
            record, artifact = gif_rows[0]
            artifact_root = Path(str(record["artifact_root"]))
            artifact_path = Path(str(artifact["path"]))
            if (
                artifact_root.is_absolute()
                or ".." in artifact_root.parts
                or artifact_path.is_absolute()
                or ".." in artifact_path.parts
            ):
                raise ValueError("report gallery artifact path is not repository-relative")
            source = (self.repo / artifact_root / artifact_path).resolve(strict=True)
            if not source.is_relative_to(self.repo):
                raise ValueError("report gallery artifact escapes repository")
            payload = source.read_bytes()
            digest = sha256_bytes(payload)
            if digest != artifact["sha256"] or len(payload) != int(artifact["size_bytes"]):
                raise ValueError("report gallery artifact differs from accepted E2 hash")
            identity = {
                "task_id": task_id,
                "variant": variant,
                "candidate_id": candidate_id,
                "source_ref": source.relative_to(self.repo).as_posix(),
                "sha256": digest,
            }
            assets.append(
                ReportEvidenceAsset(
                    asset_id=f"gif-{sha256_bytes(canonical_json_bytes(identity))[:24]}",
                    task_id=task_id,
                    split=split,
                    variant=typed_variant,
                    candidate_id=candidate_id,
                    execution_status=(
                        "typed_failed" if record.get("failure_kind") is not None else "completed"
                    ),
                    failure_kind=(
                        str(record["failure_kind"])
                        if record.get("failure_kind") is not None
                        else None
                    ),
                    source_ref=source.relative_to(self.repo).as_posix(),
                    sha256=digest,
                    media_type="image/gif",
                    size_bytes=len(payload),
                    label_zh=(
                        f"{split} · {variant} · {task_id}"
                        + (f" · {candidate_id}" if candidate_id is not None else "")
                    ),
                )
            )
        return tuple(
            sorted(
                assets,
                key=lambda item: (
                    item.split,
                    item.task_id,
                    item.variant,
                    item.candidate_id or "",
                ),
            )
        )

    def _process_evidence(self) -> dict[str, Any]:
        binding_paths = sorted(self.run_dir.glob("selector-graphs/*/*/binding.json"))
        if len(binding_paths) != 1:
            raise ValueError("complete report requires exactly one seed selector graph binding")
        binding = self._load(binding_paths[0])
        proposal_paths = sorted(
            path
            for path in (self.run_dir / "proposal-work-items").glob("*.json")
            if "-repair-" not in path.stem
        )
        proposal_scopes = []
        for path in proposal_paths:
            value = self._load(path)
            ranking = value["selector_ranking"]
            proposal_scopes.append(
                {
                    "work_id": value["work_id"],
                    "work_item_ref": self._ref(path),
                    "selected_node_ids": ranking["selected_node_ids"],
                    "top_k": ranking["top_k"],
                    "executable_alternative": ranking["executable_alternative"],
                    "target_set": value["target_set"],
                    "edit_budget": value["edit_budget"],
                }
            )
        patch_rows = []
        for path in sorted((self.run_dir / "candidates").glob("candidate-*/patch.json")):
            patch = self._load(path)
            patch_rows.append(
                {
                    "patch_ref": self._ref(path),
                    "patch_id": patch["patch_id"],
                    "summary": patch["summary"],
                    "operations": patch["operations"],
                }
            )
        reflection_rows = []
        for path in sorted((self.run_dir / "reflection-submissions").glob("*.json")):
            reflection = self._load(path)
            reflection_rows.append(
                {
                    "reflection_ref": self._ref(path),
                    "work_id": reflection["work_id"],
                    "summary_zh": reflection["summary_zh"],
                    "diagnosis_count": len(reflection["diagnoses"]),
                }
            )
        return {
            "package_graph": {
                "binding_ref": self._ref(binding_paths[0]),
                "mode": binding["mode"],
                "layer_counts": binding["layer_counts"],
                "mapped_access_events": binding["mapped_access_events"],
                "accepted_work_ids": binding["accepted_work_ids"],
                "filtered_work_ids": binding["filtered_work_ids"],
                "semantic_hypothesis_edges": binding["semantic_hypothesis_edges"],
                "selector_graph_ref": binding["selector_graph_ref"],
            },
            "search": {
                "branch_plan_ref": self._ref(self.run_dir / "branch-plan.json"),
                "branch_plan": self._load(self.run_dir / "branch-plan.json"),
                "proposal_scopes": proposal_scopes,
            },
            "patches": patch_rows,
            "lineage": {
                "branch_refs": [
                    self._ref(path)
                    for path in sorted((self.run_dir / "branches").glob("*.json"))
                ],
                "gepa_state_ref": self._ref(self.run_dir / "gepa-state-snapshot.json"),
                "gepa_state": self._load(self.run_dir / "gepa-state-snapshot.json"),
            },
            "reflections": reflection_rows,
            "merge": {
                "enumeration_ref": self._ref(
                    self.run_dir / "merge/parent-set-enumeration.json"
                ),
                "outcome_ref": self._ref(self.run_dir / "merge/outcome.json"),
            },
        }

    def compile(self, output: Path | None = None) -> EvolutionOutcomeReportInput:
        state = EvolutionRunState.model_validate_json(
            (self.run_dir / "evolution-state.json").read_text(encoding="utf-8")
        )
        runtime_state = ActiveSessionState.model_validate_json(
            (self.run_dir / "runtime-session.json").read_text(encoding="utf-8")
        )
        if runtime_state.status is RuntimeSessionStatus.ABORTED:
            raise ValueError("aborted run cannot form an effect outcome")
        search_complete = state.phase is EvolutionPhase.COMPLETE
        if search_complete:
            outcome = (
                EffectOutcome.STRICT_IMPROVEMENT
                if state.deployable_candidate_ids
                else EffectOutcome.NO_STRICT_IMPROVEMENT
            )
        else:
            if runtime_state.status is not RuntimeSessionStatus.STOPPED:
                raise ValueError(
                    "budget-incomplete effect report requires stop_and_report decision"
                )
            outcome = EffectOutcome.BUDGET_INCOMPLETE

        candidate_ids = tuple(
            dict.fromkeys((*state.branch_candidate_ids, *state.merge_candidate_ids))
        )
        candidates: list[CandidateOutcomeRow] = []
        frontier: list[FrontierReportEntry] = []
        for candidate_id in candidate_ids:
            train_delta, train_objectives, train_counts = self._split_metrics(
                candidate_id, "train"
            )
            validation_delta, validation_objectives, validation_counts = self._split_metrics(
                candidate_id, "validation"
            )
            gate_path, gate = self._final_gate(candidate_id)
            reason_codes = tuple(
                sorted(
                    {
                        str(reason)
                        for item in (gate or {}).get("gates", [])
                        if item.get("outcome") == "failed"
                        for reason in item.get("reason_codes", [])
                    }
                )
            )
            patch_path = self.run_dir / f"candidates/{candidate_id}/patch.json"
            graph_path = self.run_dir / f"candidates/{candidate_id}/graph.json"
            candidates.append(
                CandidateOutcomeRow(
                    candidate_id=candidate_id,
                    parent_ids=tuple(
                        self._load(self.run_dir / f"candidates/{candidate_id}/candidate.json")[
                            "parent_ids"
                        ]
                    ),
                    patch_refs=(self._ref(patch_path),) if patch_path.is_file() else (),
                    graph_path_refs=(self._ref(graph_path),) if graph_path.is_file() else (),
                    train_mean_delta=train_delta,
                    validation_mean_delta=validation_delta,
                    train_objective_deltas=train_objectives,
                    validation_objective_deltas=validation_objectives,
                    train_wins=train_counts[0],
                    train_ties=train_counts[1],
                    train_losses=train_counts[2],
                    validation_wins=validation_counts[0],
                    validation_ties=validation_counts[1],
                    validation_losses=validation_counts[2],
                    gate_status=str((gate or {}).get("verdict", "not_reached")),
                    rejection_reasons=reason_codes,
                )
            )
            if candidate_id not in state.deployable_candidate_ids:
                continue
            application = self._load(
                self.run_dir / f"candidates/{candidate_id}/application.json"
            )
            package_ref = str(application["workspace_ref"])
            validation_summary = (
                self.run_dir
                / f"evals/{candidate_id}/validation/candidate-run-summary.json"
            )
            if gate_path is None or not validation_summary.is_file():
                raise ValueError("frontier candidate lacks held-out Gate evidence")
            lineage_refs = tuple(
                self._ref(path)
                for path in sorted((self.run_dir / "branches").glob("*.json"))
                if candidate_id in self._load(path).get("candidate_chain", [])
            )
            if candidate_id in state.merge_candidate_ids:
                build = self.run_dir / "merge/build-record.json"
                if build.is_file():
                    lineage_refs = (*lineage_refs, self._ref(build))
            frontier.append(
                FrontierReportEntry(
                    candidate_id=candidate_id,
                    package_ref=package_ref,
                    provisional=not search_complete,
                    lineage_refs=lineage_refs,
                    patch_refs=(self._ref(patch_path),),
                    validation_summary_ref=self._ref(validation_summary),
                    gate_decision_ref=self._ref(gate_path),
                )
            )

        merge_path = self.run_dir / "merge/outcome.json"
        merge_outcome = (
            MergeOutcome.model_validate_json(merge_path.read_text(encoding="utf-8"))
            if merge_path.is_file()
            else MergeOutcome(
                status=MergeOutcomeStatus.NOT_REACHED_BUDGET_INCOMPLETE,
                considered_parent_candidate_ids=(),
                considered_parent_set_count=0,
                eligible_parent_set_count=0,
                rejected_parent_set_count=0,
                rejection_reason_counts={"not_reached": 1},
                cross_package_pair_count=0,
            )
        )
        checkpoint_paths = sorted((self.run_dir / "budget-checkpoints").glob("*.json"))
        decision_paths = sorted((self.run_dir / "continuation-decisions").glob("*.json"))
        latest_checkpoint = (
            self._load(checkpoint_paths[-1]) if checkpoint_paths else {}
        )
        pending = tuple(
            sorted(
                {
                    *latest_checkpoint.get("in_progress_work_ids", []),
                    *latest_checkpoint.get("not_exported_work_ids", []),
                    *(
                        work_id
                        for reservation in runtime_state.open_reservations
                        for work_id in reservation.unsettled_work_ids
                    ),
                }
            )
        )
        if outcome is EffectOutcome.BUDGET_INCOMPLETE and not pending:
            pending = ("pre_registered_search_not_closed",)
        runtime_path = self.run_dir / "scheduler/runtime-report.json"
        runtime = (
            self._load(runtime_path)
            if runtime_path.is_file()
            else {
                "session": runtime_state.model_dump(mode="json"),
                "active_clock": {
                    "active_wall_clock_ms": runtime_state.active_accumulated_ms,
                    "paused_ms": runtime_state.paused_accumulated_ms,
                    "cumulative_agent_duration_ms": runtime_state.cumulative_agent_duration_ms,
                },
            }
        )
        report_input = EvolutionOutcomeReportInput(
            run_id=state.run_id,
            package_id=self._load(self.run_dir / "seed-candidate.json")["package_id"],
            outcome=outcome,
            search_complete=search_complete,
            reference_summary={
                "reference_evidence_key_ref": self._ref(
                    self.run_dir / "reference-evidence-key.json"
                ),
                "reference_cache_audit_ref": self._ref(
                    self.run_dir / "reference-cache-audit.json"
                ),
            },
            candidates=tuple(candidates),
            deployable_frontier=tuple(frontier),
            merge_outcome=merge_outcome,
            gate_funnel={
                "proposed": len(candidate_ids),
                "train_admitted": sum(
                    self._load(path).get("passed") is True
                    for path in (self.run_dir / "train-admission").glob("*.json")
                ),
                "validation_completed": len(state.evaluated_candidate_ids),
                "deployable": len(state.deployable_candidate_ids),
            },
            rejected_memory_refs=(
                (self._ref(self.run_dir / "rejected.sqlite3"),)
                if (self.run_dir / "rejected.sqlite3").is_file()
                else ()
            ),
            runtime=runtime,
            budget_checkpoint_refs=tuple(self._ref(path) for path in checkpoint_paths),
            continuation_decision_refs=tuple(self._ref(path) for path in decision_paths),
            pending_work_ids=pending,
            evidence_gallery=self._accepted_gif_gallery(),
            process_evidence=self._process_evidence() if search_complete else {},
            provenance={
                "config_hash": state.config_hash,
                "run_lifecycle_ref": self._ref(self.run_dir / "run-lifecycle.json"),
                "merge_outcome_ref": self._ref(merge_path) if merge_path.is_file() else None,
            },
        )
        target = output or self.run_dir / "effect-outcome-report-input.json"
        resolved_target = target.resolve()
        if not resolved_target.is_relative_to(self.run_dir):
            raise ValueError("effect outcome input must remain inside its evolution run")
        atomic_write(
            resolved_target,
            canonical_json_bytes(report_input.model_dump(mode="json")),
        )
        return report_input


class ReferenceOutcomeCompiler:
    """Compile an honest zero-candidate report when the fresh reference stops early."""

    def __init__(self, repo: Path, run_dir: Path) -> None:
        self.repo = repo.resolve()
        self.run_dir = run_dir.resolve()
        if not self.run_dir.is_relative_to(self.repo):
            raise ValueError("reference outcome run must remain inside repository")

    def _ref(self, path: Path) -> str:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.repo):
            raise ValueError("reference evidence escapes repository")
        return resolved.relative_to(self.repo).as_posix()

    def compile(self, output: Path | None = None) -> EvolutionOutcomeReportInput:
        state = ActiveSessionState.model_validate_json(
            (self.run_dir / "runtime-session.json").read_text(encoding="utf-8")
        )
        if state.status is not RuntimeSessionStatus.STOPPED:
            raise ValueError("reference budget-incomplete report requires a stopped checkpoint")
        metadata = EvolutionOutcomeCompiler._load(self.run_dir / "run-metadata.json")
        e0 = EvolutionOutcomeCompiler._load(self.run_dir / "e0-package-record.json")
        checkpoint_paths = sorted((self.run_dir / "budget-checkpoints").glob("*.json"))
        decision_paths = sorted((self.run_dir / "continuation-decisions").glob("*.json"))
        latest = (
            EvolutionOutcomeCompiler._load(checkpoint_paths[-1])
            if checkpoint_paths
            else {}
        )
        pending = tuple(
            sorted(
                {
                    *latest.get("in_progress_work_ids", []),
                    *latest.get("not_exported_work_ids", []),
                    *(
                        work_id
                        for reservation in state.open_reservations
                        for work_id in reservation.unsettled_work_ids
                    ),
                }
            )
        ) or ("fresh_reference_not_sealed",)
        result = EvolutionOutcomeReportInput(
            run_id=state.run_id,
            package_id=str(e0["package_id"]),
            outcome=EffectOutcome.BUDGET_INCOMPLETE,
            search_complete=False,
            reference_summary={
                "run_metadata_ref": self._ref(self.run_dir / "run-metadata.json"),
                "ledger_snapshot_ref": self._ref(self.run_dir / "ledger-snapshot.json"),
                "executor_status": EvolutionOutcomeCompiler._load(
                    self.run_dir / "ledger-snapshot.json"
                ),
            },
            candidates=(),
            deployable_frontier=(),
            merge_outcome=MergeOutcome(
                status=MergeOutcomeStatus.NOT_REACHED_BUDGET_INCOMPLETE,
                considered_parent_candidate_ids=(),
                considered_parent_set_count=0,
                eligible_parent_set_count=0,
                rejected_parent_set_count=0,
                rejection_reason_counts={"reference_incomplete": 1},
                cross_package_pair_count=0,
            ),
            gate_funnel={
                "proposed": 0,
                "train_admitted": 0,
                "validation_completed": 0,
                "deployable": 0,
            },
            rejected_memory_refs=(),
            runtime={"session": state.model_dump(mode="json")},
            budget_checkpoint_refs=tuple(self._ref(path) for path in checkpoint_paths),
            continuation_decision_refs=tuple(self._ref(path) for path in decision_paths),
            pending_work_ids=pending,
            provenance={
                "source": "fresh_reference",
                "frozen_plan_hash": metadata["frozen_plan_hash"],
                "run_lifecycle_ref": self._ref(self.run_dir / "run-lifecycle.json"),
            },
        )
        target = output or self.run_dir / "effect-outcome-report-input.json"
        resolved = target.resolve()
        if not resolved.is_relative_to(self.run_dir):
            raise ValueError("reference outcome input must remain inside reference run")
        atomic_write(resolved, canonical_json_bytes(result.model_dump(mode="json")))
        return result
