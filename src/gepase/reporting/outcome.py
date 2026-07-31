"""Multi-outcome evolution reports in the existing GEPASE reporting subsystem."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field, model_validator

from gepase.evals.statistics import PairedScore
from gepase.mutation.schema import PackagePatch, PatchApplication
from gepase.optimizer.acceptance.engine import ValidationGatedAcceptance
from gepase.optimizer.acceptance.models import GateDecision
from gepase.optimizer.acceptance.validation import (
    RelativeEfficiencyEvidence,
    RelativeEfficiencyFrontierPoint,
    RelativeEfficiencyPolicy,
    build_relative_efficiency_policy,
    derive_relative_efficiency_evidence,
    rank_relative_efficiency_frontier,
)
from gepase.optimizer.candidate import PackageCandidate
from gepase.optimizer.merge.models import MergeOutcome, MergeOutcomeStatus
from gepase.optimizer.runtime import (
    EvolutionPhase,
    EvolutionRunState,
    R4EvolutionConfig,
    ReferenceEvidenceKey,
)
from gepase.optimizer.session_runtime import ActiveSessionState, RuntimeSessionStatus
from gepase.optimizer.status import CandidateStatus
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
    schema_version: Literal["1.0.0", "2.0.0"] = "2.0.0"
    report_mode: Literal["multi_outcome"] = "multi_outcome"
    presentation_mode: Literal["classic", "narrative_v1"] = "narrative_v1"
    report_id: str = Field(min_length=1)
    title_zh: str = Field(min_length=1)
    package_id: str = Field(min_length=1)
    outcome_input_ref: str | None = None
    outcome_input_refs: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def versioned_presentation_default(cls, value: Any) -> Any:
        """Keep historical v1 configs classic while new v2 configs are narrative."""

        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if (
            payload.get("schema_version", "2.0.0") == "1.0.0"
            and "presentation_mode" not in payload
        ):
            payload["presentation_mode"] = "classic"
        return payload

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
    validation_evidence_status: Literal[
        "scored", "evidence_incomplete", "not_run"
    ] = "not_run"
    incomplete_task_ids: tuple[str, ...] = ()
    v1_absolute_efficiency_diagnostic: dict[str, Any] | None = None
    relative_efficiency: dict[str, Any] | None = None
    pareto_layer: int | None = Field(default=None, ge=1)
    display_rank: int | None = Field(default=None, ge=1)

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
    schema_version: Literal["1.0.0", "2.0.0"] = "1.0.0"
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
    policy_evaluation: dict[str, Any] = Field(default_factory=dict)
    frontier_ranking: dict[str, Any] = Field(default_factory=dict)
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


_REASON_LABELS_ZH = {
    "all_gates_passed": "全部冻结 Gate 通过",
    "held_out_strict_improvement": "held-out 严格提升",
    "validation_evidence_incomplete": "验证证据不完整, 保持不可部署",
    "minibatch_regression": "训练集未达到严格改善",
    "validation_regression": "验证集发生回归",
    "protected_objective_regression": "保护目标发生回归",
    "category_regression": "任务类别保护线未通过",
    "high_risk_regression": "高风险任务保护线未通过",
    "extreme_relative_cost_regression": "相对资源成本达到极端退化线",
    "budget_incomplete": "运行在预算检查点停止",
}
_STATUS_LABELS_ZH = {
    "accepted": "可部署",
    "rejected": "验证拒绝",
    "train_rejected": "训练拒绝",
    "validation_evidence_incomplete": "验证证据不完整",
    "inconclusive": "证据不足",
    "provisional": "暂定结果",
}
_FUNNEL_LABELS_ZH = {
    "proposed": "已生成候选",
    "train_admitted": "通过训练 Gate",
    "validation_resolved": "验证已终态处理",
    "validation_completed": "验证证据完整",
    "validation_evidence_incomplete": "验证证据不完整",
    "deployable": "进入部署前沿",
}
_OBJECTIVE_LABELS_ZH = {
    "task_correctness": "任务正确性",
    "output_quality": "输出质量",
    "skill_gain": "Skill 增益",
    "reliability": "可靠性",
    "efficiency": "v1 绝对效率诊断",
    "package_quality": "Package 质量",
}
_AXIS_LABELS_ZH = {
    "duration_ms": "执行时长",
    "tool_calls": "工具调用",
    "tokens": "模型 Token",
    "artifact_size_bytes": "产物大小",
}


def _candidate_generations(candidates: list[dict[str, Any]]) -> dict[str, int]:
    ids = [str(row["candidate_id"]) for row in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("report presentation candidate ids must be unique")
    rows = {str(row["candidate_id"]): row for row in candidates}
    generations: dict[str, int] = {}

    def visit(candidate_id: str, trail: tuple[str, ...]) -> int:
        if candidate_id in generations:
            return generations[candidate_id]
        if candidate_id in trail:
            raise ValueError("report presentation candidate lineage contains a cycle")
        row = rows[candidate_id]
        parents = [str(value) for value in row.get("parent_ids", [])]
        if candidate_id in parents:
            raise ValueError("report presentation candidate cannot parent itself")
        internal = [parent for parent in parents if parent in rows]
        generation = (
            max(visit(parent, (*trail, candidate_id)) for parent in internal) + 1
            if internal
            else 1
        )
        generations[candidate_id] = generation
        return generation

    for candidate_id in ids:
        visit(candidate_id, ())
    return generations


def _display_bar(value: float | None, scale: float) -> dict[str, Any]:
    if value is None:
        return {"available": False, "side": "none", "percent": 0.0}
    return {
        "available": True,
        "side": "positive" if value >= 0 else "negative",
        "percent": min(abs(value) / scale * 48.0, 48.0),
    }


def build_outcome_presentation(data: dict[str, Any]) -> dict[str, Any]:
    """Derive a deterministic human-readable view without changing decisions."""

    candidates = [dict(row) for row in data.get("candidates", [])]
    generations = _candidate_generations(candidates)
    frontier_ids = {
        str(row["candidate_id"]) for row in data.get("deployable_frontier", [])
    }
    process = dict(data.get("process_evidence", {}))
    patches = list(process.get("patches", []))
    patch_by_ref = {
        str(row.get("patch_ref")): row
        for row in patches
        if row.get("patch_ref") is not None
    }
    graph_bindings = list(dict(process.get("package_graph", {})).get("bindings", []))
    graph_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for binding in graph_bindings:
        graph_by_parent[str(binding.get("parent_candidate_id"))].append(binding)

    aliases: dict[str, str] = {}
    generation_counts: dict[int, int] = defaultdict(int)
    merge_count = 0
    for row in candidates:
        candidate_id = str(row["candidate_id"])
        generation = generations[candidate_id]
        parents = list(row.get("parent_ids", []))
        if len(parents) > 1:
            merge_count += 1
            aliases[candidate_id] = (
                "合并候选" if merge_count == 1 else f"合并候选 {merge_count}"
            )
        else:
            generation_counts[generation] += 1
            suffix = chr(64 + generation_counts[generation])
            aliases[candidate_id] = f"第{generation}代 {suffix}"

    objective_values = [
        abs(float(value))
        for row in candidates
        for field in ("train_objective_deltas", "validation_objective_deltas")
        for value in (row.get(field) or {}).values()
    ]
    objective_scale = max([0.01, *objective_values])
    candidate_views: list[dict[str, Any]] = []
    for row in candidates:
        candidate_id = str(row["candidate_id"])
        parents = [str(value) for value in row.get("parent_ids", [])]
        generation = generations[candidate_id]
        patch_rows = [
            patch_by_ref[ref]
            for ref in (str(value) for value in row.get("patch_refs", []))
            if ref in patch_by_ref
        ]
        operations = [
            operation
            for patch in patch_rows
            for operation in patch.get("operations", [])
        ]
        modified_files = sorted(
            {str(operation["path"]) for operation in operations if operation.get("path")}
        )
        graph_rows = [
            binding for parent in parents for binding in graph_by_parent.get(parent, [])
        ]
        reasons = [str(value) for value in row.get("rejection_reasons", [])]
        status = str(row.get("gate_status", "inconclusive"))
        if len(parents) > 1:
            operator = "同 Package 多父 Merge"
        elif generation > 1:
            operator = "父代约束 refinement"
        elif "recovery" in status:
            operator = "恢复分支"
        else:
            operator = "初始分支"
        evidence_kinds: set[str] = set()
        for operation in operations:
            for reference in operation.get("evidence_refs", []):
                normalized = str(reference).lower()
                if "analyzer" in normalized:
                    evidence_kinds.add("Analyzer 失败证据")
                elif "reflection" in normalized or "generation2-feedback" in normalized:
                    evidence_kinds.add("父代训练反馈与 Reflection")
                else:
                    evidence_kinds.add("typed 任务证据")
        relative = dict(row.get("relative_efficiency") or {})
        axis_views = []
        for axis in relative.get("axis_aggregates", []):
            excluded = dict(axis.get("excluded_tasks", {}))
            axis_name = str(axis.get("axis", "unknown"))
            axis_views.append(
                {
                    "axis": axis_name,
                    "label_zh": _AXIS_LABELS_ZH.get(axis_name, axis_name),
                    "median_ratio": axis.get("median_ratio"),
                    "included_tasks": len(axis.get("task_ratios", {})),
                    "excluded_tasks": len(excluded),
                    "exclusion_reasons": sorted(set(str(v) for v in excluded.values())),
                }
            )
        train_objectives = dict(row.get("train_objective_deltas") or {})
        validation_objectives = dict(row.get("validation_objective_deltas") or {})
        objective_views = []
        for objective in sorted(OBJECTIVES):
            train_value = train_objectives.get(objective)
            validation_value = validation_objectives.get(objective)
            objective_views.append(
                {
                    "objective": objective,
                    "label_zh": _OBJECTIVE_LABELS_ZH[objective],
                    "train_delta": train_value,
                    "validation_delta": validation_value,
                    "train_bar": _display_bar(train_value, objective_scale),
                    "validation_bar": _display_bar(validation_value, objective_scale),
                }
            )
        mapped_accesses = sum(
            int(binding.get("mapped_access_events", 0)) for binding in graph_rows
        )
        candidate_views.append(
            {
                "candidate_id": candidate_id,
                "short_id": candidate_id[-12:],
                "alias_zh": aliases[candidate_id],
                "generation": generation,
                "parent_ids": parents,
                "parent_aliases_zh": [aliases.get(parent, "原始 Skill") for parent in parents],
                "operator_zh": operator,
                "status_code": status,
                "status_zh": _STATUS_LABELS_ZH.get(status, status),
                "status_tone": (
                    "accepted"
                    if candidate_id in frontier_ids
                    else "incomplete"
                    if "incomplete" in status
                    else "rejected"
                ),
                "reason_codes": reasons,
                "reasons_zh": [_REASON_LABELS_ZH.get(reason, reason) for reason in reasons],
                "is_deployable": candidate_id in frontier_ids,
                "train": {
                    "mean_delta": row.get("train_mean_delta"),
                    "wins": row.get("train_wins", 0),
                    "ties": row.get("train_ties", 0),
                    "losses": row.get("train_losses", 0),
                },
                "validation": {
                    "mean_delta": row.get("validation_mean_delta"),
                    "wins": row.get("validation_wins", 0),
                    "ties": row.get("validation_ties", 0),
                    "losses": row.get("validation_losses", 0),
                    "evidence_status": row.get("validation_evidence_status", "not_run"),
                },
                "objective_deltas": objective_views,
                "relative_efficiency": {
                    "availability": relative.get("availability", "unavailable"),
                    "relative_cost_ratio": relative.get("relative_cost_ratio"),
                    "relative_efficiency_score": relative.get("relative_efficiency_score"),
                    "axes": axis_views,
                },
                "pareto_layer": row.get("pareto_layer"),
                "display_rank": row.get("display_rank"),
                "modified_files": modified_files,
                "operation_count": len(operations),
                "patch_summary": "; ".join(
                    str(patch.get("summary", "")) for patch in patch_rows
                )
                or "没有可展示的 Patch 摘要",
                "operations": [
                    {
                        "op": operation.get("op"),
                        "path": operation.get("path"),
                        "target_node_id": operation.get("target_node_id"),
                        "rationale": operation.get("rationale"),
                        "expected_benefit": operation.get("expected_benefit"),
                        "regression_risk": operation.get("regression_risk"),
                        "evidence_refs": operation.get("evidence_refs", []),
                    }
                    for operation in operations
                ],
                "graph": {
                    "available": bool(graph_rows),
                    "binding_count": len(graph_rows),
                    "mapped_access_events": mapped_accesses,
                    "layer_counts": [
                        dict(binding.get("layer_counts", {})) for binding in graph_rows
                    ],
                    "target_nodes": sorted(
                        {
                            str(operation["target_node_id"])
                            for operation in operations
                            if operation.get("target_node_id")
                        }
                    ),
                },
                "causal_chain": [
                    {
                        "step": "失败与反馈",
                        "summary": "、".join(sorted(evidence_kinds))
                        or "没有可绑定的角色反馈",
                    },
                    {
                        "step": "图定位",
                        "summary": (
                            f"定位 {len(operations)} 个操作目标; 父代图映射 "
                            f"{mapped_accesses} 次访问"
                            if graph_rows
                            else "图证据不可用或该类型不需要 selector 图"
                        ),
                    },
                    {
                        "step": "PackagePatch",
                        "summary": (
                            f"修改 {len(modified_files)} 个文件、{len(operations)} 个操作"
                            if operations
                            else "没有 materialized Patch"
                        ),
                    },
                    {
                        "step": "评测",
                        "summary": (
                            f"train Δ {row.get('train_mean_delta')}; validation Δ "
                            f"{row.get('validation_mean_delta')}"
                        ),
                    },
                    {"step": "Gate", "summary": _STATUS_LABELS_ZH.get(status, status)},
                ],
                "technical_refs": {
                    "patch_refs": row.get("patch_refs", []),
                    "graph_path_refs": row.get("graph_path_refs", []),
                },
            }
        )

    candidate_views.sort(
        key=lambda row: (
            row["generation"],
            row["operator_zh"].startswith("同 Package"),
            row["alias_zh"],
        )
    )
    known = set(aliases)
    lineage_edges = [
        {
            "source": parent if parent in known else "original-skill",
            "target": row["candidate_id"],
            "kind": "merge" if len(row["parent_ids"]) > 1 else "mutation",
        }
        for row in candidate_views
        for parent in (row["parent_ids"] or ["original-skill"])
    ]
    lineage_nodes = [
        {
            "candidate_id": "original-skill",
            "alias_zh": "原始 Skill",
            "generation": 0,
            "status_tone": "root",
        },
        *[
            {
                "candidate_id": row["candidate_id"],
                "alias_zh": row["alias_zh"],
                "generation": row["generation"],
                "status_tone": row["status_tone"],
            }
            for row in candidate_views
        ],
    ]

    task_evidence = dict(data.get("task_evidence", {}))
    task_assets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    candidate_statuses = {
        row["candidate_id"]: row["status_zh"] for row in candidate_views
    }
    for asset in data.get("evidence_gallery", []):
        asset_row = dict(asset)
        candidate_id = asset_row.get("candidate_id")
        asset_row["candidate_alias_zh"] = aliases.get(str(candidate_id), "")
        asset_row["candidate_status_zh"] = (
            candidate_statuses.get(str(candidate_id), "证据状态不可用")
            if candidate_id
            else "Reference"
        )
        asset_row["typed_evidence"] = task_evidence.get(
            str(asset_row.get("asset_id")), {}
        )
        task_assets[
            (str(asset_row.get("split")), str(asset_row.get("task_id")))
        ].append(asset_row)
    task_groups = []
    split_counts: dict[str, int] = defaultdict(int)
    for (split, task_id), assets in sorted(
        task_assets.items(), key=lambda item: (item[0][0] != "validation", item[0][1])
    ):
        split_counts[split] += 1
        task_groups.append(
            {
                "task_id": task_id,
                "task_alias_zh": (
                    f"{'验证' if split == 'validation' else '训练'}任务 "
                    f"{split_counts[split]:02d}"
                ),
                "split": split,
                "default_open": split == "validation",
                "assets": sorted(
                    assets,
                    key=lambda item: (
                        {"no-skill": 0, "original": 1, "candidate": 2}.get(
                            str(item.get("variant")), 3
                        ),
                        item.get("candidate_alias_zh", ""),
                    ),
                ),
            }
        )

    policy = dict(data.get("policy_evaluation", {}))
    threshold = float(policy.get("max_relative_cost_ratio", 2.0))
    scatter_rows = [
        row
        for row in candidate_views
        if row["validation"]["mean_delta"] is not None
        and row["relative_efficiency"]["relative_cost_ratio"] is not None
    ]
    x_values = [
        float(row["relative_efficiency"]["relative_cost_ratio"])
        for row in scatter_rows
    ]
    y_values = [float(row["validation"]["mean_delta"]) for row in scatter_rows]
    x_max = max([threshold * 1.1, 1.1, *[value * 1.05 for value in x_values]])
    y_min = min([0.0, *y_values])
    y_max = max([0.01, *y_values])
    y_span = max(y_max - y_min, 0.01)
    for row in scatter_rows:
        ratio = float(row["relative_efficiency"]["relative_cost_ratio"])
        delta = float(row["validation"]["mean_delta"])
        row["scatter"] = {
            "x_percent": 8.0 + ratio / x_max * 84.0,
            "y_percent": 8.0 + (delta - y_min) / y_span * 84.0,
        }

    ranked = sorted(
        (row for row in candidate_views if row["display_rank"] is not None),
        key=lambda row: int(row["display_rank"]),
    )
    runtime = dict(data.get("runtime", {}))
    usage = dict(runtime.get("usage", {}))
    return {
        "schema_version": "1.0.0",
        "outcome": {
            "code": data.get("outcome"),
            "label_zh": {
                "strict_improvement": "找到可部署的严格提升",
                "no_strict_improvement": "本轮未找到严格提升",
                "budget_incomplete": "搜索在预算检查点暂停",
            }.get(str(data.get("outcome")), str(data.get("outcome"))),
            "boundary_zh": data.get("claim_boundary_zh"),
            "zero_agent_replay": policy.get("evidence_mode")
            == "sealed_source_zero_agent_replay",
        },
        "headline": {
            "proposed": len(candidate_views),
            "deployable": len(frontier_ids),
            "first": ranked[0] if ranked else None,
            "second": ranked[1] if len(ranked) > 1 else None,
        },
        "funnel": [
            {
                "key": key,
                "label_zh": _FUNNEL_LABELS_ZH.get(key, key),
                "count": value,
            }
            for key, value in data.get("gate_funnel", {}).items()
        ],
        "lineage": {"nodes": lineage_nodes, "edges": lineage_edges},
        "candidates": candidate_views,
        "tasks": task_groups,
        "charts": {
            "objective_scale": objective_scale,
            "scatter": {
                "x_max": x_max,
                "y_min": y_min,
                "y_max": y_max,
                "original_ratio": 1.0,
                "extreme_ratio": threshold,
                "points": scatter_rows,
            },
        },
        "package": {
            "graph_available": bool(graph_bindings),
            "graph_binding_count": len(graph_bindings),
            "modified_files": sorted(
                {path for row in candidate_views for path in row["modified_files"]}
            ),
            "target_node_count": len(
                {
                    node
                    for row in candidate_views
                    for node in row["graph"]["target_nodes"]
                }
            ),
        },
        "runtime": {
            "agent_calls": usage.get("agent_calls"),
            "estimated_tokens": usage.get("estimated_tokens"),
            "active_wall_clock_ms": runtime.get("active_wall_clock_ms"),
            "repairs": usage.get("repairs"),
            "proposals": usage.get("proposals"),
            "candidates": usage.get("candidates"),
        },
        "evidence": {
            "policy_id": policy.get("policy_id"),
            "policy_hash": policy.get("policy_hash"),
            "outcome_input_ref": data.get("outcome_input_ref"),
        },
    }


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

    def _load_report_json_ref(self, reference: str) -> dict[str, Any]:
        path = Path(reference)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("narrative report evidence reference is not relative")
        resolved = (self.repo / path).resolve(strict=True)
        if not resolved.is_relative_to(self.repo):
            raise ValueError("narrative report evidence escapes repository")
        value = json.loads(resolved.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("narrative report evidence must be a JSON object")
        return value

    def _narrative_task_evidence(
        self,
        outcome: EvolutionOutcomeReportInput,
        assets: tuple[ReportEvidenceAsset, ...],
    ) -> dict[str, dict[str, Any]]:
        source_ref = outcome.provenance.get("source_run_ref")
        if not isinstance(source_ref, str):
            return {}
        source_path = Path(source_ref)
        if source_path.is_absolute() or ".." in source_path.parts:
            raise ValueError("narrative source run reference is not relative")
        source_run = (self.repo / source_path).resolve(strict=True)
        if not source_run.is_relative_to(self.repo) or not source_run.is_dir():
            raise ValueError("narrative source run is invalid")
        source_verification = ArtifactStore(source_run).verify()
        if not source_verification.valid or source_verification.unindexed_files:
            raise ValueError("narrative source run is not fully sealed")
        resolved_config = json.loads(
            (source_run / "resolved-config.json").read_text(encoding="utf-8")
        )
        reference_path = Path(str(resolved_config["reference_run_ref"]))
        if reference_path.is_absolute() or ".." in reference_path.parts:
            raise ValueError("narrative reference run reference is not relative")
        reference_run = (self.repo / reference_path).resolve(strict=True)
        if not reference_run.is_relative_to(self.repo) or not reference_run.is_dir():
            raise ValueError("narrative reference run is invalid")
        reference_verification = ArtifactStore(reference_run).verify()
        if not reference_verification.valid or reference_verification.unindexed_files:
            raise ValueError("narrative reference run is not fully sealed")

        vector_index: dict[tuple[str, str, str | None], tuple[Path, dict[str, Any]]] = {}
        vector_paths = sorted(reference_run.glob("task-score-vectors/*.json"))
        vector_paths.extend(
            sorted(source_run.glob("evals/candidate-*/*/task-score-vectors/*.json"))
        )
        for vector_path in vector_paths:
            vector = json.loads(vector_path.read_text(encoding="utf-8"))
            candidate_id: str | None = None
            if vector.get("variant") == "candidate":
                relative = vector_path.relative_to(source_run).parts
                candidate_id = relative[1] if len(relative) > 1 else None
            key = (
                str(vector.get("task_id")),
                str(vector.get("variant")),
                candidate_id,
            )
            if key in vector_index:
                raise ValueError("narrative task evidence vector identity is ambiguous")
            vector_index[key] = (vector_path, vector)

        projected: dict[str, dict[str, Any]] = {}
        for asset in assets:
            key = (asset.task_id, asset.variant, asset.candidate_id)
            indexed = vector_index.get(key)
            if indexed is None:
                projected[asset.asset_id] = {
                    "available": False,
                    "reason_zh": "找不到与该产物唯一绑定的 TaskScoreVector",
                }
                continue
            vector_path, vector = indexed
            deterministic: dict[str, Any] | None = None
            grader: dict[str, Any] | None = None
            comparator: dict[str, Any] | None = None
            execution: dict[str, Any] | None = None
            for reference in vector.get("evidence_refs", []):
                value = self._load_report_json_ref(str(reference))
                if "assertion_results" in value and "weighted_score" in value:
                    deterministic = value
                elif "overall_score" in value and "grader_work_id" in value:
                    grader = value
                elif "ab_candidate_outcome" in value and "ba_candidate_outcome" in value:
                    comparator = value
                elif value.get("evidence_tier") == "E2":
                    execution = value
            work_path = vector_path.parent.parent / "work-items" / f"{vector_path.stem}.json"
            work = (
                json.loads(work_path.read_text(encoding="utf-8"))
                if work_path.is_file()
                else {}
            )
            assertions = list((deterministic or {}).get("assertion_results", []))
            projected[asset.asset_id] = {
                "available": True,
                "vector_ref": vector_path.relative_to(self.repo).as_posix(),
                "prompt_zh": work.get("prompt"),
                "requested_output": work.get("requested_output"),
                "score_vector": {
                    objective: vector.get(objective) for objective in sorted(OBJECTIVES)
                },
                "deterministic": {
                    "score": (deterministic or {}).get("weighted_score"),
                    "passed": sum(bool(item.get("passed")) for item in assertions),
                    "total": len(assertions),
                    "assertions": [
                        {
                            "assertion_id": item.get("assertion_id"),
                            "family": item.get("family"),
                            "passed": item.get("passed"),
                            "detail": item.get("detail"),
                        }
                        for item in assertions
                    ],
                },
                "grader": {
                    "score": (grader or {}).get("overall_score"),
                    "feedback_zh": (grader or {}).get("feedback_zh"),
                },
                "comparator": (
                    {
                        "ab_candidate_outcome": comparator.get("ab_candidate_outcome"),
                        "ba_candidate_outcome": comparator.get("ba_candidate_outcome"),
                        "consistent": comparator.get("consistent"),
                    }
                    if comparator is not None
                    else None
                ),
                "usage": (execution or {}).get("usage"),
            }
        return projected

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
            **(
                {
                    "policy_evaluation": outcome.policy_evaluation,
                    "frontier_ranking": outcome.frontier_ranking,
                }
                if outcome.policy_evaluation
                else {}
            ),
            "provenance": outcome.provenance,
            "claim_boundary_zh": (
                (
                    "执行证据来自已封存的 source run。相对效率 v2 是运行完成后的通用"
                    " policy calibration。本报告为零 Agent policy replay。它不是新的"
                    "独立 Agent 实验。"
                    if outcome.policy_evaluation.get("policy_id")
                    == "relative_efficiency_v2"
                    else "本报告只陈述当前 sealed run 的完整证据。"
                )
                if outcome.search_complete
                else "本轮预注册搜索尚未收口; 已验证条目仅作 provisional evidence。"
            ),
        }
        if self.config.presentation_mode == "narrative_v1":
            data["task_evidence"] = self._narrative_task_evidence(
                outcome, gallery_assets
            )
            data["presentation"] = build_outcome_presentation(data)
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
        if expected.get("presentation"):
            required_sections = [
                "overview",
                "process",
                "candidates",
                "scores",
                "tasks",
                "package",
                "runtime",
                "evidence",
            ]
            if "aria-label=\"报告目录\"" not in html:
                errors.append("narrative HTML navigation lacks accessible label")
            if "loading=\"lazy\"" not in html and expected["evidence_gallery"]:
                errors.append("narrative task-native images are not lazy loaded")
            if "https://" in html or "http://" in html:
                errors.append("narrative HTML must remain network independent")
        else:
            required_sections = [
                "outcome",
                "deployable",
                "evidence",
                "candidates",
                "merge",
                "process",
                "runtime",
            ]
            if expected.get("policy_evaluation"):
                required_sections.append("efficiency")
        for section in required_sections:
            if f'id="{section}"' not in html:
                errors.append(f"HTML section missing: {section}")
        return {
            "valid": not errors,
            "outcome": expected["outcome"],
            "frontier_count": expected["frontier_count"],
            "presentation_mode": self.config.presentation_mode,
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
        summary = self._load(summary_path)
        pair_summaries = summary.get("pair_summaries", [])
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
            (
                sum(deltas) / len(deltas)
                if deltas and summary.get("status", "scored") == "scored"
                else None
            ),
            objectives if summary.get("status", "scored") == "scored" else None,
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
            candidate_id: str | None = None
            if variant == "candidate":
                relative_parts = vector_path.relative_to(self.run_dir).parts
                if len(relative_parts) < 2 or relative_parts[0] != "evals":
                    raise ValueError("candidate gallery vector is outside candidate evals")
                candidate_id = relative_parts[1]
            eval_root = vector_path.parent.parent
            work = self._load(eval_root / "work-items" / f"{vector_path.stem}.json")
            raw_split = str(work.get("split"))
            if raw_split not in {"train", "validation"}:
                raise ValueError("report gallery work item has invalid split")
            split = cast(Literal["train", "validation"], raw_split)
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
        if not binding_paths:
            raise ValueError("complete report requires at least one selector graph binding")
        graph_bindings = []
        for path in binding_paths:
            binding = self._load(path)
            graph_bindings.append(
                {
                    "binding_ref": self._ref(path),
                    "mode": binding["mode"],
                    "layer_counts": binding["layer_counts"],
                    "mapped_access_events": binding["mapped_access_events"],
                    "accepted_work_ids": binding["accepted_work_ids"],
                    "filtered_work_ids": binding["filtered_work_ids"],
                    "semantic_hypothesis_edges": binding["semantic_hypothesis_edges"],
                    "selector_graph_ref": binding["selector_graph_ref"],
                    "parent_candidate_id": path.parents[1].name,
                }
            )
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
                "binding_count": len(graph_bindings),
                "bindings": graph_bindings,
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

        resolved_config = self._load(self.run_dir / "resolved-config.json")
        relative_mode = (
            resolved_config.get("schema_version") == "2.0.0"
            and resolved_config.get("efficiency_policy_mode") == "relative_v2"
        )
        relative_policy = (
            RelativeEfficiencyPolicy.model_validate(
                resolved_config.get("relative_efficiency_policy")
            )
            if relative_mode
            else None
        )
        relative_evidence_by_candidate: dict[str, dict[str, Any]] = {}

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
            resolution_path = self.run_dir / f"validation-resolutions/{candidate_id}.json"
            resolution = self._load(resolution_path) if resolution_path.is_file() else None
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
            if resolution is not None:
                if gate is not None:
                    raise ValueError(
                        "candidate cannot have both a GateDecision and incomplete resolution"
                    )
                reason_codes = ("validation_evidence_incomplete",)
            patch_path = self.run_dir / f"candidates/{candidate_id}/patch.json"
            graph_path = self.run_dir / f"candidates/{candidate_id}/graph.json"
            relative_evidence_path = (
                self.run_dir / f"relative-efficiency-evidence/{candidate_id}.json"
            )
            relative_evidence = None
            if relative_evidence_path.is_file():
                if not relative_mode:
                    raise ValueError("v1 run unexpectedly contains active v2 Gate evidence")
                typed_relative = RelativeEfficiencyEvidence.model_validate_json(
                    relative_evidence_path.read_text(encoding="utf-8")
                )
                assert relative_policy is not None
                if typed_relative.policy_hash != relative_policy.policy_hash:
                    raise ValueError("candidate relative-efficiency evidence uses another policy")
                relative_evidence = {
                    "evidence_ref": self._ref(relative_evidence_path),
                    **typed_relative.model_dump(mode="json"),
                }
                relative_evidence_by_candidate[candidate_id] = relative_evidence
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
                    gate_status=(
                        "validation_evidence_incomplete"
                        if resolution is not None
                        else str((gate or {}).get("verdict", "not_reached"))
                    ),
                    rejection_reasons=reason_codes,
                    validation_evidence_status=(
                        "evidence_incomplete"
                        if resolution is not None
                        else "scored"
                        if gate is not None and gate.get("validation_pairs")
                        else "not_run"
                    ),
                    incomplete_task_ids=(
                        tuple(
                            sorted(
                                str(item["task_id"])
                                for item in resolution.get("incomplete_cases", [])
                            )
                        )
                        if resolution is not None
                        else ()
                    ),
                    relative_efficiency=relative_evidence,
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

        frontier_ranking: dict[str, Any] = {}
        policy_evaluation: dict[str, Any] = {}
        if relative_mode:
            assert relative_policy is not None
            points: list[RelativeEfficiencyFrontierPoint] = []
            candidate_by_id = {item.candidate_id: item for item in candidates}
            for candidate_id in state.deployable_candidate_ids:
                row = candidate_by_id[candidate_id]
                evidence = relative_evidence_by_candidate.get(candidate_id)
                if row.validation_mean_delta is None or evidence is None:
                    raise ValueError("v2 frontier candidate lacks relative held-out evidence")
                points.append(
                    RelativeEfficiencyFrontierPoint(
                        candidate_id=candidate_id,
                        validation_primary_delta=row.validation_mean_delta,
                        relative_efficiency_score=evidence["relative_efficiency_score"],
                    )
                )
            ranking = rank_relative_efficiency_frontier(tuple(points))
            rank_by_id = {item.candidate_id: item for item in ranking.ranks}
            candidates = [
                item.model_copy(
                    update={
                        "pareto_layer": rank_by_id[item.candidate_id].pareto_layer,
                        "display_rank": rank_by_id[item.candidate_id].display_rank,
                    }
                )
                if item.candidate_id in rank_by_id
                else item
                for item in candidates
            ]
            frontier.sort(key=lambda item: rank_by_id[item.candidate_id].display_rank)
            frontier_ranking = ranking.model_dump(mode="json")
            policy_evaluation = {
                **relative_policy.model_dump(mode="json"),
                "policy_ref": self._ref(
                    self.run_dir / "relative-efficiency-policy.json"
                ),
                "evidence_mode": "active_validation_gate",
                "reference_variant": "original",
            }

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
        policy_provenance = (
            {
                "efficiency_policy_mode": resolved_config["efficiency_policy_mode"],
                "relative_efficiency_policy": resolved_config.get(
                    "relative_efficiency_policy"
                ),
            }
            if resolved_config.get("schema_version") == "2.0.0"
            else {}
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
                "validation_evidence_incomplete": len(
                    state.validation_incomplete_candidate_ids
                ),
                "validation_resolved": len(state.evaluated_candidate_ids)
                + len(state.validation_incomplete_candidate_ids),
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
            policy_evaluation=policy_evaluation,
            frontier_ranking=frontier_ranking,
            provenance={
                "config_hash": state.config_hash,
                "run_lifecycle_ref": self._ref(self.run_dir / "run-lifecycle.json"),
                "merge_outcome_ref": self._ref(merge_path) if merge_path.is_file() else None,
                **policy_provenance,
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


class RelativeEfficiencyReplayCompiler:
    """Re-evaluate one sealed evolution with the existing Gate path and v2 costs."""

    def __init__(self, repo: Path, run_dir: Path) -> None:
        self.repo = repo.resolve()
        self.run_dir = run_dir.resolve()
        if not self.run_dir.is_relative_to(self.repo):
            raise ValueError("relative-efficiency replay source must remain inside repository")

    def _ref(self, path: Path) -> str:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.repo):
            raise ValueError("relative-efficiency replay evidence escapes repository")
        return resolved.relative_to(self.repo).as_posix()

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"expected JSON object: {path}")
        return value

    def _candidate(self, candidate_id: str) -> PackageCandidate:
        seed = self.run_dir / "seed-candidate.json"
        if seed.is_file():
            value = PackageCandidate.model_validate_json(seed.read_text(encoding="utf-8"))
            if value.candidate_id == candidate_id:
                return value
        path = self.run_dir / f"candidates/{candidate_id}/candidate.json"
        return PackageCandidate.model_validate_json(path.read_text(encoding="utf-8"))

    def _pairs(self, candidate_id: str, split: str) -> tuple[PairedScore, ...]:
        payload = self._load(
            self.run_dir / f"evals/{candidate_id}/{split}/paired-scores.json"
        )
        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValueError(f"sealed {split} paired scores are missing: {candidate_id}")
        return tuple(PairedScore.model_validate(item) for item in rows)

    def _lineage_refs(self, candidate_id: str) -> tuple[str, ...]:
        refs = tuple(
            self._ref(path)
            for path in sorted((self.run_dir / "branches").glob("*.json"))
            if candidate_id in self._load(path).get("candidate_chain", [])
        )
        state = EvolutionRunState.model_validate_json(
            (self.run_dir / "evolution-state.json").read_text(encoding="utf-8")
        )
        if candidate_id in state.merge_candidate_ids:
            build = self.run_dir / "merge/build-record.json"
            if build.is_file():
                refs = (*refs, self._ref(build))
        return refs

    def compile(
        self,
        output_dir: Path,
        *,
        policy: RelativeEfficiencyPolicy | None = None,
    ) -> EvolutionOutcomeReportInput:
        output = output_dir.resolve()
        if output.exists():
            raise FileExistsError(f"relative-efficiency replay output exists: {output}")
        if not output.is_relative_to(self.repo) or output.is_relative_to(self.run_dir):
            raise ValueError("relative-efficiency replay output must be outside the source run")

        source_verification = ArtifactStore(self.run_dir).verify_complete()
        source_index = self.run_dir / "artifact-index.json"
        source_index_hash = sha256_bytes(source_index.read_bytes())
        state = EvolutionRunState.model_validate_json(
            (self.run_dir / "evolution-state.json").read_text(encoding="utf-8")
        )
        if state.phase is not EvolutionPhase.COMPLETE:
            raise ValueError("relative-efficiency replay requires a COMPLETE sealed source")
        runtime_state = ActiveSessionState.model_validate_json(
            (self.run_dir / "runtime-session.json").read_text(encoding="utf-8")
        )
        config = R4EvolutionConfig.model_validate_json(
            (self.run_dir / "resolved-config.json").read_text(encoding="utf-8")
        )
        reference_key = ReferenceEvidenceKey.model_validate_json(
            (self.run_dir / "reference-evidence-key.json").read_text(encoding="utf-8")
        )
        reference_root = (self.repo / reference_key.reference_run_ref).resolve(strict=True)
        reference_verification = ArtifactStore(reference_root).verify_complete()
        reference_index = reference_root / "artifact-index.json"
        if sha256_bytes(reference_index.read_bytes()) != (
            reference_key.source_run_artifact_index_hash
        ):
            raise ValueError("sealed Reference index differs from ReferenceEvidenceKey")
        if reference_key.reference_run_ref != config.reference_run_ref:
            raise ValueError("Controller config and ReferenceEvidenceKey disagree")

        v1_path = self.run_dir / "effect-outcome-report-input.json"
        v1 = EvolutionOutcomeReportInput.model_validate_json(
            v1_path.read_text(encoding="utf-8")
        )
        if not v1.search_complete or v1.run_id != state.run_id:
            raise ValueError("v1 outcome is not the complete sealed source run")
        candidate_ids = tuple(
            dict.fromkeys((*state.branch_candidate_ids, *state.merge_candidate_ids))
        )
        v1_rows = {item.candidate_id: item for item in v1.candidates}
        if set(v1_rows) != set(candidate_ids):
            raise ValueError("v1 outcome candidate set differs from source evolution")

        selected_policy = policy or build_relative_efficiency_policy()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
        final_root_ref = output.relative_to(self.repo).as_posix()
        store = ArtifactStore(temporary)
        policy_ref = f"{final_root_ref}/relative-efficiency-policy-v2.json"
        store.write_json(
            "relative-efficiency-policy-v2.json",
            selected_policy.model_dump(mode="json"),
        )
        decisions: dict[str, GateDecision] = {}
        evidence_by_candidate: dict[str, dict[str, Any]] = {}
        candidate_rows: list[CandidateOutcomeRow] = []
        accepted_ids: list[str] = []
        try:
            acceptance = ValidationGatedAcceptance(
                self.repo,
                temporary / "acceptance",
                run_id=f"{state.run_id}-relative-efficiency-v2",
            )
            for candidate_id in candidate_ids:
                base_row = v1_rows[candidate_id]
                admission_path = self.run_dir / f"train-admission/{candidate_id}.json"
                admission = self._load(admission_path)
                diagnostic_path = self.run_dir / f"secondary-objectives/{candidate_id}.json"
                diagnostic = (
                    {
                        "label_zh": "v1 绝对预算诊断",
                        "source_ref": self._ref(diagnostic_path),
                        **self._load(diagnostic_path),
                    }
                    if diagnostic_path.is_file()
                    else None
                )
                if admission.get("passed") is not True:
                    train_gate = admission.get("gate")
                    train_reasons = (
                        tuple(str(item) for item in train_gate.get("reason_codes", []))
                        if isinstance(train_gate, dict)
                        else base_row.rejection_reasons
                    )
                    candidate_rows.append(
                        base_row.model_copy(
                            update={
                                "gate_status": "train_rejected",
                                "rejection_reasons": train_reasons,
                                "validation_evidence_status": "not_run",
                                "v1_absolute_efficiency_diagnostic": diagnostic,
                            }
                        )
                    )
                    continue
                resolution_path = (
                    self.run_dir / f"validation-resolutions/{candidate_id}.json"
                )
                if resolution_path.is_file():
                    candidate_rows.append(
                        base_row.model_copy(
                            update={"v1_absolute_efficiency_diagnostic": diagnostic}
                        )
                    )
                    continue

                train_pairs = self._pairs(candidate_id, "train")
                validation_pairs = self._pairs(candidate_id, "validation")
                evidence = derive_relative_efficiency_evidence(
                    self.repo,
                    validation_pairs,
                    candidate_id=candidate_id,
                    reference_run_ref=reference_key.reference_run_ref,
                    reference_key_hash=reference_key.key_hash,
                    policy=selected_policy,
                )
                evidence_relative = f"relative-efficiency-evidence/{candidate_id}.json"
                evidence_ref = f"{final_root_ref}/{evidence_relative}"
                store.write_json(evidence_relative, evidence.model_dump(mode="json"))
                candidate = self._candidate(candidate_id)
                patch = PackagePatch.model_validate_json(
                    (
                        self.run_dir / f"candidates/{candidate_id}/patch.json"
                    ).read_text(encoding="utf-8")
                )
                application = PatchApplication.model_validate_json(
                    (
                        self.run_dir / f"candidates/{candidate_id}/application.json"
                    ).read_text(encoding="utf-8")
                )
                parent = self._candidate(application.parent_candidate_id)
                decision = acceptance.evaluate(
                    parent,
                    candidate,
                    patch,
                    application,
                    train_pairs=train_pairs,
                    validation_pairs=validation_pairs,
                    efficiency_regression=0.0,
                    complexity_regression=0.0,
                    secondary_evidence_refs=(policy_ref, evidence_ref),
                    relative_efficiency_policy=selected_policy,
                    relative_efficiency_evidence=evidence,
                    minibatch_policy=config.train_policy,
                    validation_policy=config.validation_policy,
                    static_regression_aware=True,
                    record_evolution_candidate=False,
                )
                decisions[candidate_id] = decision
                if decision.verdict is CandidateStatus.ACCEPTED:
                    accepted_ids.append(candidate_id)
                evidence_by_candidate[candidate_id] = {
                    "evidence_ref": evidence_ref,
                    **evidence.model_dump(mode="json"),
                }
                candidate_rows.append(
                    base_row.model_copy(
                        update={
                            "gate_status": decision.verdict.value,
                            "rejection_reasons": decision.reason_codes,
                            "v1_absolute_efficiency_diagnostic": diagnostic,
                            "relative_efficiency": evidence_by_candidate[candidate_id],
                        }
                    )
                )

            point_rows: list[RelativeEfficiencyFrontierPoint] = []
            for candidate_id in accepted_ids:
                validation_delta = v1_rows[candidate_id].validation_mean_delta
                if validation_delta is None:
                    continue
                point_rows.append(
                    RelativeEfficiencyFrontierPoint(
                        candidate_id=candidate_id,
                        validation_primary_delta=validation_delta,
                        relative_efficiency_score=(
                            evidence_by_candidate[candidate_id][
                                "relative_efficiency_score"
                            ]
                        ),
                    )
                )
            points = tuple(point_rows)
            if len(points) != len(accepted_ids):
                raise ValueError("deployable candidate lacks complete validation statistics")
            ranking = rank_relative_efficiency_frontier(points)
            rank_by_candidate = {item.candidate_id: item for item in ranking.ranks}
            candidate_rows = [
                row.model_copy(
                    update={
                        "pareto_layer": rank_by_candidate[row.candidate_id].pareto_layer,
                        "display_rank": rank_by_candidate[row.candidate_id].display_rank,
                    }
                )
                if row.candidate_id in rank_by_candidate
                else row
                for row in candidate_rows
            ]
            frontier: list[FrontierReportEntry] = []
            for ranked in ranking.ranks:
                candidate_id = ranked.candidate_id
                application = self._load(
                    self.run_dir / f"candidates/{candidate_id}/application.json"
                )
                decision = decisions[candidate_id]
                gate_path = (
                    output
                    / "acceptance/gate-decisions"
                    / f"{decision.decision_id}.json"
                )
                frontier.append(
                    FrontierReportEntry(
                        candidate_id=candidate_id,
                        package_ref=str(application["workspace_ref"]),
                        provisional=False,
                        lineage_refs=self._lineage_refs(candidate_id),
                        patch_refs=(
                            self._ref(
                                self.run_dir / f"candidates/{candidate_id}/patch.json"
                            ),
                        ),
                        validation_summary_ref=self._ref(
                            self.run_dir
                            / f"evals/{candidate_id}/validation/candidate-run-summary.json"
                        ),
                        gate_decision_ref=gate_path.relative_to(self.repo).as_posix(),
                    )
                )

            outcome = EvolutionOutcomeReportInput(
                schema_version="2.0.0",
                run_id=f"{state.run_id}-relative-efficiency-v2-replay",
                package_id=v1.package_id,
                outcome=(
                    EffectOutcome.STRICT_IMPROVEMENT
                    if frontier
                    else EffectOutcome.NO_STRICT_IMPROVEMENT
                ),
                search_complete=True,
                reference_summary=v1.reference_summary,
                candidates=tuple(candidate_rows),
                deployable_frontier=tuple(frontier),
                merge_outcome=v1.merge_outcome,
                gate_funnel={
                    **v1.gate_funnel,
                    "deployable": len(frontier),
                },
                rejected_memory_refs=tuple(
                    dict.fromkeys(
                        (
                            *v1.rejected_memory_refs,
                            f"{final_root_ref}/acceptance/rejected.sqlite3",
                        )
                    )
                ),
                runtime=v1.runtime,
                budget_checkpoint_refs=v1.budget_checkpoint_refs,
                continuation_decision_refs=v1.continuation_decision_refs,
                pending_work_ids=(),
                evidence_gallery=v1.evidence_gallery,
                process_evidence=v1.process_evidence,
                policy_evaluation={
                    "schema_version": "2.0.0",
                    "policy_id": selected_policy.policy_id,
                    "policy_hash": selected_policy.policy_hash,
                    "policy_ref": policy_ref,
                    "comparable_axes": list(selected_policy.comparable_axes),
                    "artifact_size_mode": selected_policy.artifact_size_mode,
                    "aggregate": selected_policy.aggregate,
                    "score_mapping": selected_policy.score_mapping,
                    "max_relative_cost_ratio": selected_policy.max_relative_cost_ratio,
                    "frontier_method": selected_policy.frontier_method,
                    "frontier_tie_break": list(selected_policy.frontier_tie_break),
                    "unknown_efficiency_policy": (
                        selected_policy.unknown_efficiency_policy
                    ),
                    "source_policy_version": "v1_frozen_original",
                    "calibration_timing": "post_run_user_confirmed",
                    "evidence_mode": "sealed_source_zero_agent_replay",
                    "reference_variant": "original",
                    "reference_key_hash": reference_key.key_hash,
                },
                frontier_ranking=ranking.model_dump(mode="json"),
                provenance={
                    **v1.provenance,
                    "source_run_ref": self._ref(self.run_dir),
                    "source_root_artifact_index_sha256": source_index_hash,
                    "source_v1_outcome_ref": self._ref(v1_path),
                    "source_v1_outcome_sha256": sha256_bytes(v1_path.read_bytes()),
                    "relative_efficiency_policy_ref": policy_ref,
                    "relative_efficiency_policy_hash": selected_policy.policy_hash,
                    "policy_replay": True,
                    "policy_replay_agent_calls": 0,
                    "policy_replay_api_calls": 0,
                    "new_evaluations": 0,
                    "new_candidates": 0,
                    "new_patches": 0,
                },
            )
            store.write_json("frontier-ranking.json", ranking.model_dump(mode="json"))
            store.write_json(
                "deployable-frontier.json",
                {
                    "schema_version": "2.0.0",
                    "candidate_ids": [item.candidate_id for item in frontier],
                    "entries": [item.model_dump(mode="json") for item in frontier],
                },
            )
            store.write_json("outcome-input.json", outcome.model_dump(mode="json"))
            store.write_json(
                "replay-audit.json",
                {
                    "schema_version": "2.0.0",
                    "source_run_ref": self._ref(self.run_dir),
                    "source_artifact_verification": source_verification.as_dict(),
                    "source_root_artifact_index_sha256_before": source_index_hash,
                    "source_root_artifact_index_sha256_after": sha256_bytes(
                        source_index.read_bytes()
                    ),
                    "reference_artifact_verification": reference_verification.as_dict(),
                    "reference_artifact_index_sha256": sha256_bytes(
                        reference_index.read_bytes()
                    ),
                    "v1_outcome_ref": self._ref(v1_path),
                    "v1_outcome_sha256": sha256_bytes(v1_path.read_bytes()),
                    "policy_hash": selected_policy.policy_hash,
                    "candidate_ids": list(candidate_ids),
                    "decision_candidate_ids": sorted(decisions),
                    "validation_incomplete_candidate_ids": list(
                        state.validation_incomplete_candidate_ids
                    ),
                    "train_rejected_candidate_ids": sorted(
                        candidate_id
                        for candidate_id in candidate_ids
                        if self._load(
                            self.run_dir / f"train-admission/{candidate_id}.json"
                        ).get("passed")
                        is not True
                    ),
                    "deployable_candidate_ids": [
                        item.candidate_id for item in frontier
                    ],
                    "source_runtime_agent_calls": runtime_state.used.agent_calls,
                    "policy_replay_agent_calls": 0,
                    "policy_replay_api_calls": 0,
                    "new_evaluations": 0,
                    "new_candidates": 0,
                    "new_patches": 0,
                    "source_bytes_modified": False,
                },
            )
            for path in sorted(temporary.rglob("*")):
                if path.is_file() and path.name != "artifact-index.json":
                    relative = path.relative_to(temporary).as_posix()
                    media_type = (
                        "application/json"
                        if path.suffix == ".json"
                        else "application/octet-stream"
                    )
                    store.index_existing(relative, media_type)
            store.verify_complete()
            if sha256_bytes(source_index.read_bytes()) != source_index_hash:
                raise ValueError("sealed source evolution changed during policy replay")
            os.replace(temporary, output)
            return outcome
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise


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
