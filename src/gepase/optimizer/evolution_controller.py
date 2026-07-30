"""One resumable Core controller for the R4 package-evolution chain."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any, Literal

from pydantic import Field

from gepase.evals.engine import MultiFidelityEvalEngine
from gepase.evals.eval_plan import FrozenEvalPlan
from gepase.evals.functional import AnalyzerSubmission
from gepase.evals.schema import EvidenceTier
from gepase.evals.scores import TaskScoreVector
from gepase.evals.statistics import PairedScore
from gepase.mutation.applier import apply_package_patch
from gepase.mutation.causal import audit_causality
from gepase.mutation.proposer import (
    PatchProposalStore,
    PatchProposalSubmission,
    PatchProposalWorkItem,
    PatchTargetSnapshot,
    ProposalWorkStatus,
    proposal_accounting_increments,
)
from gepase.mutation.schema import (
    PackagePatch,
    PatchApplication,
    PatchApplicationStatus,
    PatchOperationKind,
)
from gepase.mutation.target_set import TargetSet, choose_bounded_target_set
from gepase.mutation.validators.schema_gate import run_schema_gate
from gepase.mutation.validators.static_gate import run_static_gate
from gepase.optimizer.acceptance.engine import ValidationGatedAcceptance
from gepase.optimizer.acceptance.minibatch import run_minibatch_gate
from gepase.optimizer.acceptance.models import GateOutcome
from gepase.optimizer.acceptance.validation import derive_task_score_secondary_evidence
from gepase.optimizer.candidate import PackageCandidate, build_seed_candidate
from gepase.optimizer.evolution.branching import (
    BranchRegistry,
    freeze_lineage_root,
)
from gepase.optimizer.evolution.models import (
    BreedingSnapshot,
    EvolutionCandidateIdentity,
    ExclusiveContribution,
    MergeEligibility,
    MergeParentCandidate,
)
from gepase.optimizer.evolution.parent_sets import (
    ParentSetEnumerationReport,
    enumerate_parent_sets,
)
from gepase.optimizer.gepa_adapter import (
    CandidateEvaluation,
    CandidateEvaluationRow,
)
from gepase.optimizer.gepa_compat import (
    assert_compatible_gepa,
    build_gepa_state,
    select_candidate_indices,
)
from gepase.optimizer.graph_selector import eligible_mutation_targets
from gepase.optimizer.merge.closure import parent_contribution
from gepase.optimizer.merge.conflicts import detect_conflicts
from gepase.optimizer.merge.deterministic import deterministic_merge_patch
from gepase.optimizer.merge.models import MergeOutcome, MergeOutcomeStatus
from gepase.optimizer.runtime import (
    BudgetUsage,
    EvolutionPhase,
    EvolutionRunState,
    ReferenceEvidenceKey,
    audit_reference_cache,
    build_reference_evidence_key,
    canonical_fingerprint,
    load_r4_config,
)
from gepase.optimizer.selectors import (
    RankedSelection,
    SelectionContext,
    SelectionResult,
    SelectorKind,
    SelectorRankingAudit,
    selector_for,
)
from gepase.optimizer.session_runtime import (
    ActiveSessionRuntime,
    ActiveSessionState,
    BudgetCheckpoint,
    BudgetContinuationDecision,
    HostAttemptAccounting,
    ReservationSettlement,
    RuntimeBarrier,
    RuntimeBudgetBinding,
    RuntimeSessionStatus,
    UsageAllowance,
)
from gepase.optimizer.status import CandidateStatus
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.coverage import audit_graph_coverage
from gepase.package.dynamic_graph import (
    SelectorGraphBinding,
    overlay_package_access,
)
from gepase.package.ir import FailureSlice, IRNode, NodeKind, PackageGraph
from gepase.package.loader import load_package
from gepase.package.slicing import reverse_slice
from gepase.run_lifecycle import RunLifecycle, RunLifecycleMode, RunLifecycleStatus
from gepase.schemas.common import FrozenModel
from gepase.store.artifacts import (
    ArtifactStore,
    atomic_write,
    canonical_json_bytes,
    resolve_scoped_artifact_index,
    sha256_bytes,
)
from gepase.store.candidates import CandidateStore
from gepase.store.evolution_pool import EvolutionPoolEntry, EvolutionPoolStore
from gepase.store.rejected import RejectedEditStore


def _sum_usage(values: tuple[UsageAllowance, ...]) -> UsageAllowance:
    total = UsageAllowance(agent_calls=0, estimated_tokens=0, active_wall_clock_ms=0)
    for value in values:
        total = total.add(value)
    return total


def _reconcile_active_session_accounting(
    project_root: Path,
    run_dir: Path,
    state: ActiveSessionState,
) -> dict[str, Any]:
    """Prove that settlement and HostAttempt ledgers reproduce session usage.

    A preaccounted recovery settlement contributes zero actual usage because its
    cited HostAttempt records already own those calls.  Summing both typed
    ledgers therefore neither drops failed contexts nor double-counts recovered
    submissions.
    """

    settlement_paths = sorted((run_dir / "reservation-settlements").glob("*.json"))
    settlements = tuple(
        ReservationSettlement.model_validate_json(path.read_text(encoding="utf-8"))
        for path in settlement_paths
    )
    attempt_paths = sorted((run_dir / "host-attempt-accounting").glob("*.json"))
    attempts = tuple(
        HostAttemptAccounting.model_validate_json(path.read_text(encoding="utf-8"))
        for path in attempt_paths
    )
    settlement_usage = _sum_usage(tuple(item.actual for item in settlements))
    host_attempt_usage = _sum_usage(tuple(item.usage for item in attempts))
    candidate_accounting_ids = tuple(
        item for item in state.internal_accounting_ids if item.startswith("candidate:")
    )
    attempt_ids = tuple(item.accounting_id for item in attempts)
    expected_internal_ids = {*candidate_accounting_ids, *attempt_ids}
    if expected_internal_ids != set(state.internal_accounting_ids):
        raise ValueError("active-session internal accounting has an unknown or missing owner")
    if len(candidate_accounting_ids) != state.used.candidates:
        raise ValueError("active-session candidate accounting does not reproduce usage")
    combined = settlement_usage.add(host_attempt_usage).model_copy(
        update={"candidates": len(candidate_accounting_ids)}
    )
    for name in (
        "agent_calls",
        "estimated_tokens",
        "proposals",
        "candidates",
        "repairs",
    ):
        if getattr(combined, name) != getattr(state.used, name):
            raise ValueError(f"active-session {name} does not reconcile with typed ledgers")
    settlement_duration_ms = sum(item.agent_duration_ms for item in settlements)
    host_attempt_duration_ms = sum(item.agent_duration_ms for item in attempts)
    if settlement_duration_ms + host_attempt_duration_ms != state.cumulative_agent_duration_ms:
        raise ValueError("active-session Agent duration does not reconcile with typed ledgers")
    preaccounted_ids = tuple(
        accounting_id
        for item in settlements
        if item.accounting_mode == "preaccounted_host_attempts"
        for accounting_id in item.host_attempt_accounting_ids
    )
    if not set(preaccounted_ids) <= set(attempt_ids):
        raise ValueError("preaccounted settlement cites a missing HostAttempt")
    return {
        "authority": ("ActiveSessionRuntime + ReservationSettlement + HostAttemptAccounting"),
        "reconciled": True,
        "accepted_role_settlement_count": len(settlements),
        "accepted_role_settlement_usage": settlement_usage.model_dump(mode="json"),
        "accepted_role_settlement_duration_ms": settlement_duration_ms,
        "host_attempt_count": len(attempts),
        "host_attempt_usage": host_attempt_usage.model_dump(mode="json"),
        "host_attempt_duration_ms": host_attempt_duration_ms,
        "preaccounted_host_attempt_count": len(preaccounted_ids),
        "candidate_internal_accounting_count": len(candidate_accounting_ids),
        "authoritative_session_usage": state.used.model_dump(mode="json"),
        "authoritative_cumulative_agent_duration_ms": (state.cumulative_agent_duration_ms),
        "settlement_refs": [path.relative_to(project_root).as_posix() for path in settlement_paths],
        "host_attempt_refs": [path.relative_to(project_root).as_posix() for path in attempt_paths],
    }


class CandidateTaskReflection(FrozenModel):
    task_id: str
    paired_delta: float = Field(ge=-1, le=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    failed_expectation_ids: tuple[str, ...]
    grader_feedback_zh: str


class CandidateReflectionWorkItem(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    work_id: str
    role: Literal["reflection"] = "reflection"
    candidate_id: str
    parent_candidate_ids: tuple[str, ...]
    patch_ref: str
    graph_ref: str
    graph_diff: dict[str, Any]
    task_feedback: tuple[CandidateTaskReflection, ...] = Field(min_length=1)
    node_hints: tuple[dict[str, str], ...]
    submission_schema_ref: str = "schemas/candidate_reflection_submission.schema.json"
    forbidden_inputs: tuple[str, ...] = (
        "held-out expected answer",
        "sibling candidate output",
        "future candidate",
        "deployable frontier identity",
    )


class ReflectionDiagnosis(FrozenModel):
    task_id: str
    diagnosis_zh: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    target_node_ids: tuple[str, ...]
    recommendation_zh: str = Field(min_length=1)


class CandidateReflectionSubmission(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    submission_id: str
    work_id: str
    host: str
    model: str
    host_task_id: str
    context_id: str
    duration_ms: int = Field(ge=0)
    token_estimate: int = Field(ge=0)
    diagnoses: tuple[ReflectionDiagnosis, ...]
    summary_zh: str = Field(min_length=1)


class TrainAdmission(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    candidate_id: str
    passed: bool
    gate: dict[str, Any]
    paired_scores: tuple[PairedScore, ...]
    strict_task_wins: tuple[str, ...]
    protected_floor_satisfied: bool
    validation_required: bool


class Generation2PlanningOutcome(FrozenModel):
    """Train-only, zero-execution plan for one bounded second-generation intent."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    status: Literal[
        "planned",
        "no_eligible_parent",
        "proposal_budget_exhausted",
        "candidate_budget_exhausted",
    ]
    parent_candidate_id: str | None = None
    parent_generation: int | None = Field(default=None, ge=1)
    planned_generation: int | None = Field(default=None, ge=2)
    branch_id: str | None = None
    proposal_work_id: str | None = None
    train_feedback_ref: str | None = None
    train_evidence_refs: tuple[str, ...] = ()
    selector_graph_ref: str | None = None
    rejected_memory_refs: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = Field(min_length=1)
    proposal_intent_charged: Literal[False] = False
    candidate_materialized: Literal[False] = False
    held_out_evidence_read: Literal[False] = False
    sibling_evidence_read: Literal[False] = False
    merge_path_used: Literal[False] = False

    @property
    def planned(self) -> bool:
        return self.status == "planned"


@dataclass(frozen=True)
class _SelectorGraphView:
    graph: PackageGraph
    binding: SelectorGraphBinding | None
    cache_hit: bool = False
    cache_audit_ref: str | None = None


@dataclass(frozen=True)
class _ProposalScope:
    failure_slice: FailureSlice
    full_selection: SelectionResult
    selected: tuple[RankedSelection, ...]
    target_set: TargetSet | None
    selected_nodes: tuple[IRNode, ...]
    operations: tuple[PatchOperationKind, ...]
    ranking_audit: SelectorRankingAudit


class R4EvolutionController:
    """Own state transitions while all Agent role execution remains external."""

    def __init__(
        self,
        project_root: Path,
        run_dir: Path,
        config_path: Path,
        *,
        lifecycle_mode: RunLifecycleMode | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.run_dir = run_dir.resolve()
        if not self.run_dir.is_relative_to(self.project_root):
            raise ValueError("R4 run must remain inside the project")
        self.config_hash, self.config = load_r4_config(self.project_root, config_path)
        self.lifecycle_mode = lifecycle_mode
        if self.config.run_id != self.run_dir.name:
            raise ValueError("R4 run directory must match frozen config run_id")
        self.lifecycle: RunLifecycle | None = None
        if self.config.lifecycle_policy is not None:
            if lifecycle_mode is None:
                raise ValueError("strict evolution config requires an explicit lifecycle mode")
            self.lifecycle = RunLifecycle(
                self.run_dir,
                run_id=self.config.run_id,
                owner="evolution",
                expected_config_hash=self.config_hash,
            )
            required = (
                "resolved-config.json",
                "evolution-state.json",
                "candidates.sqlite3",
                "checkpoint.json",
                "runtime-session.json",
            )
            self.lifecycle.prepare(
                lifecycle_mode,
                required_files=() if lifecycle_mode is RunLifecycleMode.CREATE_NEW else required,
            )
        else:
            # Sealed v0.1 configs predate the typed lifecycle contract.  Keep
            # their exact fingerprint and read behavior without retroactively
            # modifying protected runs.
            self.run_dir.mkdir(parents=True, exist_ok=True)

    @property
    def state_path(self) -> Path:
        return self.run_dir / "evolution-state.json"

    def _write(self, relative: str, value: object) -> None:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")  # type: ignore[union-attr]
        atomic_write(self.run_dir / relative, canonical_json_bytes(value))

    def _read(self, relative: str) -> dict[str, Any]:
        value = json.loads((self.run_dir / relative).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"expected object: {relative}")
        return value

    def state(self) -> EvolutionRunState:
        return EvolutionRunState.model_validate_json(self.state_path.read_text(encoding="utf-8"))

    def _save_state(self, state: EvolutionRunState) -> None:
        validated = EvolutionRunState.model_validate(state.model_dump(mode="json"))
        self._write("evolution-state.json", validated)
        with CandidateStore(self.run_dir / "candidates.sqlite3") as store:
            store.save_state(validated.model_dump(mode="json"))
            store.write_checkpoint(self.run_dir)
        if self.lifecycle is not None and (self.run_dir / "runtime-session.json").is_file():
            runtime_status = self._session().state().status
            lifecycle_status = (
                RunLifecycleStatus.PAUSED
                if runtime_status
                in {
                    RuntimeSessionStatus.PAUSED,
                    RuntimeSessionStatus.AWAITING_CONTINUATION,
                    RuntimeSessionStatus.STOPPED,
                }
                else RunLifecycleStatus.ABORTED
                if runtime_status is RuntimeSessionStatus.ABORTED
                else RunLifecycleStatus.COMPLETE
                if runtime_status is RuntimeSessionStatus.COMPLETE
                else RunLifecycleStatus.ACTIVE
            )
            self.lifecycle.checkpoint(
                config_hash=self.config_hash,
                status=lifecycle_status,
                critical_files=(
                    "resolved-config.json",
                    "evolution-state.json",
                    "candidates.sqlite3",
                    "checkpoint.json",
                    "runtime-session.json",
                ),
            )

    def _session(self) -> ActiveSessionRuntime:
        policy = self.config.active_session_budget_policy
        if policy is None:
            raise ValueError("active-session runtime is not enabled for this legacy config")
        return ActiveSessionRuntime(
            self.run_dir,
            run_id=self.config.run_id,
            config_hash=self.config_hash,
            policy=policy,
        )

    @staticmethod
    def _node_content(package_root: Path, node: IRNode) -> str:
        text = (package_root / node.path).read_text(encoding="utf-8")
        if node.span is None:
            return text
        lines = text.splitlines(keepends=True)
        return "".join(lines[node.span.start_line - 1 : node.span.end_line])

    @staticmethod
    def _operation_for(node: IRNode) -> PatchOperationKind:
        if node.kind in {NodeKind.SECTION, NodeKind.INSTRUCTION, NodeKind.REFERENCE_CHUNK}:
            return PatchOperationKind.REPLACE_MARKDOWN_BLOCK
        if node.kind is NodeKind.FRONTMATTER:
            return PatchOperationKind.UPDATE_FRONTMATTER
        if node.kind is NodeKind.FUNCTION:
            return PatchOperationKind.REPLACE_PYTHON_FUNCTION
        if node.kind is NodeKind.FILE:
            return PatchOperationKind.REPLACE_TEXT_FILE
        raise ValueError(f"unsupported proposal target kind: {node.kind.value}")

    def _safe_project_ref(self, reference: str, *, directory: bool = False) -> Path:
        relative = Path(reference)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("selector graph reference must be repository-relative")
        resolved = (self.project_root / relative).resolve(strict=True)
        if not resolved.is_relative_to(self.project_root):
            raise ValueError("selector graph reference escapes the project")
        if directory and not resolved.is_dir():
            raise ValueError(f"selector graph reference is not a directory: {reference}")
        return resolved

    def _frozen_plan_task_ids(self, split: Literal["train", "validation"]) -> tuple[str, ...]:
        plan = FrozenEvalPlan.model_validate_json(
            (self.project_root / self.config.frozen_plan_ref).read_text(encoding="utf-8")
        )
        selected = tuple(
            sorted(case.case_id for case in plan.functional_cases if case.split == split)
        )
        if not selected:
            raise ValueError(f"frozen EvalPlan has no {split} cases")
        return selected

    def _candidate_split_task_ids(
        self,
        candidate_id: str,
        split: Literal["train", "validation"],
    ) -> tuple[str, ...]:
        metadata_path = self.run_dir / f"evals/{candidate_id}/{split}/run-metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError(f"candidate {split} metadata must be an object")
        if metadata.get("mode") != "frozen-candidate":
            raise ValueError(f"candidate {split} metadata is not frozen-candidate")
        if metadata.get("candidate_id") != candidate_id or metadata.get("split") != split:
            raise ValueError(f"candidate {split} metadata identity differs")
        raw = metadata.get("selected_case_ids")
        if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
            raise ValueError(f"candidate {split} metadata has no frozen selected cases")
        selected = tuple(str(item) for item in raw)
        if len(selected) != len(set(selected)):
            raise ValueError(f"candidate {split} metadata repeats selected cases")
        frozen_plan_ref = metadata.get("frozen_plan_ref")
        if not isinstance(frozen_plan_ref, str) or not frozen_plan_ref:
            raise ValueError(f"candidate {split} metadata lacks frozen_plan_ref")
        metadata_relative_plan = Path(frozen_plan_ref)
        if metadata_relative_plan.is_absolute() or ".." in metadata_relative_plan.parts:
            raise ValueError(f"candidate {split} frozen_plan_ref must be repository-relative")
        metadata_plan_path = (self.project_root / metadata_relative_plan).resolve(strict=True)
        controller_plan_path = (self.project_root / self.config.frozen_plan_ref).resolve(
            strict=True
        )
        if not metadata_plan_path.is_relative_to(self.project_root):
            raise ValueError(f"candidate {split} frozen_plan_ref escapes the project")
        if not controller_plan_path.is_relative_to(self.project_root):
            raise ValueError("Controller frozen_plan_ref escapes the project")
        if metadata_plan_path != controller_plan_path:
            raise ValueError(f"candidate {split} metadata uses another frozen EvalPlan")
        plan = FrozenEvalPlan.model_validate_json(controller_plan_path.read_text(encoding="utf-8"))
        if metadata.get("frozen_plan_hash") != plan.plan_hash:
            raise ValueError(f"candidate {split} metadata frozen plan hash differs")
        expected = tuple(
            sorted(case.case_id for case in plan.functional_cases if case.split == split)
        )
        if not expected:
            raise ValueError(f"Controller frozen EvalPlan has no {split} cases")
        self._require_exact_task_ids(
            selected,
            expected,
            source=f"candidate {split} selected cases",
        )
        return expected

    @staticmethod
    def _require_exact_task_ids(
        observed: tuple[str, ...],
        expected: tuple[str, ...],
        *,
        source: str,
    ) -> None:
        duplicates = sorted(task_id for task_id, count in Counter(observed).items() if count > 1)
        missing = sorted(set(expected) - set(observed))
        unexpected = sorted(set(observed) - set(expected))
        if duplicates or missing or unexpected or len(observed) != len(expected):
            raise ValueError(
                f"{source} task IDs differ from frozen selected cases: "
                f"duplicates={duplicates}, missing={missing}, unexpected={unexpected}"
            )

    def _train_task_ids(self, evidence_run: Path) -> tuple[str, ...]:
        metadata = json.loads((evidence_run / "run-metadata.json").read_text(encoding="utf-8"))
        if metadata.get("split") == "train":
            selected = tuple(sorted(str(item) for item in metadata["selected_case_ids"]))
            if not selected:
                raise ValueError("candidate train evidence has no selected cases")
            return selected
        summary_path = evidence_run / "functional-run-summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        selected = tuple(
            sorted(
                {
                    str(row["task_id"])
                    for row in summary["pair_summaries"]
                    if row["split"] == "train"
                }
            )
        )
        if not selected:
            raise ValueError("reference evidence has no train cases")
        return selected

    @staticmethod
    def _graph_layer_counts(graph: PackageGraph) -> dict[str, int]:
        counts = Counter(edge.layer for edge in graph.edges)
        return {
            layer: counts.get(layer, 0)
            for layer in ("static", "planned", "observed", "semantic_hypothesis")
        }

    def _record_selector_graph_cache_access(
        self,
        binding: SelectorGraphBinding,
        *,
        hit: bool,
    ) -> str:
        directory = (
            self.run_dir
            / "selector-graph-cache-audits"
            / binding.parent_candidate_id
            / binding.cache_key
        )
        sequence = len(tuple(directory.glob("*.json"))) + 1
        relative = (directory / f"access-{sequence:04d}.json").relative_to(self.run_dir).as_posix()
        self._write(
            relative,
            {
                "schema_version": "1.0.0",
                "sequence": sequence,
                "cache_key": binding.cache_key,
                "hit": hit,
                "parent_candidate_id": binding.parent_candidate_id,
                "parent_snapshot_hash": binding.parent_snapshot_hash,
                "parent_content_hash": binding.parent_content_hash,
                "evidence_scope_hash": binding.evidence_scope_hash,
                "graph_policy_hash": binding.graph_policy_hash,
                "binding_ref": (
                    self.run_dir
                    / "selector-graphs"
                    / binding.parent_candidate_id
                    / binding.cache_key
                    / "binding.json"
                )
                .relative_to(self.project_root)
                .as_posix(),
                "selector_graph_ref": binding.selector_graph_ref,
                "verified_at": datetime.now(UTC).isoformat(),
            },
        )
        return (self.run_dir / relative).relative_to(self.project_root).as_posix()

    def build_selector_graph_view(
        self,
        parent: PackageCandidate,
        *,
        package_ref: str,
        evidence_run_ref: str,
        expected_graph_ref: str,
        evidence_variant: Literal["original", "candidate"],
        allowed_task_ids: tuple[str, ...],
        reference_key_hash: str,
    ) -> _SelectorGraphView:
        """Build the sole parent-bound graph view used by mutation selectors.

        Static mode intentionally preserves the v0.1 path.  The observed mode
        recompiles the parent Package, binds only sealed parent-train access,
        and persists the exact graph and audits later exported to the Proposer.
        """

        package_root = self._safe_project_ref(package_ref, directory=True)
        policy = self.config.selector_graph_policy
        if policy.mode == "static":
            analysis = PackageAnalyzer().analyze(package_root)
            if analysis.snapshot.package_id != parent.package_id:
                raise ValueError("fresh Package belongs to another candidate package")
            if analysis.snapshot.snapshot_hash != parent.content_hash:
                raise ValueError("fresh Package content differs from parent candidate")
            return _SelectorGraphView(graph=analysis.graph, binding=None)

        if not allowed_task_ids:
            raise ValueError("observed selector graph requires a non-empty train scope")
        if evidence_variant == "original" and parent.generation != 0:
            raise ValueError("derived parent may not consume seed-original access")
        if evidence_variant == "candidate" and parent.generation == 0:
            raise ValueError("seed parent may not consume candidate access")

        evidence_run = self._safe_project_ref(evidence_run_ref, directory=True)
        artifact_index_path, _scoped_hashes = resolve_scoped_artifact_index(evidence_run)
        metadata_path = evidence_run / "run-metadata.json"
        evidence_scope_hash = canonical_fingerprint(
            {
                "evidence_run_ref": evidence_run_ref,
                "artifact_index_sha256": sha256_bytes(artifact_index_path.read_bytes()),
                "run_metadata_sha256": sha256_bytes(metadata_path.read_bytes()),
                "allowed_task_ids": sorted(allowed_task_ids),
                "expected_graph_ref": expected_graph_ref,
                "evidence_variant": evidence_variant,
                "provider_snapshot": self.config.provider_snapshot,
                "host": self.config.host,
                "model": self.config.model,
                "runtime_environment_fingerprint": (self.config.runtime_environment_fingerprint),
                "tool_policy_fingerprint": self.config.tool_policy_fingerprint,
                "host_policy": self.config.host_policy,
                "seed": self.config.seed,
                "timeout_seconds": self.config.timeout_seconds,
                "reference_key_hash": reference_key_hash,
            }
        )
        graph_policy_hash = policy.policy_hash
        cache_key = canonical_fingerprint(
            {
                "parent_snapshot_hash": parent.snapshot_hash,
                "parent_content_hash": parent.content_hash,
                "evidence_scope_hash": evidence_scope_hash,
                "graph_policy_hash": graph_policy_hash,
            }
        )
        base = f"selector-graphs/{parent.candidate_id}/{cache_key}"
        binding_path = self.run_dir / base / "binding.json"
        graph_path = self.run_dir / base / "graph.json"
        if binding_path.is_file() and graph_path.is_file():
            current_snapshot = load_package(package_root)
            if (
                current_snapshot.package_id != parent.package_id
                or current_snapshot.snapshot_hash != parent.content_hash
            ):
                raise ValueError("cached selector graph parent Package has changed")
            binding = SelectorGraphBinding.model_validate_json(
                binding_path.read_text(encoding="utf-8")
            )
            if (
                binding.cache_key != cache_key
                or binding.evidence_scope_hash != evidence_scope_hash
                or binding.graph_policy_hash != graph_policy_hash
                or binding.parent_candidate_id != parent.candidate_id
                or binding.parent_snapshot_hash != parent.snapshot_hash
                or binding.parent_content_hash != parent.content_hash
                or binding.evidence_run_ref != evidence_run_ref
                or binding.evidence_variant != evidence_variant
                or binding.evidence_task_ids != tuple(sorted(allowed_task_ids))
            ):
                raise ValueError("selector graph cache binding mismatch")
            cached_artifacts = (
                (binding.snapshot_ref, binding.snapshot_sha256),
                (binding.package_ir_ref, binding.package_ir_sha256),
                (binding.static_graph_ref, binding.static_graph_sha256),
                (binding.selector_graph_ref, binding.selector_graph_sha256),
                (binding.coverage_ref, binding.coverage_sha256),
                (binding.overlay_audit_ref, binding.overlay_audit_sha256),
            )
            for reference, expected_hash in cached_artifacts:
                path = self._safe_project_ref(reference)
                if sha256_bytes(path.read_bytes()) != expected_hash:
                    raise ValueError(f"cached selector artifact hash mismatch: {reference}")
            graph = PackageGraph.model_validate_json(graph_path.read_text(encoding="utf-8"))
            if sha256_bytes(graph_path.read_bytes()) != binding.selector_graph_sha256:
                raise ValueError("cached selector graph hash mismatch")
            if graph.snapshot_hash != parent.content_hash:
                raise ValueError("cached selector graph belongs to another parent content")
            cache_audit_ref = self._record_selector_graph_cache_access(binding, hit=True)
            return _SelectorGraphView(
                graph=graph,
                binding=binding,
                cache_hit=True,
                cache_audit_ref=cache_audit_ref,
            )

        analysis = PackageAnalyzer().analyze(package_root)
        if analysis.snapshot.package_id != parent.package_id:
            raise ValueError("fresh Package belongs to another candidate package")
        if analysis.snapshot.snapshot_hash != parent.content_hash:
            raise ValueError("fresh Package content differs from parent candidate")
        coverage = audit_graph_coverage(analysis.snapshot, analysis.graph)
        if (
            coverage.snapshot_file_count != coverage.file_node_count
            or coverage.file_node_coverage != 1.0
            or any(not item.parse_status_explicit for item in coverage.files)
        ):
            raise ValueError("fresh selector graph does not satisfy explicit coverage")

        graph, overlay = overlay_package_access(
            analysis.graph,
            evidence_run,
            allowed_task_ids=set(allowed_task_ids),
            expected_graph_ref=expected_graph_ref,
            expected_variant=evidence_variant,
            expected_provider_id=self.config.provider_snapshot,
            expected_host=self.config.host,
            expected_model=self.config.model,
            expected_seed=self.config.seed,
            expected_timeout_seconds=self.config.timeout_seconds,
            expected_candidate_id=(
                parent.candidate_id if evidence_variant == "candidate" else None
            ),
            expected_content_hash=(
                parent.content_hash if evidence_variant == "candidate" else None
            ),
            expected_reference_key_hash=reference_key_hash,
        )
        layers = self._graph_layer_counts(graph)
        if layers["planned"] or layers["semantic_hypothesis"]:
            raise ValueError("GH-E0 selector graph may contain only static and observed layers")
        if (
            policy.require_observed_when_access_present
            and overlay.mapped_events > 0
            and (overlay.observed_edges == 0 or layers["observed"] == 0)
        ):
            raise ValueError("typed package access produced no observed selector edges")

        payloads = {
            "snapshot.json": analysis.snapshot.model_dump(mode="json"),
            "package-ir.json": analysis.package_ir.model_dump(mode="json"),
            "static-graph.json": analysis.graph.model_dump(mode="json"),
            "graph.json": graph.model_dump(mode="json"),
            "coverage.json": coverage.model_dump(mode="json"),
            "overlay-audit.json": overlay.model_dump(mode="json"),
        }
        refs: dict[str, tuple[str, str]] = {}
        for name, payload in payloads.items():
            relative = f"{base}/{name}"
            data = canonical_json_bytes(payload)
            atomic_write(self.run_dir / relative, data)
            refs[name] = (
                (self.run_dir / relative).relative_to(self.project_root).as_posix(),
                sha256_bytes(data),
            )
        binding = SelectorGraphBinding(
            mode="static_observed",
            parent_candidate_id=parent.candidate_id,
            parent_snapshot_hash=parent.snapshot_hash,
            parent_content_hash=parent.content_hash,
            package_ref=package_ref,
            snapshot_ref=refs["snapshot.json"][0],
            snapshot_sha256=refs["snapshot.json"][1],
            package_ir_ref=refs["package-ir.json"][0],
            package_ir_sha256=refs["package-ir.json"][1],
            static_graph_ref=refs["static-graph.json"][0],
            static_graph_sha256=refs["static-graph.json"][1],
            selector_graph_ref=refs["graph.json"][0],
            selector_graph_sha256=refs["graph.json"][1],
            coverage_ref=refs["coverage.json"][0],
            coverage_sha256=refs["coverage.json"][1],
            overlay_audit_ref=refs["overlay-audit.json"][0],
            overlay_audit_sha256=refs["overlay-audit.json"][1],
            layer_counts=layers,
            evidence_run_ref=evidence_run_ref,
            evidence_variant=evidence_variant,
            evidence_task_ids=tuple(sorted(allowed_task_ids)),
            evidence_scope_hash=evidence_scope_hash,
            graph_policy_hash=graph_policy_hash,
            cache_key=cache_key,
            accepted_work_ids=overlay.accepted_work_ids,
            filtered_work_ids=overlay.filtered_work_ids,
            mapped_access_events=overlay.mapped_events,
            rejected_access_events=overlay.rejected_events,
            observed_edges=overlay.observed_edges,
        )
        self._write(f"{base}/binding.json", binding)
        cache_audit_ref = self._record_selector_graph_cache_access(binding, hit=False)
        return _SelectorGraphView(
            graph=graph,
            binding=binding,
            cache_audit_ref=cache_audit_ref,
        )

    def _ranking_audit(
        self,
        ranked: tuple[RankedSelection, ...],
        selected: tuple[RankedSelection, ...],
    ) -> SelectorRankingAudit:
        top_k_limit = min(self.config.selector_graph_policy.top_k_audit_limit, len(ranked))
        selected_ids = {item.node_id for item in selected}
        executable = next(
            (
                item
                for item in ranked
                if item.node_id not in selected_ids
                and item.path.endswith((".py", ".sh", ".bash", ".zsh"))
            ),
            None,
        )
        if executable is None:
            executable = next(
                (item for item in ranked if item.path.endswith((".py", ".sh", ".bash", ".zsh"))),
                None,
            )
        top_k = list(ranked[:top_k_limit])
        present = {item.node_id for item in top_k}
        top_k.extend(item for item in selected if item.node_id not in present)
        top_k.sort(key=lambda item: item.rank)
        return SelectorRankingAudit(
            total_ranked=len(ranked),
            selected_node_ids=tuple(item.node_id for item in selected),
            top_k=tuple(top_k),
            executable_alternative=executable,
        )

    @staticmethod
    def _require_observed_selection(
        binding: SelectorGraphBinding | None,
        selected: tuple[RankedSelection, ...],
    ) -> None:
        if binding is None or not binding.accepted_work_ids:
            return
        dynamic_values = [
            contribution.raw_value
            for row in selected
            for contribution in row.contributions
            if contribution.feature == "dynamic_access"
        ]
        if binding.observed_edges == 0 or not dynamic_values or max(dynamic_values) <= 0:
            raise ValueError(
                "parent typed access did not contribute to the selected proposal targets"
            )

    def _train_failure_rows(self) -> list[dict[str, Any]]:
        reference = self.project_root / self.config.reference_run_ref
        summary = json.loads((reference / "functional-run-summary.json").read_text())
        gains = {
            row["task_id"]: float(row["skill_gain"])
            for row in summary["pair_summaries"]
            if row["split"] == "train"
        }
        work_by_id = {
            item["analyzer_work_id"]: item
            for path in (reference / "analyzer-work-items").glob("*.json")
            for item in [json.loads(path.read_text(encoding="utf-8"))]
        }
        rows: list[dict[str, Any]] = []
        for path in sorted((reference / "analyzer-submissions").glob("*.json")):
            submission = AnalyzerSubmission.model_validate_json(path.read_text(encoding="utf-8"))
            work = work_by_id[submission.analyzer_work_id]
            task_id = str(work["task_id"])
            if task_id not in gains:
                continue
            analyses = [item for item in submission.analyses if item.variant == "original"]
            if not analyses:
                continue
            analysis = analyses[0]
            rows.append(
                {
                    "task_id": task_id,
                    "skill_gain": gains[task_id],
                    "analysis": analysis,
                    "submission_ref": path.relative_to(self.project_root).as_posix(),
                }
            )
        return sorted(rows, key=lambda row: (row["skill_gain"], row["task_id"]))

    def _select_proposal_scope(
        self,
        parent: PackageCandidate,
        selector_view: _SelectorGraphView,
        *,
        target_ids: tuple[str, ...],
        evidence_refs: tuple[str, ...],
        excluded_node_ids: set[str],
        iteration: int,
        selector_kind: SelectorKind,
        scope_reason: str,
    ) -> _ProposalScope:
        """Build the shared bounded selection used by initial and recovery proposals."""

        graph = selector_view.graph
        failure_slice = reverse_slice(graph, target_ids, max_nodes=80, max_tokens=8_000)
        targets = tuple(
            item
            for item in eligible_mutation_targets(graph)
            if item.node_id not in excluded_node_ids
        )
        if not targets:
            raise ValueError("proposal selection has no eligible targets")
        full_selection = selector_for(selector_kind).select(
            SelectionContext(
                graph=graph,
                targets=targets,
                failure_slices=(failure_slice,),
                evidence_refs=evidence_refs,
                diagnostic_severity={node_id: 1.0 for node_id in target_ids},
                seed=self.config.seed,
                iteration=iteration,
            ),
            limit=len(targets),
        )
        selected, target_set = choose_bounded_target_set(
            graph,
            full_selection.selected,
            parent_candidate_id=parent.candidate_id,
            evidence_refs=evidence_refs,
            scope_reason=scope_reason,
            max_targets=self.config.selector_target_limit,
        )
        self._require_observed_selection(selector_view.binding, selected)
        graph_nodes = {node.node_id: node for node in graph.nodes}
        selected_nodes = tuple(graph_nodes[item.node_id] for item in selected)
        return _ProposalScope(
            failure_slice=failure_slice,
            full_selection=full_selection,
            selected=selected,
            target_set=target_set,
            selected_nodes=selected_nodes,
            operations=tuple(dict.fromkeys(self._operation_for(node) for node in selected_nodes)),
            ranking_audit=self._ranking_audit(full_selection.selected, selected),
        )

    def _build_proposal_work(
        self,
        seed: PackageCandidate,
        selector_view: _SelectorGraphView,
        *,
        branch_index: int,
        failure: dict[str, Any],
        excluded_node_ids: set[str],
    ) -> PatchProposalWorkItem:
        analysis = failure["analysis"]
        target_ids = (
            tuple(
                node_id for node_id in analysis.target_node_ids if node_id not in excluded_node_ids
            )
            or analysis.target_node_ids
        )
        kind = {
            "graph": SelectorKind.GRAPH_GUIDED,
            "trace": SelectorKind.TRACE_ONLY,
            "round_robin": SelectorKind.ROUND_ROBIN,
            "random": SelectorKind.RANDOM,
        }[self.config.selector]
        evidence_ref = failure["submission_ref"]
        scope = self._select_proposal_scope(
            seed,
            selector_view,
            target_ids=target_ids,
            evidence_refs=(evidence_ref,),
            excluded_node_ids=excluded_node_ids,
            iteration=branch_index,
            selector_kind=kind,
            scope_reason=(
                "One failure hypothesis may expose one graph-connected companion; "
                "otherwise the primary remains a single-target patch."
            ),
        )
        work_identity = {
            "run": self.config.run_id,
            "branch": branch_index,
            "targets": [node.node_id for node in scope.selected_nodes],
        }
        work_id = f"proposal-work-{canonical_fingerprint(work_identity)[:24]}"
        exported_targets = tuple(
            PatchTargetSnapshot(
                node_id=node.node_id,
                node_kind=node.kind.value,
                path=node.path,
                locator=node.locator,
                content_hash=node.content_hash,
                content=self._node_content(self.project_root / seed.source_package_ref, node),
                selection=ranked,
            )
            for ranked, node in zip(scope.selected, scope.selected_nodes, strict=True)
        )
        return PatchProposalWorkItem(
            work_id=work_id,
            run_id=self.config.run_id,
            task_id=str(failure["task_id"]),
            parent_candidate_id=seed.candidate_id,
            parent_snapshot_hash=seed.snapshot_hash,
            parent_content_hash=seed.content_hash,
            selector=scope.full_selection.selector.value,
            selector_graph=selector_view.binding,
            selector_ranking=(scope.ranking_audit if selector_view.binding is not None else None),
            targets=exported_targets,
            target_set=scope.target_set,
            allowed_operations=scope.operations,
            edit_budget=self.config.patch_budget,
            evidence_refs=(evidence_ref,),
            actionable_side_information={
                "failure_evidence": {
                    "kind": "low_score",
                    "task_id": failure["task_id"],
                    "skill_gain": failure["skill_gain"],
                    "issue_zh": analysis.issue_zh,
                    "recommendation_zh": analysis.recommendation_zh,
                    "evidence_refs": list(analysis.evidence_refs),
                },
                "graph_slice": scope.failure_slice.model_dump(mode="json"),
                "causal_contract": {"required": True},
                "causal_targets": [
                    {
                        "node_id": node.node_id,
                        "failure_evidence_ids": [evidence_ref],
                        "causal_path_node_ids": (
                            list(scope.target_set.causal_path_node_ids)
                            if scope.target_set is not None
                            else [node.node_id]
                        ),
                        "expected_affected_assertions": [],
                        "expected_affected_metrics": [
                            "task_correctness",
                            "output_quality",
                            "skill_gain",
                        ],
                        "allowed_operation_classes": [self._operation_for(node).value],
                        "executable_target": node.path.endswith((".py", ".sh")),
                    }
                    for node in scope.selected_nodes
                ],
                "preserve": [
                    "unrelated package behavior",
                    "public interfaces unless explicitly targeted",
                    "all source and fixture files outside the exported target",
                ],
            },
            output_instructions=(
                "Return only one typed PackagePatch for the exported bounded target scope. "
                "Preserve Markdown headings and Python signatures, do not read "
                "assertions, sibling candidates, or repository state, and do not edit files."
            ),
        )

    def initialize(self) -> dict[str, Any]:
        if self.state_path.exists():
            if self.lifecycle is not None:
                raise ValueError("strict create_new cannot reuse an initialized run")
            return {"created": False, "state": self.state().model_dump(mode="json")}
        key = build_reference_evidence_key(self.project_root, self.config)
        audit = audit_reference_cache(self.project_root, key)
        if not audit.hit:
            raise ValueError("R4 cannot start without a complete reference cache hit")
        seed = build_seed_candidate(
            self.project_root, self.config.package_ref, run_id=self.config.run_id
        )
        evidence_run = self._safe_project_ref(self.config.reference_run_ref, directory=True)
        selector_view = self.build_selector_graph_view(
            seed,
            package_ref=seed.source_package_ref,
            evidence_run_ref=self.config.reference_run_ref,
            expected_graph_ref=self.config.package_graph_ref,
            evidence_variant="original",
            allowed_task_ids=self._train_task_ids(evidence_run),
            reference_key_hash=key.key_hash,
        )
        gepa = assert_compatible_gepa()
        self._write("resolved-config.json", self.config)
        self._write("reference-evidence-key.json", key)
        self._write("reference-cache-audit.json", audit)
        self._write("gepa-provenance.json", gepa)
        self._write("seed-candidate.json", seed)
        if self.config.active_session_budget_policy is not None:
            self._session().create()
        with CandidateStore(self.run_dir / "candidates.sqlite3") as store:
            store.add_candidate(seed, CandidateStatus.SEED)
        if self.config.lifecycle_policy is not None:
            state = EvolutionRunState(
                run_id=self.config.run_id,
                config_hash=self.config_hash,
                phase=EvolutionPhase.INITIALIZED,
                seed_candidate_id=seed.candidate_id,
                budget_usage=BudgetUsage(cache_hits=1),
            )
            self._save_state(state)
            checkpoint = self.pause_at_barrier(
                RuntimeBarrier.PACKAGE_COMPILED,
                continuation_risk_zh=(
                    "fresh parent Package 和 selector graph 已编译并绑定; "
                    "继续后 Core 将构建 train-only proposal scope。"
                ),
            )
            return {
                "created": True,
                "seed_candidate_id": seed.candidate_id,
                "proposal_work_items": 0,
                "reference_cache_hit": True,
                "gepa": gepa,
                "budget_checkpoint": checkpoint,
            }
        failures = self._train_failure_rows()
        if len(failures) < self.config.branch_count:
            raise ValueError("R3 train feedback cannot seed the required branches")
        excluded: set[str] = set()
        works: list[PatchProposalWorkItem] = []
        with PatchProposalStore(self.run_dir / "proposal-work.sqlite3") as proposals:
            for index, failure in enumerate(failures[: self.config.branch_count]):
                work = self._build_proposal_work(
                    seed,
                    selector_view,
                    branch_index=index,
                    failure=failure,
                    excluded_node_ids=excluded,
                )
                excluded.update(item.node_id for item in work.targets)
                proposals.add_work(work)
                self._write(f"proposal-work-items/{work.work_id}.json", work)
                works.append(work)
            proposals.write_snapshot(self.run_dir)
        state = EvolutionRunState(
            run_id=self.config.run_id,
            config_hash=self.config_hash,
            phase=EvolutionPhase.PROPOSAL,
            seed_candidate_id=seed.candidate_id,
            budget_usage=BudgetUsage(cache_hits=1),
        )
        self._save_state(state)
        self._write(
            "branch-plan.json",
            {
                "schema_version": "1.0.0",
                "train_feedback_only": True,
                "held_out_feedback_read": False,
                "branches": [
                    {
                        "branch_index": index,
                        "task_id": work.task_id,
                        "work_id": work.work_id,
                        "target_node_ids": [item.node_id for item in work.targets],
                    }
                    for index, work in enumerate(works)
                ],
            },
        )
        return {
            "created": True,
            "seed_candidate_id": seed.candidate_id,
            "proposal_work_items": len(works),
            "reference_cache_hit": True,
            "gepa": gepa,
        }

    def prepare_initial_proposals(self) -> dict[str, Any]:
        """Build the existing bounded proposal work after package-compile approval."""

        if self.config.lifecycle_policy is None:
            raise ValueError("legacy R4 initializes proposal work atomically")
        state = self.state()
        if state.phase is EvolutionPhase.PROPOSAL:
            with PatchProposalStore(self.run_dir / "proposal-work.sqlite3") as proposals:
                works = proposals.work_items()
            return {
                "created": False,
                "proposal_work_items": len(works),
                "state": state.model_dump(mode="json"),
            }
        if state.phase is not EvolutionPhase.INITIALIZED:
            raise ValueError("initial proposal scope requires initialized phase")
        if self._session().state().status is not RuntimeSessionStatus.ACTIVE:
            raise ValueError("package-compile continuation decision is required")
        seed = self._candidate(state.seed_candidate_id)
        key = ReferenceEvidenceKey.model_validate_json(
            (self.run_dir / "reference-evidence-key.json").read_text(encoding="utf-8")
        )
        evidence_run = self._safe_project_ref(self.config.reference_run_ref, directory=True)
        selector_view = self.build_selector_graph_view(
            seed,
            package_ref=seed.source_package_ref,
            evidence_run_ref=self.config.reference_run_ref,
            expected_graph_ref=self.config.package_graph_ref,
            evidence_variant="original",
            allowed_task_ids=self._train_task_ids(evidence_run),
            reference_key_hash=key.key_hash,
        )
        failures = self._train_failure_rows()
        if len(failures) < self.config.branch_count:
            raise ValueError("train feedback cannot seed the required branches")
        excluded: set[str] = set()
        works: list[PatchProposalWorkItem] = []
        with PatchProposalStore(self.run_dir / "proposal-work.sqlite3") as proposals:
            for index, failure in enumerate(failures[: self.config.branch_count]):
                work = self._build_proposal_work(
                    seed,
                    selector_view,
                    branch_index=index,
                    failure=failure,
                    excluded_node_ids=excluded,
                )
                excluded.update(item.node_id for item in work.targets)
                proposals.add_work(work)
                self._write(f"proposal-work-items/{work.work_id}.json", work)
                works.append(work)
            proposals.write_snapshot(self.run_dir)
        self._write(
            "branch-plan.json",
            {
                "schema_version": "1.0.0",
                "train_feedback_only": True,
                "held_out_feedback_read": False,
                "branches": [
                    {
                        "branch_index": index,
                        "task_id": work.task_id,
                        "work_id": work.work_id,
                        "target_node_ids": [item.node_id for item in work.targets],
                    }
                    for index, work in enumerate(works)
                ],
            },
        )
        self._save_state(state.model_copy(update={"phase": EvolutionPhase.PROPOSAL}))
        checkpoint = self.pause_at_barrier(
            RuntimeBarrier.PROPOSAL_SCOPE_READY,
            not_exported_work_ids=tuple(work.work_id for work in works),
            next_role="proposal",
            next_work_count=len(works),
            continuation_risk_zh=(
                "TargetSet/Patch scope 已冻结; 继续后将导出隔离的 Proposer work。"
            ),
        )
        return {
            "created": True,
            "proposal_work_items": len(works),
            "budget_checkpoint": checkpoint,
        }

    def export_next_proposal(self) -> PatchProposalWorkItem | None:
        """Reserve the next proposal call before changing its durable work status."""

        with PatchProposalStore(self.run_dir / "proposal-work.sqlite3") as store:
            preview = store.pending_work()
            if preview is None:
                store.write_snapshot(self.run_dir)
                return None
            if self.config.active_session_budget_policy is not None:
                is_repair = preview.repair_attempt > 0
                self._session().reserve(
                    batch_id=f"proposal:{preview.work_id}",
                    role="proposal",
                    work_ids=(preview.work_id,),
                    upper_bound=self._session().estimate_batch(
                        "proposal",
                        1,
                        proposal_intents=0 if is_repair else 1,
                        repair_attempts=1 if is_repair else 0,
                    ),
                )
            exported = store.next_work()
            if exported != preview:
                raise ValueError("proposal export changed after budget reservation")
            store.write_snapshot(self.run_dir)
        if self.config.active_session_budget_policy is not None:
            self._save_state(self.state())
        return exported

    def pause_at_barrier(
        self,
        barrier: RuntimeBarrier,
        *,
        completed_work_ids: tuple[str, ...] = (),
        not_exported_work_ids: tuple[str, ...] = (),
        candidate_gate_summary: dict[str, str] | None = None,
        next_role: str | None = None,
        next_work_count: int = 0,
        next_proposal_intents: int | None = None,
        next_repair_attempts: int | None = None,
        continuation_risk_zh: str = "继续运行将消费下一有界 tranche, 并可能产生新的 Agent work。",
    ) -> dict[str, Any]:
        session = self._session()
        estimate = (
            session.estimate_batch(
                next_role,
                next_work_count,
                proposal_intents=next_proposal_intents,
                repair_attempts=next_repair_attempts,
            )
            if next_role is not None and next_work_count > 0
            else None
        )
        state_bytes = self.state_path.read_bytes()
        evidence_hash = sha256_bytes(state_bytes)
        checkpoint = session.pause(
            barrier=barrier,
            evidence_hash=evidence_hash,
            completed_work_ids=completed_work_ids,
            not_exported_work_ids=not_exported_work_ids,
            candidate_gate_summary=candidate_gate_summary or {},
            next_batch_estimate=estimate,
            continuation_risk_zh=continuation_risk_zh,
        )
        self._save_state(self.state())
        return checkpoint.model_dump(mode="json")

    def apply_continuation_decision(self, decision: BudgetContinuationDecision) -> dict[str, Any]:
        state = self._session().apply_decision(decision)
        self._save_state(self.state())
        return state.model_dump(mode="json")

    def resume(self) -> dict[str, Any]:
        if self.lifecycle is None or self.lifecycle_mode is not RunLifecycleMode.RESUME:
            raise ValueError("optimizer resume requires strict lifecycle_mode=resume")
        runtime_state = self._session().state()
        if runtime_state.status is not RuntimeSessionStatus.ACTIVE:
            raise ValueError("optimizer resume requires an active hash-bound continuation decision")
        with PatchProposalStore(self.run_dir / "proposal-work.sqlite3") as proposals:
            proposal_count = proposals.resume()
            proposals.write_snapshot(self.run_dir)
        with CandidateStore(self.run_dir / "candidates.sqlite3") as candidates:
            reflection_count = candidates.resume_reflections()
            candidates.write_checkpoint(self.run_dir)
        eval_count = 0
        for ledger_path in sorted((self.run_dir / "evals").glob("candidate-*/*/ledger.sqlite3")):
            eval_run = ledger_path.parent
            lifecycle_payload = self._read(
                (eval_run / "run-lifecycle.json").relative_to(self.run_dir).as_posix()
            )
            if lifecycle_payload.get("status") in {"complete", "aborted"}:
                continue
            with MultiFidelityEvalEngine(
                self.project_root,
                eval_run,
                lifecycle_mode=RunLifecycleMode.RESUME,
            ) as engine:
                eval_count += engine.ledger.resume_interrupted()
                engine.snapshot_ledger()
        self._save_state(self.state())
        return {
            "run_id": self.config.run_id,
            "proposal_work_resumed": proposal_count,
            "reflection_work_resumed": reflection_count,
            "eval_work_resumed": eval_count,
            "usage_preserved": self._session().state().used.model_dump(mode="json"),
        }

    def ingest_proposal(self, submission: PatchProposalSubmission) -> dict[str, Any]:
        state = self.state()
        with PatchProposalStore(self.run_dir / "proposal-work.sqlite3") as store:
            work = store.get_work(submission.work_id)
            proposal_increment, repair_increment = proposal_accounting_increments(work)
            if (
                proposal_increment
                and state.budget_usage.proposals >= self.config.runtime_budget.max_proposals
            ):
                raise ValueError("R4 proposal budget is exhausted")
            added = store.ingest(submission)
            store.write_snapshot(self.run_dir)
        self._write(f"proposal-submissions/{submission.work_id}.json", submission)
        if added:
            if self.config.active_session_budget_policy is not None:
                self._session().settle(
                    work_id=submission.work_id,
                    actual_tokens=submission.provenance.token_estimate,
                    actual_duration_ms=submission.provenance.duration_ms,
                    repairs=repair_increment,
                    proposals=proposal_increment,
                )
            usage = state.budget_usage.model_copy(
                update={
                    "proposals": state.budget_usage.proposals + proposal_increment,
                    "agent_calls": state.budget_usage.agent_calls + 1,
                    "estimated_tokens": (
                        state.budget_usage.estimated_tokens + submission.provenance.token_estimate
                    ),
                    "cumulative_agent_duration_ms": (
                        state.budget_usage.cumulative_agent_duration_ms
                        + submission.provenance.duration_ms
                    ),
                    "repairs": state.budget_usage.repairs + repair_increment,
                }
            )
            self._save_state(state.model_copy(update={"budget_usage": usage}))
        return {"ingested": added, "submission_id": submission.submission_id}

    def prepare_proposal_repair(self, failed_work_id: str) -> dict[str, Any]:
        """Create one bounded proposer repair work item after a typed failure."""

        return self.prepare_proposal_repairs((failed_work_id,))

    def prepare_proposal_repairs(self, failed_work_ids: tuple[str, ...]) -> dict[str, Any]:
        """Append an atomic repair batch for typed failed proposer submissions."""

        state = self.state()
        if state.phase is not EvolutionPhase.PROPOSAL:
            raise ValueError("proposal repair requires the proposal phase")
        if not failed_work_ids or len(failed_work_ids) != len(set(failed_work_ids)):
            raise ValueError("proposal repair requires distinct failed work ids")
        repairs: list[PatchProposalWorkItem] = []
        with PatchProposalStore(self.run_dir / "proposal-work.sqlite3") as store:
            for failed_work_id in failed_work_ids:
                repairs.append(
                    store.plan_repair(
                        failed_work_id,
                        max_repair_attempts=self.config.runtime_budget.max_repair_attempts_per_work,
                    )
                )
            store.write_snapshot(self.run_dir)
        for failed_work_id, repair in zip(failed_work_ids, repairs, strict=True):
            self._write(f"proposal-work-items/{repair.work_id}.json", repair)
            self._write(
                f"proposal-repairs/{repair.work_id}.json",
                {
                    "schema_version": "1.0.0",
                    "failed_work_id": failed_work_id,
                    "repair_work_id": repair.work_id,
                    "repair_attempt": repair.repair_attempt,
                    "parent_candidate_id": repair.parent_candidate_id,
                    "target_node_ids": [item.node_id for item in repair.targets],
                },
            )
        checkpoint = None
        if self.config.active_session_budget_policy is not None:
            checkpoint = self.pause_at_barrier(
                RuntimeBarrier.PROPOSAL_SCOPE_READY,
                not_exported_work_ids=tuple(repair.work_id for repair in repairs),
                next_role="proposal",
                next_work_count=len(repairs),
                next_proposal_intents=0,
                next_repair_attempts=len(repairs),
                continuation_risk_zh=(
                    "Proposer 原始输出已以 typed failure 保留; 继续后将仅导出 "
                    "同 scope 的隔离 repair work batch。"
                ),
            )
        return {
            "failed_work_ids": list(failed_work_ids),
            "repair_work_ids": [repair.work_id for repair in repairs],
            "repair_attempts": [repair.repair_attempt for repair in repairs],
            "budget_checkpoint": checkpoint,
        }

    def apply_proposals(self) -> dict[str, Any]:
        state = self.state()
        seed = PackageCandidate.model_validate_json(
            (self.run_dir / "seed-candidate.json").read_text(encoding="utf-8")
        )
        source_graph = PackageAnalyzer().analyze(self.project_root / seed.source_package_ref).graph
        with PatchProposalStore(self.run_dir / "proposal-work.sqlite3") as proposal_store:
            all_works = proposal_store.work_items()
            all_submissions = proposal_store.submissions()
        by_work_id = {item.work_id: item for item in all_works}
        completed_pairs = [
            (by_work_id[submission.work_id], submission)
            for submission in all_submissions
            if submission.status is ProposalWorkStatus.COMPLETED
            and submission.patch is not None
            and submission.work_id in by_work_id
        ]
        if len(completed_pairs) < self.config.branch_count:
            raise ValueError("all required branch proposals must be ingested before apply")
        works = [item for item, _submission in completed_pairs]
        submissions = [submission for _item, submission in completed_pairs]
        causality = audit_causality(works, submissions)
        if not causality["valid"]:
            raise ValueError("proposal causality audit failed")
        self._write("proposal-causality-audit.json", causality)
        root = freeze_lineage_root(
            package_id=seed.package_id,
            source_snapshot_hash=seed.snapshot_hash,
            root_candidate_id=seed.candidate_id,
            root_content_hash=seed.content_hash,
            config_hash=self.config_hash,
        )
        registry = BranchRegistry(root)
        branches: list[dict[str, Any]] = []
        candidate_ids: list[str] = []
        with RejectedEditStore(self.run_dir / "rejected.sqlite3") as rejected:
            for index, submission in enumerate(submissions[: self.config.branch_count]):
                if submission.patch is None:
                    raise ValueError(f"proposal failed: {submission.work_id}")
                patch = submission.patch
                gate0 = run_schema_gate(seed, patch, source_graph, rejected_store=rejected)
                application, candidate = apply_package_patch(
                    self.project_root,
                    seed,
                    patch,
                    self.run_dir / "candidate-workspaces",
                    run_id=self.config.run_id,
                )
                gate1 = run_static_gate(
                    self.project_root,
                    application,
                    baseline_package_root=self.project_root / seed.source_package_ref,
                )
                self._write(f"patches/{patch.patch_id}.json", patch)
                self._write(f"applications/{application.application_id}.json", application)
                self._write(
                    f"pre-eval-gates/{patch.patch_id}.json",
                    {
                        "gate_0": gate0.model_dump(mode="json"),
                        "gate_1": gate1.model_dump(mode="json"),
                    },
                )
                if (
                    gate0.outcome is not GateOutcome.PASSED
                    or gate1.outcome is not GateOutcome.PASSED
                    or application.status is not PatchApplicationStatus.APPLIED
                    or candidate is None
                    or application.workspace_ref is None
                ):
                    continue
                if self.config.active_session_budget_policy is not None:
                    self._session().record_internal_usage(
                        accounting_id=f"candidate:{candidate.candidate_id}",
                        usage=UsageAllowance(
                            agent_calls=0,
                            estimated_tokens=0,
                            active_wall_clock_ms=0,
                            candidates=1,
                        ),
                    )
                branch = registry.create_initial(
                    failure_cluster_id=f"train-feedback:{works[index].task_id}",
                    variant_index=index,
                    child_candidate_id=candidate.candidate_id,
                    operator=candidate.operator,
                )
                graph = (
                    PackageAnalyzer().analyze(self.project_root / application.workspace_ref).graph
                )
                self._write(f"candidates/{candidate.candidate_id}/candidate.json", candidate)
                self._write(f"candidates/{candidate.candidate_id}/graph.json", graph)
                self._write(f"candidates/{candidate.candidate_id}/application.json", application)
                self._write(f"candidates/{candidate.candidate_id}/patch.json", patch)
                self._write(f"branches/{branch.branch_id}.json", branch)
                with CandidateStore(self.run_dir / "candidates.sqlite3") as store:
                    store.add_candidate(candidate, CandidateStatus.PROPOSED)
                branches.append(branch.model_dump(mode="json"))
                candidate_ids.append(candidate.candidate_id)
        if len(candidate_ids) < self.config.branch_count:
            raise ValueError("fewer than two valid mutation branches survived Gate 0/1")
        usage = state.budget_usage.model_copy(update={"candidates": len(candidate_ids)})
        self._save_state(
            state.model_copy(
                update={
                    "phase": EvolutionPhase.TRAIN_EXECUTION,
                    "branch_candidate_ids": tuple(candidate_ids),
                    "budget_usage": usage,
                }
            )
        )
        return {"applied_branches": len(candidate_ids), "branches": branches}

    def _candidate(self, candidate_id: str) -> PackageCandidate:
        with CandidateStore(self.run_dir / "candidates.sqlite3") as store:
            return store.candidate(candidate_id)

    def _candidate_application(self, candidate_id: str) -> PatchApplication:
        return PatchApplication.model_validate_json(
            (self.run_dir / f"candidates/{candidate_id}/application.json").read_text(
                encoding="utf-8"
            )
        )

    def _candidate_patch(self, candidate_id: str) -> PackagePatch:
        return PackagePatch.model_validate_json(
            (self.run_dir / f"candidates/{candidate_id}/patch.json").read_text(encoding="utf-8")
        )

    def plan_candidate(
        self, candidate_id: str, split: Literal["train", "validation"]
    ) -> dict[str, Any]:
        candidate = self._candidate(candidate_id)
        application = self._candidate_application(candidate_id)
        if application.workspace_ref is None:
            raise ValueError("candidate lacks a materialized workspace")
        eval_dir = self.run_dir / f"evals/{candidate_id}/{split}"
        graph_ref = (
            (self.run_dir / f"candidates/{candidate_id}/graph.json")
            .relative_to(self.project_root)
            .as_posix()
        )
        lifecycle_mode = None
        if self.config.lifecycle_policy is not None:
            lifecycle_mode = RunLifecycleMode.CREATE_NEW
        with MultiFidelityEvalEngine(
            self.project_root,
            eval_dir,
            lifecycle_mode=lifecycle_mode,
            allow_empty_initialization_recovery=True,
        ) as engine:
            if self.config.active_session_budget_policy is not None:
                engine.bind_runtime_budget(
                    RuntimeBudgetBinding(
                        owner_run_id=self.config.run_id,
                        owner_run_ref=self.run_dir.relative_to(self.project_root).as_posix(),
                        config_hash=self.config_hash,
                        policy=self.config.active_session_budget_policy,
                    )
                )
            result = engine.plan_frozen_candidate(
                self.project_root / self.config.frozen_plan_ref,
                self.project_root / self.config.scoring_policy_ref,
                self.run_dir / "reference-evidence-key.json",
                candidate_id=candidate.candidate_id,
                candidate_content_hash=candidate.content_hash,
                candidate_ref=application.workspace_ref,
                package_graph_ref=graph_ref,
                split=split,
                host=self.config.host,
                model=self.config.model,
                seed=self.config.seed,
                timeout_seconds=self.config.timeout_seconds,
            )
        self._write(
            f"scheduler/{candidate_id}-{split}-execution.json",
            {
                "schema_version": "1.0.0",
                "role": "executor",
                "barrier": f"{candidate_id}:{split}:execution",
                "max_concurrency": self.config.runtime_budget.max_concurrency,
                "work_items": result["planned_work_items"],
                "fresh_candidate_only": True,
                "reference_cache_hit": True,
            },
        )
        return result

    def record_pre_eval_gates(self, candidate_id: str) -> dict[str, Any]:
        """Recompute and durably bind Gate 0/1 to any materialized candidate."""

        candidate = self._candidate(candidate_id)
        patch = self._candidate_patch(candidate_id)
        application = self._candidate_application(candidate_id)
        parent = self._candidate(application.parent_candidate_id)
        parent_graph = (
            PackageAnalyzer().analyze(self.project_root / parent.source_package_ref).graph
        )
        with RejectedEditStore(self.run_dir / "rejected.sqlite3") as rejected:
            gate0 = run_schema_gate(parent, patch, parent_graph, rejected_store=rejected)
            gate1 = run_static_gate(
                self.project_root,
                application,
                baseline_package_root=self.project_root / parent.source_package_ref,
            )
        record = {
            "schema_version": "1.0.0",
            "candidate_id": candidate.candidate_id,
            "parent_candidate_id": parent.candidate_id,
            "patch_id": patch.patch_id,
            "gate_0": gate0.model_dump(mode="json"),
            "gate_1": gate1.model_dump(mode="json"),
            "passed": (
                gate0.outcome is GateOutcome.PASSED
                and gate1.outcome is GateOutcome.PASSED
                and application.status is PatchApplicationStatus.APPLIED
                and application.original_workspace_hash_unchanged
            ),
        }
        self._write(f"pre-eval-gates/{patch.patch_id}.json", record)
        if candidate_id in self.state().merge_candidate_ids:
            build = self._read("merge/build-record.json")
            build["gate_0_1_ref"] = (
                (self.run_dir / f"pre-eval-gates/{patch.patch_id}.json")
                .relative_to(self.project_root)
                .as_posix()
            )
            build["gate_0_1_passed"] = record["passed"]
            self._write("merge/build-record.json", build)
        if not record["passed"]:
            raise ValueError("candidate did not pass pre-evaluation Gate 0/1")
        return record

    def _paired_scores(
        self,
        candidate_id: str,
        split: Literal["train", "validation"],
    ) -> tuple[PairedScore, ...]:
        path = self.run_dir / f"evals/{candidate_id}/{split}/paired-scores.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        pairs = tuple(PairedScore.model_validate(item) for item in raw["rows"])
        expected = self._candidate_split_task_ids(
            candidate_id,
            split,
        )
        self._require_exact_task_ids(
            tuple(item.task_id for item in pairs),
            expected,
            source=f"candidate {split} paired scores",
        )
        return pairs

    def admit_train(self, candidate_id: str) -> TrainAdmission:
        pairs = self._paired_scores(candidate_id, "train")
        decision = run_minibatch_gate(pairs, policy=self.config.train_policy)
        wins = tuple(sorted(item.task_id for item in pairs if item.delta > 0))
        admission = TrainAdmission(
            candidate_id=candidate_id,
            passed=decision.gate.outcome is GateOutcome.PASSED,
            gate=decision.gate.model_dump(mode="json"),
            paired_scores=pairs,
            strict_task_wins=wins,
            protected_floor_satisfied=(
                decision.statistics.losses / decision.statistics.n
                <= self.config.train_policy.maximum_loss_fraction
            ),
            validation_required=decision.gate.outcome is GateOutcome.PASSED,
        )
        self._write(f"train-admission/{candidate_id}.json", admission)
        if admission.passed and candidate_id not in self.state().merge_candidate_ids:
            self._record_train_pool(candidate_id, admission)
        else:
            candidate = self._candidate(candidate_id)
            patch = self._candidate_patch(candidate_id)
            application = self._candidate_application(candidate_id)
            seed = PackageCandidate.model_validate_json(
                (self.run_dir / "seed-candidate.json").read_text(encoding="utf-8")
            )
            ValidationGatedAcceptance(
                self.project_root, self.run_dir, run_id=self.config.run_id
            ).evaluate(
                seed,
                candidate,
                patch,
                application,
                train_pairs=pairs,
                minibatch_policy=self.config.train_policy,
                validation_policy=self.config.validation_policy,
                record_evolution_candidate=False,
                static_regression_aware=True,
            )
        if self.config.active_session_budget_policy is not None:
            self.pause_at_barrier(
                RuntimeBarrier.CANDIDATE_TRAIN_COMPLETE,
                candidate_gate_summary={
                    candidate_id: "train_admitted" if admission.passed else "train_rejected"
                },
                next_role="executor" if admission.passed else "reflection",
                next_work_count=(
                    len(self._frozen_plan_task_ids("validation")) if admission.passed else 1
                ),
                continuation_risk_zh=(
                    "该候选已完整覆盖冻结 train split; 继续后只按冻结 Gate "
                    "决定进入 held-out validation 或最多一次 Reflection。"
                ),
            )
        return admission

    def _branch_for_candidate(self, candidate_id: str) -> dict[str, Any]:
        for path in (self.run_dir / "branches").glob("*.json"):
            row = self._read(path.relative_to(self.run_dir).as_posix())
            if candidate_id in row.get("candidate_chain", []):
                return row
        raise KeyError(candidate_id)

    def _record_train_pool(self, candidate_id: str, admission: TrainAdmission) -> None:
        candidate = self._candidate(candidate_id)
        patch = self._candidate_patch(candidate_id)
        branch = self._branch_for_candidate(candidate_id)
        application = self._candidate_application(candidate_id)
        graph = PackageGraph.model_validate_json(
            (self.run_dir / f"candidates/{candidate_id}/graph.json").read_text(encoding="utf-8")
        )
        from gepase.optimizer.merge.closure import dependency_closure

        closure = dependency_closure(graph, set(patch.selected_node_ids))
        entry = EvolutionPoolEntry(
            candidate_id=candidate_id,
            parent_candidate_id=candidate.parent_ids[0],
            patch_id=patch.patch_id,
            package_id=candidate.package_id,
            source_package_ref=candidate.source_package_ref,
            source_snapshot_hash=candidate.snapshot_hash,
            lineage_root_candidate_id=self.state().seed_candidate_id,
            branch_id=str(branch["branch_id"]),
            branch_root_candidate_id=str(branch["branch_root_candidate_id"]),
            failure_cluster_ids=(str(branch["failure_cluster_id"]),),
            ancestor_candidate_ids=tuple(str(item) for item in branch["candidate_chain"][:-1]),
            candidate_content_hash=candidate.content_hash,
            train_evidence_refs=tuple(
                sorted(
                    {
                        ref
                        for pair in admission.paired_scores
                        for ref in (pair.parent_record_id, pair.candidate_record_id)
                    }
                )
            ),
            exclusive_task_keys=admission.strict_task_wins,
            exclusive_component_ids=patch.selected_node_ids,
            exclusive_closure_ids=closure,
            train_mean_delta=mean(item.delta for item in admission.paired_scores),
            train_floor_satisfied=True,
            gate_0_1_passed=(
                application.status is PatchApplicationStatus.APPLIED
                and application.original_workspace_hash_unchanged
            ),
            merge_eligibility=MergeEligibility.ELIGIBLE,
        )
        with EvolutionPoolStore(self.run_dir / "evolution-pool.sqlite3") as pool:
            pool.add(entry)
            pool.snapshot(self.run_dir / "evolution-pool.json")

    def finalize_validation(self, candidate_id: str) -> dict[str, Any]:
        train = self._paired_scores(candidate_id, "train")
        validation = self._paired_scores(candidate_id, "validation")
        candidate = self._candidate(candidate_id)
        patch = self._candidate_patch(candidate_id)
        application = self._candidate_application(candidate_id)
        # A merge child has multiple lineage parents but its deterministic union
        # is rebased onto the explicit application parent (the common LCA).
        parent = self._candidate(application.parent_candidate_id)
        secondary = derive_task_score_secondary_evidence(self.project_root, validation)
        secondary_ref = (
            (self.run_dir / f"secondary-objectives/{candidate_id}.json")
            .relative_to(self.project_root)
            .as_posix()
        )
        self._write(f"secondary-objectives/{candidate_id}.json", secondary)
        efficiency_delta = secondary.improvements["task_score_efficiency"]
        decision = ValidationGatedAcceptance(
            self.project_root, self.run_dir, run_id=self.config.run_id
        ).evaluate(
            parent,
            candidate,
            patch,
            application,
            train_pairs=train,
            validation_pairs=validation,
            efficiency_regression=max(0.0, -efficiency_delta),
            secondary_objective_improvements=secondary.improvements,
            secondary_evidence_refs=(*secondary.evidence_refs, secondary_ref),
            minibatch_policy=self.config.train_policy,
            validation_policy=self.config.validation_policy,
            record_evolution_candidate=False,
            static_regression_aware=True,
        )
        state = self.state()
        evaluated = tuple(dict.fromkeys((*state.evaluated_candidate_ids, candidate_id)))
        deployable = state.deployable_candidate_ids
        if decision.verdict is CandidateStatus.ACCEPTED:
            deployable = tuple(dict.fromkeys((*deployable, candidate_id)))
        self._save_state(
            state.model_copy(
                update={
                    "evaluated_candidate_ids": evaluated,
                    "deployable_candidate_ids": deployable,
                    "phase": EvolutionPhase.REFLECTION,
                }
            )
        )
        if candidate_id in state.merge_candidate_ids:
            outcome_path = self.run_dir / "merge/outcome.json"
            if not outcome_path.is_file():
                raise ValueError("merge child validation lacks typed MergeOutcome")
            outcome = MergeOutcome.model_validate_json(outcome_path.read_text(encoding="utf-8"))
            if outcome.merge_candidate_id != candidate_id:
                raise ValueError("MergeOutcome points to another child")
            self._write(
                "merge/outcome.json",
                outcome.model_copy(
                    update={
                        "status": MergeOutcomeStatus.MATERIALIZED_AND_EVALUATED,
                        "evaluation_complete": True,
                    }
                ),
            )
        result = decision.model_dump(mode="json")
        if self.config.active_session_budget_policy is not None:
            result["budget_checkpoint"] = self.pause_at_barrier(
                RuntimeBarrier.CANDIDATE_VALIDATION_COMPLETE,
                candidate_gate_summary={candidate_id: decision.verdict.value},
                continuation_risk_zh=(
                    "该候选已完整覆盖 3-case held-out validation; 继续不会改写已完成分数。"
                ),
            )
        return result

    def _identity(
        self,
        candidate: PackageCandidate,
        *,
        branch_id: str | None,
        branch_root: str | None,
        failure_ids: tuple[str, ...],
    ) -> EvolutionCandidateIdentity:
        return EvolutionCandidateIdentity(
            candidate_id=candidate.candidate_id,
            package_id=candidate.package_id,
            source_package_ref=candidate.source_package_ref,
            source_snapshot_hash=candidate.snapshot_hash,
            lineage_root_candidate_id=self.state().seed_candidate_id,
            parent_ids=candidate.parent_ids,
            branch_id=branch_id,
            branch_root_candidate_id=branch_root,
            generation=candidate.generation,
            operator=candidate.operator,
            content_hash=candidate.content_hash,
            failure_cluster_ids=failure_ids,
        )

    def build_merge(self) -> dict[str, Any]:
        state = self.state()
        with EvolutionPoolStore(self.run_dir / "evolution-pool.sqlite3") as pool:
            entries = pool.all()
        branches = [entry for entry in entries if entry.candidate_id in state.branch_candidate_ids]
        seed = self._candidate(state.seed_candidate_id)
        seed_identity = self._identity(seed, branch_id=None, branch_root=None, failure_ids=())
        identities = [seed_identity]
        parents: list[MergeParentCandidate] = []
        for entry in branches:
            candidate = self._candidate(entry.candidate_id)
            identity = self._identity(
                candidate,
                branch_id=entry.branch_id,
                branch_root=entry.branch_root_candidate_id,
                failure_ids=entry.failure_cluster_ids,
            )
            identities.append(identity)
            parents.append(
                MergeParentCandidate(
                    identity=identity,
                    patch_id=entry.patch_id,
                    ancestor_chain=(seed.candidate_id, candidate.candidate_id),
                    contribution=ExclusiveContribution(
                        task_keys=entry.exclusive_task_keys,
                        objective_keys=entry.exclusive_objective_keys,
                        component_ids=entry.exclusive_component_ids,
                        closure_ids=entry.exclusive_closure_ids,
                    ),
                    train_evidence_refs=entry.train_evidence_refs,
                    gate_0_1_passed=entry.gate_0_1_passed,
                    train_floor_satisfied=entry.train_floor_satisfied,
                    merge_eligibility=entry.merge_eligibility,
                )
            )
        breeding_identity = [item.identity.candidate_id for item in parents]
        if parents:
            breeding = BreedingSnapshot(
                snapshot_id=f"breeding-{canonical_fingerprint(breeding_identity)[:24]}",
                candidates=tuple(parents),
                lineage=tuple(identities),
                selection_config_hash=self.config_hash,
                train_evidence_refs=tuple(
                    sorted({ref for parent in parents for ref in parent.train_evidence_refs})
                ),
            )
            enumeration = enumerate_parent_sets(breeding)
            self._write("merge/breeding-snapshot.json", breeding)
        else:
            enumeration = ParentSetEnumerationReport(
                breeding_snapshot_id=(f"breeding-{canonical_fingerprint(breeding_identity)[:24]}"),
                ranked_parent_sets=(),
                merge_compatible_parent_set_count=0,
                merge_compatible_package_count=0,
                cross_package_pairs_observed=0,
                rejected_pair_count=0,
                rejection_reasons={"insufficient_parents": 1},
                consumer_validated=True,
            )
        self._write("merge/parent-set-enumeration.json", enumeration)
        if not enumeration.ranked_parent_sets:
            rejection_counts = Counter(
                reason
                for item in enumeration.considered_parent_sets
                if not item.eligible
                for reason in item.reason_codes
            )
            if not rejection_counts:
                rejection_counts.update({"insufficient_parents": 1})
            outcome = MergeOutcome(
                status=MergeOutcomeStatus.NO_ELIGIBLE_PARENT_SET,
                considered_parent_candidate_ids=tuple(
                    item.identity.candidate_id for item in parents
                ),
                considered_parent_set_count=(len(enumeration.considered_parent_sets)),
                eligible_parent_set_count=0,
                rejected_parent_set_count=len(
                    [item for item in enumeration.considered_parent_sets if not item.eligible]
                ),
                rejection_reason_counts=dict(sorted(rejection_counts.items())),
                cross_package_pair_count=enumeration.cross_package_pairs_observed,
                enumeration_ref=(self.run_dir / "merge/parent-set-enumeration.json")
                .relative_to(self.project_root)
                .as_posix(),
            )
            self._write("merge/outcome.json", outcome)
            self._save_state(state.model_copy(update={"phase": EvolutionPhase.MERGE}))
            result = outcome.model_dump(mode="json")
            if self.config.active_session_budget_policy is not None:
                result["budget_checkpoint"] = self.pause_at_barrier(
                    RuntimeBarrier.MERGE_RESOLVED,
                    candidate_gate_summary={"merge": outcome.status.value},
                    continuation_risk_zh=(
                        "全部 train-admitted parent set 已枚举且无合法集合; 继续后只收口当前证据。"
                    ),
                )
            return result
        selected = enumeration.ranked_parent_sets[0].parent_set
        source_graph = PackageAnalyzer().analyze(self.project_root / seed.source_package_ref).graph
        patch_map = {
            candidate_id: self._candidate_patch(candidate_id)
            for candidate_id in state.branch_candidate_ids
        }
        contributions = tuple(
            parent_contribution(
                parent,
                lca_candidate_id=seed.candidate_id,
                graph=source_graph,
                patch_for_candidate=lambda candidate_id: patch_map[candidate_id],
            )
            for parent in selected.parents
        )
        conflicts = detect_conflicts(contributions)
        self._write(
            "merge/conflict-report.json",
            {
                "schema_version": "1.0.0",
                "conflicts": [item.model_dump(mode="json") for item in conflicts],
                "unresolved": len(conflicts),
            },
        )
        if conflicts:
            raise ValueError("merge parent contributions have unresolved conflicts")
        merge_patch, contribution_map = deterministic_merge_patch(
            seed,
            source_graph,
            contributions,
            parent_set_id=selected.parent_set_id,
        )
        application, child = apply_package_patch(
            self.project_root,
            seed,
            merge_patch,
            self.run_dir / "candidate-workspaces",
            run_id=self.config.run_id,
            candidate_parent_ids=tuple(parent.identity.candidate_id for parent in selected.parents),
            candidate_generation=max(parent.identity.generation for parent in selected.parents) + 1,
            candidate_operator="same_package_multi_parent_merge",
        )
        if child is None or application.status is not PatchApplicationStatus.APPLIED:
            raise ValueError(f"merge child application failed: {application.error_code}")
        if self.config.active_session_budget_policy is not None:
            self._session().record_internal_usage(
                accounting_id=f"candidate:{child.candidate_id}",
                usage=UsageAllowance(
                    agent_calls=0,
                    estimated_tokens=0,
                    active_wall_clock_ms=0,
                    candidates=1,
                ),
            )
        graph = PackageAnalyzer().analyze(self.project_root / str(application.workspace_ref)).graph
        self._write(f"patches/{merge_patch.patch_id}.json", merge_patch)
        self._write(f"applications/{application.application_id}.json", application)
        self._write(f"candidates/{child.candidate_id}/candidate.json", child)
        self._write(f"candidates/{child.candidate_id}/graph.json", graph)
        self._write(f"candidates/{child.candidate_id}/application.json", application)
        self._write(f"candidates/{child.candidate_id}/patch.json", merge_patch)
        self._write("merge/contribution-map.json", contribution_map)
        self._write(
            "merge/build-record.json",
            {
                "schema_version": "1.0.0",
                "status": "materialized",
                "parent_set_id": selected.parent_set_id,
                "lca_candidate_id": seed.candidate_id,
                "parent_candidate_ids": list(child.parent_ids),
                "candidate_id": child.candidate_id,
                "patch_id": merge_patch.patch_id,
                "application_id": application.application_id,
                "same_package": True,
                "same_snapshot": True,
                "cross_package_parent_count": 0,
                "held_out_features_read": 0,
            },
        )
        outcome = MergeOutcome(
            status=MergeOutcomeStatus.MATERIALIZED_PENDING_EVALUATION,
            considered_parent_candidate_ids=tuple(item.identity.candidate_id for item in parents),
            considered_parent_set_count=(len(enumeration.considered_parent_sets)),
            eligible_parent_set_count=enumeration.merge_compatible_parent_set_count,
            rejected_parent_set_count=len(
                [item for item in enumeration.considered_parent_sets if not item.eligible]
            ),
            rejection_reason_counts=dict(
                sorted(
                    Counter(
                        reason
                        for item in enumeration.considered_parent_sets
                        if not item.eligible
                        for reason in item.reason_codes
                    ).items()
                )
            ),
            cross_package_pair_count=enumeration.cross_package_pairs_observed,
            selected_parent_set_id=selected.parent_set_id,
            merge_candidate_id=child.candidate_id,
            evaluation_complete=False,
            enumeration_ref=(self.run_dir / "merge/parent-set-enumeration.json")
            .relative_to(self.project_root)
            .as_posix(),
            build_record_ref=(self.run_dir / "merge/build-record.json")
            .relative_to(self.project_root)
            .as_posix(),
        )
        self._write("merge/outcome.json", outcome)
        with CandidateStore(self.run_dir / "candidates.sqlite3") as store:
            store.add_candidate(child, CandidateStatus.PROPOSED)
        usage = state.budget_usage.model_copy(
            update={"candidates": state.budget_usage.candidates + 1}
        )
        self._save_state(
            state.model_copy(
                update={
                    "phase": EvolutionPhase.MERGE,
                    "merge_candidate_ids": (*state.merge_candidate_ids, child.candidate_id),
                    "budget_usage": usage,
                }
            )
        )
        pre_eval = self.record_pre_eval_gates(child.candidate_id)
        result = {
            "candidate_id": child.candidate_id,
            "parents": list(child.parent_ids),
            "patch_id": merge_patch.patch_id,
            "conflicts": 0,
            "contribution_map_fingerprint": contribution_map.fingerprint,
            "gate_0_1_passed": pre_eval["passed"],
        }
        if self.config.active_session_budget_policy is not None:
            result["budget_checkpoint"] = self.pause_at_barrier(
                RuntimeBarrier.MERGE_RESOLVED,
                candidate_gate_summary={child.candidate_id: "merge_child_materialized"},
                next_role="executor",
                next_work_count=len(self._frozen_plan_task_ids("train")),
                continuation_risk_zh=(
                    "合法父集已按预注册排序构建 Merge child; 继续后该 child 必须完整走 Gate 0-3。"
                ),
            )
        return result

    def _evaluation_for_candidate(self, candidate: PackageCandidate) -> CandidateEvaluation:
        if candidate.generation == 0:
            run = self.project_root / self.config.reference_run_ref
            plan = FrozenEvalPlan.model_validate_json(
                (self.project_root / self.config.frozen_plan_ref).read_text(encoding="utf-8")
            )
            rows: list[CandidateEvaluationRow] = []
            for path in sorted((run / "task-score-vectors").glob("*.json")):
                vector = TaskScoreVector.model_validate_json(path.read_text(encoding="utf-8"))
                case = next(
                    item for item in plan.functional_cases if item.case_id == vector.task_id
                )
                if vector.variant != self.config.reference_variant or case.split != "train":
                    continue
                score = 0.55 * vector.task_correctness + 0.45 * vector.output_quality
                rows.append(
                    CandidateEvaluationRow(
                        task_id=vector.task_id,
                        record_id=path.stem,
                        record_ref=path.relative_to(self.project_root).as_posix(),
                        evidence_tier=EvidenceTier.E3_EXECUTABLE,
                        score=score,
                        objective_scores=vector.objectives,
                        task_score_vector=vector,
                        output={},
                        uncertainty=max(0.0, 1.0 - vector.reliability),
                        provenance={
                            "host": self.config.host,
                            "model": self.config.model,
                            "host_task_id": "r3-reference-anchor",
                            "submission_id": path.stem,
                        },
                    )
                )
        else:
            run = self.run_dir / f"evals/{candidate.candidate_id}/train"
            summary = self._read(f"evals/{candidate.candidate_id}/train/candidate-run-summary.json")
            utility = {
                row["task_id"]: float(row["candidate_score"]) for row in summary["pair_summaries"]
            }
            rows = []
            for path in sorted((run / "task-score-vectors").glob("*.json")):
                vector = TaskScoreVector.model_validate_json(path.read_text(encoding="utf-8"))
                rows.append(
                    CandidateEvaluationRow(
                        task_id=vector.task_id,
                        record_id=path.stem,
                        record_ref=path.relative_to(self.project_root).as_posix(),
                        evidence_tier=EvidenceTier.E3_EXECUTABLE,
                        score=utility[vector.task_id],
                        objective_scores=vector.objectives,
                        task_score_vector=vector,
                        output={},
                        uncertainty=max(0.0, 1.0 - vector.reliability),
                        provenance={
                            "host": self.config.host,
                            "model": self.config.model,
                            "host_task_id": f"r4-candidate-{candidate.candidate_id}",
                            "submission_id": path.stem,
                        },
                    )
                )
        evaluation_identity = {"candidate": candidate.candidate_id, "split": "train"}
        return CandidateEvaluation(
            evaluation_id=(f"evaluation-{canonical_fingerprint(evaluation_identity)[:24]}"),
            candidate_id=candidate.candidate_id,
            candidate_content_hash=candidate.content_hash,
            split="train",
            requested_tier=EvidenceTier.E3_EXECUTABLE,
            rows=tuple(rows),
            mean_score=mean(item.score for item in rows),
            objective_means={
                key: mean(row.objective_scores[key] for row in rows)
                for key in sorted({name for row in rows for name in row.objective_scores})
            },
        )

    def write_gepa_snapshot(self) -> dict[str, Any]:
        state = self.state()
        ordered = [self._candidate(state.seed_candidate_id)]
        with EvolutionPoolStore(self.run_dir / "evolution-pool.sqlite3") as pool:
            admitted = {entry.candidate_id for entry in pool.all()}
        ordered.extend(
            self._candidate(candidate_id)
            for candidate_id in (*state.branch_candidate_ids, *state.merge_candidate_ids)
            if candidate_id in admitted
        )
        evaluations = [self._evaluation_for_candidate(candidate) for candidate in ordered]
        candidate_task_sets = {
            self._candidate_split_task_ids(candidate.candidate_id, "train")
            for candidate in ordered
            if candidate.generation > 0
        }
        if len(candidate_task_sets) > 1:
            raise ValueError("GEPA candidates use different frozen train task sets")
        expected_task_ids = (
            next(iter(candidate_task_sets))
            if candidate_task_sets
            else self._train_task_ids(self.project_root / self.config.reference_run_ref)
        )
        for evaluation in evaluations:
            self._require_exact_task_ids(
                tuple(item.task_id for item in evaluation.rows),
                expected_task_ids,
                source=f"GEPA evaluation {evaluation.candidate_id}",
            )
        gepa_state = build_gepa_state(ordered, evaluations, frontier_type="instance")
        pareto_index, best_index = select_candidate_indices(gepa_state, seed=self.config.seed)
        result = {
            "schema_version": "1.0.0",
            "gepa_version": assert_compatible_gepa()["version"],
            "official_state_consistent": gepa_state.is_consistent(),
            "candidate_ids": [item.candidate_id for item in ordered],
            "pareto_selected_index": pareto_index,
            "pareto_selected_candidate_id": ordered[pareto_index].candidate_id,
            "current_best_index": best_index,
            "current_best_candidate_id": ordered[best_index].candidate_id,
            "task_level_scores_preserved": True,
            "frozen_train_task_ids": list(expected_task_ids),
            "frontier_type": "instance",
        }
        self._write("gepa-state-snapshot.json", result)
        return result

    def prepare_reflection(self, candidate_id: str) -> CandidateReflectionWorkItem:
        state = self.state()
        if candidate_id in state.reflected_candidate_ids:
            raise ValueError("candidate already has its single reflection")
        candidate = self._candidate(candidate_id)
        patch = self._candidate_patch(candidate_id)
        application = self._candidate_application(candidate_id)
        feedback: list[CandidateTaskReflection] = []
        for split in ("train", "validation"):
            path = self.run_dir / f"evals/{candidate_id}/{split}/candidate-run-summary.json"
            if not path.is_file():
                continue
            summary = json.loads(path.read_text(encoding="utf-8"))
            for row in summary["pair_summaries"]:
                feedback.append(
                    CandidateTaskReflection(
                        task_id=row["task_id"],
                        paired_delta=row["paired_delta"],
                        evidence_refs=(row["reference_vector_ref"], row["candidate_vector_ref"]),
                        failed_expectation_ids=(),
                        grader_feedback_zh=(
                            "请结合 typed vector、E3 与独立评分证据解释该任务的改进或回归。"
                        ),
                    )
                )
        graph = PackageGraph.model_validate_json(
            (self.run_dir / f"candidates/{candidate_id}/graph.json").read_text(encoding="utf-8")
        )
        reflection_identity = {"candidate": candidate_id, "patch": patch.patch_id}
        work_id = f"reflection-work-{canonical_fingerprint(reflection_identity)[:24]}"
        work = CandidateReflectionWorkItem(
            work_id=work_id,
            candidate_id=candidate_id,
            parent_candidate_ids=candidate.parent_ids,
            patch_ref=(self.run_dir / f"candidates/{candidate_id}/patch.json")
            .relative_to(self.project_root)
            .as_posix(),
            graph_ref=(self.run_dir / f"candidates/{candidate_id}/graph.json")
            .relative_to(self.project_root)
            .as_posix(),
            graph_diff=(
                application.graph_diff.model_dump(mode="json") if application.graph_diff else {}
            ),
            task_feedback=tuple(feedback),
            node_hints=tuple(
                {
                    "node_id": node.node_id,
                    "path": node.path,
                    "kind": node.kind.value,
                    "label": node.label,
                }
                for node in graph.nodes
                if node.node_id in set(application.affected_node_ids)
            )[:120],
        )
        if self.config.active_session_budget_policy is not None:
            self._session().reserve(
                batch_id=f"reflection:{work_id}",
                role="reflection",
                work_ids=(work_id,),
            )
        self._write(f"reflection-work-items/{work_id}.json", work)
        if self.config.active_session_budget_policy is not None:
            self._save_state(self.state())
        return work

    def ingest_reflection(self, submission: CandidateReflectionSubmission) -> dict[str, Any]:
        work = CandidateReflectionWorkItem.model_validate_json(
            (self.run_dir / f"reflection-work-items/{submission.work_id}.json").read_text(
                encoding="utf-8"
            )
        )
        graph = PackageGraph.model_validate_json(
            (self.project_root / work.graph_ref).read_text(encoding="utf-8")
        )
        known_nodes = {node.node_id for node in graph.nodes}
        allowed_refs = {
            ref for feedback in work.task_feedback for ref in feedback.evidence_refs
        } | {work.patch_ref, work.graph_ref}
        for diagnosis in submission.diagnoses:
            if not set(diagnosis.evidence_refs) <= allowed_refs:
                raise ValueError("reflection cited evidence outside its work item")
            if not set(diagnosis.target_node_ids) <= known_nodes:
                raise ValueError("reflection cited unknown candidate graph nodes")
        self._write(f"reflection-submissions/{submission.work_id}.json", submission)
        if self.config.active_session_budget_policy is not None:
            self._session().settle(
                work_id=submission.work_id,
                actual_tokens=submission.token_estimate,
                actual_duration_ms=submission.duration_ms,
            )
        state = self.state()
        reflected = tuple(dict.fromkeys((*state.reflected_candidate_ids, work.candidate_id)))
        usage = state.budget_usage.model_copy(
            update={
                "agent_calls": state.budget_usage.agent_calls + 1,
                "estimated_tokens": state.budget_usage.estimated_tokens + submission.token_estimate,
                "cumulative_agent_duration_ms": (
                    state.budget_usage.cumulative_agent_duration_ms + submission.duration_ms
                ),
            }
        )
        self._save_state(
            state.model_copy(update={"reflected_candidate_ids": reflected, "budget_usage": usage})
        )
        return {"ingested": True, "candidate_id": work.candidate_id}

    @staticmethod
    def _held_out_reference(reference: str) -> bool:
        lowered = reference.casefold()
        return any(
            marker in lowered for marker in ("/validation/", "validation-", "/test/", "held-out")
        )

    def plan_generation2_refinement(
        self,
        parent_candidate_id: str | None = None,
    ) -> Generation2PlanningOutcome:
        """Plan one generation-2 proposal from admitted train evidence only.

        This method creates no Candidate, reserves no Agent work, and does not
        increment proposal accounting.  Export/ingest remains owned by the
        existing PatchProposalStore and ActiveSessionRuntime path.
        """

        state = self.state()
        existing_paths = sorted((self.run_dir / "generation2-plans").glob("*.json"))
        existing = [
            Generation2PlanningOutcome.model_validate_json(path.read_text(encoding="utf-8"))
            for path in existing_paths
        ]
        if parent_candidate_id is not None:
            prior = next(
                (item for item in existing if item.parent_candidate_id == parent_candidate_id),
                None,
            )
            if prior is not None:
                return prior
        if sum(item.planned for item in existing) >= 2:
            outcome = Generation2PlanningOutcome(
                status="candidate_budget_exhausted",
                reason_codes=("bounded_refinement_limit_reached",),
            )
            self._write("generation2-plans/terminal-candidate-cap.json", outcome)
            return outcome
        if state.budget_usage.proposals >= self.config.runtime_budget.max_proposals:
            outcome = Generation2PlanningOutcome(
                status="proposal_budget_exhausted",
                reason_codes=("frozen_proposal_budget_exhausted",),
            )
            self._write("generation2-plans/terminal-proposal-budget.json", outcome)
            return outcome
        if state.budget_usage.candidates >= self.config.runtime_budget.max_candidates - 1:
            outcome = Generation2PlanningOutcome(
                status="candidate_budget_exhausted",
                reason_codes=("merge_candidate_slot_reserved",),
            )
            self._write("generation2-plans/terminal-candidate-budget.json", outcome)
            return outcome

        with EvolutionPoolStore(self.run_dir / "evolution-pool.sqlite3") as pool:
            pool_entries = pool.all()
        eligible = []
        for entry in pool_entries:
            try:
                candidate = self._candidate(entry.candidate_id)
                admission = TrainAdmission.model_validate_json(
                    (self.run_dir / f"train-admission/{entry.candidate_id}.json").read_text(
                        encoding="utf-8"
                    )
                )
                branch = self._branch_for_candidate(entry.candidate_id)
            except (FileNotFoundError, KeyError, ValueError):
                continue
            if (
                candidate.generation != 1
                or not admission.passed
                or not entry.gate_0_1_passed
                or not entry.train_floor_satisfied
                or entry.package_id != candidate.package_id
                or entry.source_snapshot_hash != candidate.snapshot_hash
                or entry.candidate_content_hash != candidate.content_hash
                or entry.branch_id != branch.get("branch_id")
                or branch.get("head_candidate_id") != candidate.candidate_id
                or int(branch.get("generation", 0)) != candidate.generation
            ):
                continue
            eligible.append((entry, candidate, admission, branch))
        if parent_candidate_id is not None:
            eligible = [row for row in eligible if row[1].candidate_id == parent_candidate_id]
        if parent_candidate_id is None and eligible:
            preferred: list[str] = []
            snapshot_path = self.run_dir / "gepa-state-snapshot.json"
            if snapshot_path.is_file():
                snapshot = self._read("gepa-state-snapshot.json")
                preferred.extend(
                    str(snapshot[key])
                    for key in (
                        "pareto_selected_candidate_id",
                        "current_best_candidate_id",
                    )
                    if snapshot.get(key)
                )
            order = {candidate_id: index for index, candidate_id in enumerate(preferred)}
            eligible.sort(
                key=lambda row: (
                    order.get(row[1].candidate_id, len(order)),
                    -row[0].train_mean_delta,
                    row[1].candidate_id,
                )
            )
        if not eligible:
            outcome = Generation2PlanningOutcome(
                status="no_eligible_parent",
                parent_candidate_id=parent_candidate_id,
                reason_codes=("no_train_admitted_generation1_pareto_parent",),
            )
            suffix = parent_candidate_id or "auto"
            self._write(f"generation2-plans/no-eligible-{suffix}.json", outcome)
            return outcome

        entry, parent, admission, branch = eligible[0]
        reflection_matches: list[
            tuple[Path, CandidateReflectionWorkItem, CandidateReflectionSubmission]
        ] = []
        for submission_path in sorted((self.run_dir / "reflection-submissions").glob("*.json")):
            submission = CandidateReflectionSubmission.model_validate_json(
                submission_path.read_text(encoding="utf-8")
            )
            work_path = self.run_dir / f"reflection-work-items/{submission.work_id}.json"
            if not work_path.is_file():
                continue
            reflection_work = CandidateReflectionWorkItem.model_validate_json(
                work_path.read_text(encoding="utf-8")
            )
            if reflection_work.candidate_id == parent.candidate_id:
                reflection_matches.append((submission_path, reflection_work, submission))
        if len(reflection_matches) != 1:
            outcome = Generation2PlanningOutcome(
                status="no_eligible_parent",
                parent_candidate_id=parent.candidate_id,
                parent_generation=parent.generation,
                reason_codes=("exactly_one_reflection_required",),
            )
            self._write(f"generation2-plans/{parent.candidate_id}.json", outcome)
            return outcome
        _reflection_path, reflection_work, reflection = reflection_matches[0]

        frozen_train_task_ids = self._candidate_split_task_ids(parent.candidate_id, "train")
        self._require_exact_task_ids(
            tuple(item.task_id for item in admission.paired_scores),
            frozen_train_task_ids,
            source="generation-2 parent admission",
        )
        train_task_ids = set(frozen_train_task_ids)
        feedback = tuple(
            item for item in reflection_work.task_feedback if item.task_id in train_task_ids
        )
        diagnoses = tuple(
            item
            for item in reflection.diagnoses
            if item.task_id in train_task_ids
            and not any(self._held_out_reference(ref) for ref in item.evidence_refs)
        )
        feedback_task_ids = tuple(item.task_id for item in feedback)
        feedback_complete = True
        try:
            self._require_exact_task_ids(
                feedback_task_ids,
                frozen_train_task_ids,
                source="generation-2 train feedback",
            )
        except ValueError:
            feedback_complete = False
        if not feedback_complete or not diagnoses:
            outcome = Generation2PlanningOutcome(
                status="no_eligible_parent",
                parent_candidate_id=parent.candidate_id,
                parent_generation=parent.generation,
                reason_codes=("train_only_reflection_projection_is_empty",),
            )
            self._write(f"generation2-plans/{parent.candidate_id}.json", outcome)
            return outcome
        feedback_refs = tuple(
            sorted(
                {
                    reference
                    for item in feedback
                    for reference in item.evidence_refs
                    if not self._held_out_reference(reference)
                }
            )
        )
        if any(self._held_out_reference(ref) for ref in feedback_refs):
            raise ValueError("held-out evidence entered generation-2 feedback projection")
        projection = {
            "schema_version": "1.0.0",
            "parent_candidate_id": parent.candidate_id,
            "parent_generation": parent.generation,
            "planned_generation": parent.generation + 1,
            "source_reflection_work_id": reflection_work.work_id,
            "source_reflection_submission_id": reflection.submission_id,
            "task_feedback": [item.model_dump(mode="json") for item in feedback],
            "diagnoses": [item.model_dump(mode="json") for item in diagnoses],
            "train_evidence_refs": list(feedback_refs),
            "held_out_evidence_read": False,
            "summary_omitted_to_prevent_split_mixing": True,
        }
        feedback_relative = f"generation2-feedback/{parent.candidate_id}.json"
        feedback_path = self.run_dir / feedback_relative
        if feedback_path.is_file():
            if self._read(feedback_relative) != projection:
                raise ValueError("generation-2 train feedback projection changed on resume")
        else:
            self._write(feedback_relative, projection)

        application = self._candidate_application(parent.candidate_id)
        if application.workspace_ref is None:
            raise ValueError("generation-2 parent lacks a materialized Package workspace")
        train_run_ref = (
            (self.run_dir / f"evals/{parent.candidate_id}/train")
            .relative_to(self.project_root)
            .as_posix()
        )
        train_run = self._safe_project_ref(train_run_ref, directory=True)
        train_metadata = json.loads((train_run / "run-metadata.json").read_text(encoding="utf-8"))
        if not isinstance(train_metadata, dict):
            raise ValueError("candidate train metadata must be an object")
        selector_view = self.build_selector_graph_view(
            parent,
            package_ref=application.workspace_ref,
            evidence_run_ref=train_run_ref,
            expected_graph_ref=str(train_metadata["package_graph_ref"]),
            evidence_variant="candidate",
            allowed_task_ids=tuple(sorted(train_task_ids)),
            reference_key_hash=str(train_metadata["reference_key_hash"]),
        )
        graph_nodes = {node.node_id: node for node in selector_view.graph.nodes}
        delta_by_task = {item.task_id: item.delta for item in admission.paired_scores}
        ordered_diagnoses = sorted(
            diagnoses,
            key=lambda item: (delta_by_task.get(item.task_id, 0.0), item.task_id),
        )
        selected_diagnosis = next(
            (
                diagnosis
                for diagnosis in ordered_diagnoses
                if any(node_id in graph_nodes for node_id in diagnosis.target_node_ids)
            ),
            None,
        )
        if selected_diagnosis is None:
            outcome = Generation2PlanningOutcome(
                status="no_eligible_parent",
                parent_candidate_id=parent.candidate_id,
                parent_generation=parent.generation,
                reason_codes=("reflection_has_no_parent_graph_target",),
            )
            self._write(f"generation2-plans/{parent.candidate_id}.json", outcome)
            return outcome
        target_ids = tuple(
            item for item in selected_diagnosis.target_node_ids if item in graph_nodes
        )
        feedback_ref = feedback_path.relative_to(self.project_root).as_posix()
        admission_ref = (
            (self.run_dir / f"train-admission/{parent.candidate_id}.json")
            .relative_to(self.project_root)
            .as_posix()
        )
        evidence_refs = (feedback_ref, admission_ref)
        scope = self._select_proposal_scope(
            parent,
            selector_view,
            target_ids=target_ids,
            evidence_refs=evidence_refs,
            excluded_node_ids=set(),
            iteration=parent.generation,
            selector_kind=SelectorKind.GRAPH_GUIDED,
            scope_reason=(
                "Generation-2 refinement is parent-bound and train-only; one distinct "
                "graph-connected companion is allowed after mutation-locus deduplication."
            ),
        )
        with RejectedEditStore(self.run_dir / "rejected.sqlite3") as rejected_store:
            rejected_rows = rejected_store.relevant(
                tuple(node.node_id for node in scope.selected_nodes)
            )
        rejected_history = tuple(item.model_dump(mode="json") for item in rejected_rows)
        identity = {
            "run": self.config.run_id,
            "parent": parent.candidate_id,
            "generation": parent.generation + 1,
            "branch": branch["branch_id"],
            "feedback": canonical_fingerprint(projection),
            "targets": [node.node_id for node in scope.selected_nodes],
        }
        work_id = f"proposal-work-{canonical_fingerprint(identity)[:24]}"
        work = PatchProposalWorkItem(
            work_id=work_id,
            run_id=self.config.run_id,
            task_id=selected_diagnosis.task_id,
            parent_candidate_id=parent.candidate_id,
            parent_snapshot_hash=parent.snapshot_hash,
            parent_content_hash=parent.content_hash,
            selector=scope.full_selection.selector.value,
            selector_graph=selector_view.binding,
            selector_ranking=(scope.ranking_audit if selector_view.binding is not None else None),
            targets=tuple(
                PatchTargetSnapshot(
                    node_id=node.node_id,
                    node_kind=node.kind.value,
                    path=node.path,
                    locator=node.locator,
                    content_hash=node.content_hash,
                    content=self._node_content(
                        self.project_root / application.workspace_ref,
                        node,
                    ),
                    selection=ranked,
                )
                for ranked, node in zip(scope.selected, scope.selected_nodes, strict=True)
            ),
            target_set=scope.target_set,
            allowed_operations=scope.operations,
            edit_budget=self.config.patch_budget,
            evidence_refs=evidence_refs,
            actionable_side_information={
                "generation_contract": {
                    "parent_candidate_id": parent.candidate_id,
                    "parent_generation": parent.generation,
                    "planned_generation": parent.generation + 1,
                    "branch_id": branch["branch_id"],
                    "branch_root_candidate_id": branch["branch_root_candidate_id"],
                    "train_feedback_ref": feedback_ref,
                    "train_evolution_pool_refs": list(entry.train_evidence_refs),
                    "held_out_evidence_read": False,
                    "sibling_evidence_read": False,
                    "merge_path_used": False,
                },
                "failure_evidence": {
                    "kind": "train_refinement",
                    "task_id": selected_diagnosis.task_id,
                    "paired_delta": delta_by_task[selected_diagnosis.task_id],
                    "diagnosis_zh": selected_diagnosis.diagnosis_zh,
                    "recommendation_zh": selected_diagnosis.recommendation_zh,
                },
                "graph_slice": scope.failure_slice.model_dump(mode="json"),
                "causal_contract": {"required": True},
            },
            rejected_history=rejected_history,
            output_instructions=(
                "Return only one typed PackagePatch for this parent-bound generation-2 "
                "train refinement. Do not read held-out, sibling, deployable, or merge evidence."
            ),
        )
        with PatchProposalStore(self.run_dir / "proposal-work.sqlite3") as proposal_store:
            proposal_store.add_work(work)
            proposal_store.write_snapshot(self.run_dir)
        work_relative = f"proposal-work-items/{work.work_id}.json"
        work_path = self.run_dir / work_relative
        if work_path.is_file():
            stored = PatchProposalWorkItem.model_validate_json(
                work_path.read_text(encoding="utf-8")
            )
            if stored != work:
                raise ValueError("generation-2 proposal work changed on resume")
        else:
            self._write(work_relative, work)
        outcome = Generation2PlanningOutcome(
            status="planned",
            parent_candidate_id=parent.candidate_id,
            parent_generation=parent.generation,
            planned_generation=parent.generation + 1,
            branch_id=str(branch["branch_id"]),
            proposal_work_id=work.work_id,
            train_feedback_ref=feedback_ref,
            train_evidence_refs=entry.train_evidence_refs,
            selector_graph_ref=(
                selector_view.binding.selector_graph_ref
                if selector_view.binding is not None
                else None
            ),
            rejected_memory_refs=tuple(item.record_id for item in rejected_rows),
            reason_codes=("train_admitted_pareto_parent_refinement_planned",),
        )
        self._write(f"generation2-plans/{parent.candidate_id}.json", outcome)
        return outcome

    def prepare_recovery_proposal(self, rejected_candidate_id: str) -> PatchProposalWorkItem:
        """Open one new seed-rooted branch from rejected train evidence.

        A rejected edit is never promoted or silently refined in place.  The
        reflection may instead seed another independent mutation branch while
        the original rejection remains durable and the merge reserve remains
        untouched.
        """

        state = self.state()
        if state.budget_usage.proposals >= self.config.runtime_budget.max_proposals:
            raise ValueError("R4 proposal budget is exhausted")
        if state.budget_usage.candidates >= self.config.runtime_budget.max_candidates - 1:
            raise ValueError("R4 must preserve one candidate slot for the merge child")
        admission = TrainAdmission.model_validate_json(
            (self.run_dir / f"train-admission/{rejected_candidate_id}.json").read_text(
                encoding="utf-8"
            )
        )
        if admission.passed:
            raise ValueError("recovery proposal requires a rejected train candidate")
        reflection_paths = sorted((self.run_dir / "reflection-submissions").glob("*.json"))
        matching: list[tuple[Path, CandidateReflectionSubmission]] = []
        for path in reflection_paths:
            submission = CandidateReflectionSubmission.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if path.name != f"{submission.work_id}.json":
                continue
            work = CandidateReflectionWorkItem.model_validate_json(
                (self.run_dir / f"reflection-work-items/{submission.work_id}.json").read_text(
                    encoding="utf-8"
                )
            )
            if work.candidate_id == rejected_candidate_id:
                matching.append((path, submission))
        if len(matching) != 1:
            raise ValueError("rejected candidate requires exactly one ingested reflection")
        reflection_path, reflection = matching[0]

        seed = self._candidate(state.seed_candidate_id)
        reference_key = ReferenceEvidenceKey.model_validate_json(
            (self.run_dir / "reference-evidence-key.json").read_text(encoding="utf-8")
        )
        evidence_run = self._safe_project_ref(self.config.reference_run_ref, directory=True)
        selector_view = self.build_selector_graph_view(
            seed,
            package_ref=seed.source_package_ref,
            evidence_run_ref=self.config.reference_run_ref,
            expected_graph_ref=self.config.package_graph_ref,
            evidence_variant="original",
            allowed_task_ids=self._train_task_ids(evidence_run),
            reference_key_hash=reference_key.key_hash,
        )
        graph = selector_view.graph
        graph_nodes = {node.node_id: node for node in graph.nodes}
        # A recovery branch may replace the same seed node as the rejected edit
        # with a materially different bounded alternative.  It must not overlap
        # any train-admitted contribution reserved for a future merge.
        admitted_ids = {
            path.stem
            for path in (self.run_dir / "train-admission").glob("candidate-*.json")
            if TrainAdmission.model_validate_json(path.read_text(encoding="utf-8")).passed
        }
        excluded = {
            node_id
            for candidate_id in admitted_ids
            for node_id in self._candidate_patch(candidate_id).selected_node_ids
        }
        delta_by_task = {pair.task_id: pair.delta for pair in admission.paired_scores}
        diagnoses = sorted(
            reflection.diagnoses,
            key=lambda item: (delta_by_task.get(item.task_id, 0.0), item.task_id),
        )
        selected_diagnosis: ReflectionDiagnosis | None = None
        seed_target_ids: tuple[str, ...] = ()
        for diagnosis in diagnoses:
            available = tuple(
                node_id
                for node_id in diagnosis.target_node_ids
                if node_id in graph_nodes and node_id not in excluded
            )
            if available:
                selected_diagnosis = diagnosis
                seed_target_ids = available
                break
        if selected_diagnosis is None:
            raise ValueError("reflection did not identify a new seed-resolvable graph target")

        evidence_refs = (
            reflection_path.relative_to(self.project_root).as_posix(),
            f"artifacts/runs/{self.config.run_id}/train-admission/{rejected_candidate_id}.json",
        )
        scope = self._select_proposal_scope(
            seed,
            selector_view,
            target_ids=seed_target_ids,
            evidence_refs=evidence_refs,
            excluded_node_ids=excluded,
            iteration=len(state.branch_candidate_ids),
            selector_kind=SelectorKind.GRAPH_GUIDED,
            scope_reason=(
                "Recovery remains seed-rooted and may include one graph-connected companion "
                "only when the same rejected-candidate diagnosis supports both targets."
            ),
        )
        identity = {
            "run": self.config.run_id,
            "rejected": rejected_candidate_id,
            "reflection": reflection.submission_id,
            "targets": [node.node_id for node in scope.selected_nodes],
        }
        work_id = f"proposal-work-{canonical_fingerprint(identity)[:24]}"
        work = PatchProposalWorkItem(
            work_id=work_id,
            run_id=self.config.run_id,
            task_id=selected_diagnosis.task_id,
            parent_candidate_id=seed.candidate_id,
            parent_snapshot_hash=seed.snapshot_hash,
            parent_content_hash=seed.content_hash,
            selector=scope.full_selection.selector.value,
            selector_graph=selector_view.binding,
            selector_ranking=(scope.ranking_audit if selector_view.binding is not None else None),
            targets=tuple(
                PatchTargetSnapshot(
                    node_id=node.node_id,
                    node_kind=node.kind.value,
                    path=node.path,
                    locator=node.locator,
                    content_hash=node.content_hash,
                    content=self._node_content(self.project_root / seed.source_package_ref, node),
                    selection=ranked,
                )
                for ranked, node in zip(scope.selected, scope.selected_nodes, strict=True)
            ),
            target_set=scope.target_set,
            allowed_operations=scope.operations,
            edit_budget=self.config.patch_budget,
            evidence_refs=evidence_refs,
            actionable_side_information={
                "failure_evidence": {
                    "kind": "rejected_train_candidate",
                    "candidate_id": rejected_candidate_id,
                    "task_id": selected_diagnosis.task_id,
                    "paired_delta": delta_by_task[selected_diagnosis.task_id],
                    "diagnosis_zh": selected_diagnosis.diagnosis_zh,
                    "recommendation_zh": selected_diagnosis.recommendation_zh,
                    "reflection_summary_zh": reflection.summary_zh,
                },
                "graph_slice": scope.failure_slice.model_dump(mode="json"),
                "causal_contract": {"required": True},
                "causal_targets": [
                    {
                        "node_id": node.node_id,
                        "failure_evidence_ids": list(evidence_refs),
                        "causal_path_node_ids": (
                            list(scope.target_set.causal_path_node_ids)
                            if scope.target_set is not None
                            else [node.node_id]
                        ),
                        "expected_affected_assertions": [],
                        "expected_affected_metrics": [
                            "task_correctness",
                            "output_quality",
                            "skill_gain",
                        ],
                        "allowed_operation_classes": [self._operation_for(node).value],
                        "executable_target": node.path.endswith((".py", ".sh")),
                    }
                    for node in scope.selected_nodes
                ],
                "preserve": [
                    "the rejected edit must not be copied implicitly",
                    "unrelated package behavior",
                    "public interfaces unless explicitly targeted",
                ],
            },
            rejected_history=(
                {
                    "candidate_id": rejected_candidate_id,
                    "patch_id": self._candidate_patch(rejected_candidate_id).patch_id,
                    "gate_reason_codes": admission.gate.get("reason_codes", []),
                    "mean_delta": admission.gate.get("checks", {}).get("mean_delta"),
                },
            ),
            output_instructions=(
                "Return only one typed, bounded PackagePatch for the exported seed scope. "
                "Use the rejected-candidate reflection, do not reproduce the rejected edit, "
                "do not read held-out evidence, and do not edit files."
            ),
        )
        if self.config.active_session_budget_policy is not None:
            self._session().reserve(
                batch_id=f"proposal:{work.work_id}",
                role="proposal",
                work_ids=(work.work_id,),
            )
        with PatchProposalStore(self.run_dir / "proposal-work.sqlite3") as store:
            store.add_work(work)
            store.write_snapshot(self.run_dir)
        self._write(f"proposal-work-items/{work.work_id}.json", work)
        plan = self._read("branch-plan.json")
        recovery = list(plan.get("recovery_branches", []))
        recovery.append(
            {
                "branch_index": len(state.branch_candidate_ids),
                "rejected_candidate_id": rejected_candidate_id,
                "reflection_work_id": reflection.work_id,
                "work_id": work.work_id,
                "task_id": work.task_id,
                "target_node_ids": [item.node_id for item in work.targets],
                "seed_rooted": True,
                "held_out_feedback_read": False,
            }
        )
        plan["recovery_branches"] = recovery
        self._write("branch-plan.json", plan)
        self._save_state(state.model_copy(update={"phase": EvolutionPhase.PROPOSAL}))
        return work

    def apply_recovery_proposal(self, work_id: str) -> dict[str, Any]:
        """Apply one reflected proposal as a new independent seed-rooted branch."""

        state = self.state()
        with PatchProposalStore(self.run_dir / "proposal-work.sqlite3") as proposal_store:
            work = proposal_store.get_work(work_id)
            submissions = [item for item in proposal_store.submissions() if item.work_id == work_id]
        if len(submissions) != 1:
            raise ValueError("recovery proposal requires one completed typed submission")
        submission = submissions[0]
        patch = submission.patch
        if patch is None:
            raise ValueError("recovery proposal requires one completed typed submission")
        causality = audit_causality((work,), (submission,))
        if not causality["valid"]:
            raise ValueError("recovery proposal causality audit failed")
        seed = self._candidate(state.seed_candidate_id)
        graph = PackageAnalyzer().analyze(self.project_root / seed.source_package_ref).graph
        with RejectedEditStore(self.run_dir / "rejected.sqlite3") as rejected:
            gate0 = run_schema_gate(seed, patch, graph, rejected_store=rejected)
            application, candidate = apply_package_patch(
                self.project_root,
                seed,
                patch,
                self.run_dir / "candidate-workspaces",
                run_id=self.config.run_id,
            )
            gate1 = run_static_gate(
                self.project_root,
                application,
                baseline_package_root=self.project_root / seed.source_package_ref,
            )
        self._write(f"patches/{patch.patch_id}.json", patch)
        self._write(f"applications/{application.application_id}.json", application)
        self._write(
            f"pre-eval-gates/{patch.patch_id}.json",
            {"gate_0": gate0.model_dump(mode="json"), "gate_1": gate1.model_dump(mode="json")},
        )
        if (
            gate0.outcome is not GateOutcome.PASSED
            or gate1.outcome is not GateOutcome.PASSED
            or application.status is not PatchApplicationStatus.APPLIED
            or candidate is None
            or application.workspace_ref is None
        ):
            raise ValueError("recovery proposal did not survive Gate 0/1")
        if self.config.active_session_budget_policy is not None:
            self._session().record_internal_usage(
                accounting_id=f"candidate:{candidate.candidate_id}",
                usage=UsageAllowance(
                    agent_calls=0,
                    estimated_tokens=0,
                    active_wall_clock_ms=0,
                    candidates=1,
                ),
            )

        root = freeze_lineage_root(
            package_id=seed.package_id,
            source_snapshot_hash=seed.snapshot_hash,
            root_candidate_id=seed.candidate_id,
            root_content_hash=seed.content_hash,
            config_hash=self.config_hash,
        )
        registry = BranchRegistry(root)
        existing = [
            self._read(path.relative_to(self.run_dir).as_posix())
            for path in sorted((self.run_dir / "branches").glob("*.json"))
        ]
        for row in sorted(existing, key=lambda item: int(item["variant_index"])):
            if int(row["generation"]) != 1:
                raise ValueError("R4 recovery currently supports initial branches only")
            recreated = registry.create_initial(
                failure_cluster_id=str(row["failure_cluster_id"]),
                variant_index=int(row["variant_index"]),
                child_candidate_id=str(row["branch_root_candidate_id"]),
                operator=str(row["operator_history"][0]),
            )
            if recreated.branch_id != row["branch_id"]:
                raise ValueError("stored branch identity cannot be reconstructed")
        branch = registry.create_initial(
            failure_cluster_id=f"reflected-train:{work.task_id}",
            variant_index=max(int(row["variant_index"]) for row in existing) + 1,
            child_candidate_id=candidate.candidate_id,
            operator=candidate.operator,
        )
        candidate_graph = (
            PackageAnalyzer().analyze(self.project_root / application.workspace_ref).graph
        )
        self._write(f"candidates/{candidate.candidate_id}/candidate.json", candidate)
        self._write(f"candidates/{candidate.candidate_id}/graph.json", candidate_graph)
        self._write(f"candidates/{candidate.candidate_id}/application.json", application)
        self._write(f"candidates/{candidate.candidate_id}/patch.json", patch)
        self._write(f"branches/{branch.branch_id}.json", branch)
        with CandidateStore(self.run_dir / "candidates.sqlite3") as store:
            store.add_candidate(candidate, CandidateStatus.PROPOSED)
        usage = state.budget_usage.model_copy(
            update={"candidates": state.budget_usage.candidates + 1}
        )
        self._save_state(
            state.model_copy(
                update={
                    "phase": EvolutionPhase.TRAIN_EXECUTION,
                    "branch_candidate_ids": (*state.branch_candidate_ids, candidate.candidate_id),
                    "budget_usage": usage,
                }
            )
        )
        return {
            "candidate_id": candidate.candidate_id,
            "branch": branch.model_dump(mode="json"),
            "gate_0": gate0.outcome.value,
            "gate_1": gate1.outcome.value,
            "seed_rooted": True,
        }

    def audit(self) -> dict[str, Any]:
        state = self.state()
        with PatchProposalStore(self.run_dir / "proposal-work.sqlite3") as proposals:
            work_items = proposals.work_items()
            submissions = proposals.submissions()
        candidate_ids = (*state.branch_candidate_ids, *state.merge_candidate_ids)
        train_coverage: dict[str, int] = {}
        validation_coverage: dict[str, int] = {}
        cache_hits = 0
        isolation_valid = True
        for candidate_id in candidate_ids:
            for split, target in (("train", train_coverage), ("validation", validation_coverage)):
                run = self.run_dir / f"evals/{candidate_id}/{split}"
                if not run.is_dir():
                    target[candidate_id] = 0
                    continue
                with MultiFidelityEvalEngine(
                    self.project_root,
                    run,
                    lifecycle_mode=(
                        RunLifecycleMode.OPEN_EXISTING
                        if self.config.lifecycle_policy is not None
                        else None
                    ),
                ) as engine:
                    target[candidate_id] = len(engine.ledger.submissions())
                cache = self._read(f"evals/{candidate_id}/{split}/reference-cache-audit.json")
                cache_hits += int(cache.get("hit") is True)
                isolation_path = run / "isolation-audit.json"
                if isolation_path.is_file():
                    isolation_valid &= bool(json.loads(isolation_path.read_text()).get("valid"))
        reflection_counts: dict[str, int] = {candidate_id: 0 for candidate_id in candidate_ids}
        for path in (self.run_dir / "reflection-work-items").glob("*.json"):
            work = CandidateReflectionWorkItem.model_validate_json(path.read_text(encoding="utf-8"))
            reflection_counts[work.candidate_id] = reflection_counts.get(work.candidate_id, 0) + 1
        merge_record = (
            self._read("merge/build-record.json")
            if (self.run_dir / "merge/build-record.json").is_file()
            else {}
        )
        merge_outcome_path = self.run_dir / "merge/outcome.json"
        if merge_outcome_path.is_file():
            merge_outcome = MergeOutcome.model_validate_json(
                merge_outcome_path.read_text(encoding="utf-8")
            )
            merge_complete = merge_outcome.status in {
                MergeOutcomeStatus.MATERIALIZED_AND_EVALUATED,
                MergeOutcomeStatus.NO_ELIGIBLE_PARENT_SET,
            }
            merge_cross_package_count = merge_outcome.cross_package_pair_count
        else:
            # The sealed R4 fixture predates MergeOutcome.  Preserve its exact
            # artifacts while recognizing the already evaluated legacy child.
            merge_outcome = None
            merge_complete = (
                self.config.conditional_merge_policy is None
                and bool(state.merge_candidate_ids)
                and set(state.merge_candidate_ids) <= set(state.evaluated_candidate_ids)
                and merge_record.get("same_package") is True
            )
            merge_cross_package_count = int(merge_record.get("cross_package_parent_count", 0))
        selector_cache_paths = sorted(
            (self.run_dir / "selector-graph-cache-audits").glob("*/*/*.json")
        )
        selector_cache_audits = [
            json.loads(path.read_text(encoding="utf-8")) for path in selector_cache_paths
        ]
        selector_cache_required = self.config.selector_graph_policy.mode == "static_observed"
        result = {
            "schema_version": "1.0.0",
            "valid": (
                len(state.branch_candidate_ids) >= 2
                and merge_complete
                and all(count <= 1 for count in reflection_counts.values())
                and merge_cross_package_count == 0
                and isolation_valid
                and (not selector_cache_required or bool(selector_cache_audits))
            ),
            "single_candidate_model": "gepase.optimizer.candidate.PackageCandidate",
            "single_patch_model": "gepase.mutation.schema.PackagePatch",
            "single_eval_engine": "gepase.evals.engine.MultiFidelityEvalEngine",
            "candidate_count": len(candidate_ids),
            "branch_count": len(state.branch_candidate_ids),
            "merge_child_count": len(state.merge_candidate_ids),
            "train_executor_coverage": train_coverage,
            "validation_executor_coverage": validation_coverage,
            "reference_cache_hits": cache_hits,
            "reference_cache_misses": state.budget_usage.cache_misses,
            "selector_graph_cache": {
                "required": selector_cache_required,
                "accesses": len(selector_cache_audits),
                "hits": sum(item.get("hit") is True for item in selector_cache_audits),
                "misses": sum(item.get("hit") is not True for item in selector_cache_audits),
                "audit_refs": [
                    path.relative_to(self.project_root).as_posix() for path in selector_cache_paths
                ],
            },
            "reflection_count_by_candidate": reflection_counts,
            "proposal_causality": audit_causality(work_items, submissions),
            "isolation_valid": isolation_valid,
            "merge": merge_record,
            "merge_outcome": (
                merge_outcome.model_dump(mode="json")
                if merge_outcome is not None
                else {
                    "status": "legacy_materialized_and_evaluated",
                    "compatibility_only": True,
                }
            ),
            "gepa": (
                self._read("gepa-state-snapshot.json")
                if (self.run_dir / "gepa-state-snapshot.json").is_file()
                else None
            ),
            "runtime_budget": self.config.runtime_budget.model_dump(mode="json"),
            "budget_usage": state.budget_usage.model_dump(mode="json"),
            "e1_acceptance_calls": 0,
            "graph_case_pruning": False,
        }
        self._write("r4-audit.json", result)
        return result

    def complete(self) -> dict[str, Any]:
        """Seal a fully evaluated R4 run and aggregate truthful runtime usage.

        Evaluation-role usage is owned by each candidate Functional run, while
        proposal/reflection usage is already charged in ``EvolutionRunState``.
        This method joins those two authoritative sources without charging the
        reused R3 reference evidence as fresh Agent work.
        """

        state = self.state()
        admissions = {
            path.stem: TrainAdmission.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted((self.run_dir / "train-admission").glob("candidate-*.json"))
        }
        required_validation = {
            candidate_id for candidate_id, admission in admissions.items() if admission.passed
        }
        evaluated = set(state.evaluated_candidate_ids)
        if required_validation != evaluated:
            missing = sorted(required_validation - evaluated)
            extra = sorted(evaluated - required_validation)
            raise ValueError(
                f"R4 cannot complete before all admitted candidates resolve validation: "
                f"missing={missing}, extra={extra}"
            )
        outcome_path = self.run_dir / "merge/outcome.json"
        if outcome_path.is_file():
            merge_outcome = MergeOutcome.model_validate_json(
                outcome_path.read_text(encoding="utf-8")
            )
            if merge_outcome.status not in {
                MergeOutcomeStatus.MATERIALIZED_AND_EVALUATED,
                MergeOutcomeStatus.NO_ELIGIBLE_PARENT_SET,
            }:
                raise ValueError("R4 cannot complete before typed MergeOutcome resolves")
            if (
                merge_outcome.status is MergeOutcomeStatus.MATERIALIZED_AND_EVALUATED
                and merge_outcome.merge_candidate_id not in evaluated
            ):
                raise ValueError("evaluated MergeOutcome child is absent from run state")
        elif not state.merge_candidate_ids or not set(state.merge_candidate_ids) <= evaluated:
            raise ValueError("R4 cannot complete before the legacy merge child resolves validation")

        runtime_state: ActiveSessionState | None = None
        if self.config.active_session_budget_policy is not None:
            runtime = self._session()
            runtime_state = runtime.state()
            if runtime_state.status is not RuntimeSessionStatus.ACTIVE:
                raise ValueError("final-report barrier requires an approved active session")
            latest: BudgetCheckpoint | None = None
            if runtime_state.latest_checkpoint_id is not None:
                latest_path = (
                    self.run_dir
                    / "budget-checkpoints"
                    / f"{runtime_state.latest_checkpoint_id}.json"
                )
                latest = BudgetCheckpoint.model_validate_json(
                    latest_path.read_text(encoding="utf-8")
                )
            if latest is None or latest.barrier is not RuntimeBarrier.BEFORE_FINAL_REPORT:
                checkpoint = self.pause_at_barrier(
                    RuntimeBarrier.BEFORE_FINAL_REPORT,
                    candidate_gate_summary={
                        candidate_id: (
                            "deployable"
                            if candidate_id in state.deployable_candidate_ids
                            else "not_deployable"
                        )
                        for candidate_id in state.evaluated_candidate_ids
                    },
                    continuation_risk_zh=(
                        "预注册搜索已收口; 继续后 Core 将冻结 runtime, "
                        "并允许报告从 raw evidence 重建。"
                    ),
                )
                return {
                    "phase": "awaiting_final_report_approval",
                    "search_complete": True,
                    "deployable_candidate_ids": list(state.deployable_candidate_ids),
                    "budget_checkpoint": checkpoint,
                    "audit_valid": False,
                }

        roles: dict[str, dict[str, int]] = {}
        evaluation_calls = 0
        evaluation_tokens = 0
        evaluation_duration_ms = 0
        evaluation_failures = 0
        usage_refs: list[str] = []
        for path in sorted((self.run_dir / "evals").glob("candidate-*/*/usage-report.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            usage_refs.append(path.relative_to(self.project_root).as_posix())
            for role, row in payload.get("roles", {}).items():
                target = roles.setdefault(
                    str(role),
                    {
                        "calls": 0,
                        "failures": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "duration_ms": 0,
                        "tool_calls": 0,
                    },
                )
                usage = row.get("usage", {})
                target["calls"] += int(row.get("calls", 0))
                target["failures"] += int(row.get("failures", 0))
                target["input_tokens"] += int(usage.get("input_tokens", 0))
                target["output_tokens"] += int(usage.get("output_tokens", 0))
                target["duration_ms"] += int(usage.get("duration_ms", 0))
                target["tool_calls"] += int(usage.get("tool_calls", 0))
                evaluation_calls += int(row.get("calls", 0))
                evaluation_failures += int(row.get("failures", 0))
                evaluation_tokens += int(usage.get("input_tokens", 0)) + int(
                    usage.get("output_tokens", 0)
                )
                evaluation_duration_ms += int(usage.get("duration_ms", 0))

        started_at = datetime.fromtimestamp(
            (self.run_dir / "resolved-config.json").stat().st_mtime, tz=UTC
        )
        completed_at = datetime.now(UTC)
        calendar_elapsed_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))
        if self.config.active_session_budget_policy is not None:
            clock = self._session().clock(now=completed_at)
            wall_clock_duration_ms = clock["active_wall_clock_ms"]
            paused_ms = clock["paused_ms"]
        else:
            wall_clock_duration_ms = calendar_elapsed_ms
            paused_ms = 0
        cache_audits = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(
                (self.run_dir / "evals").glob("candidate-*/*/reference-cache-audit.json")
            )
        ]
        cache_hits = 1 + sum(item.get("hit") is True for item in cache_audits)
        cache_misses = sum(item.get("hit") is not True for item in cache_audits)
        projected_usage = state.budget_usage.model_copy(
            update={
                "agent_calls": state.budget_usage.agent_calls + evaluation_calls,
                "estimated_tokens": state.budget_usage.estimated_tokens + evaluation_tokens,
                "cumulative_agent_duration_ms": (
                    state.budget_usage.cumulative_agent_duration_ms + evaluation_duration_ms
                ),
                "wall_clock_duration_ms": wall_clock_duration_ms,
                # The Agent host did not expose queue-enqueue timestamps in this
                # run. Preserve zero as "not observed" and say so explicitly in
                # the report instead of fabricating a derived wait duration.
                "queue_wait_ms": 0,
                "cache_hits": cache_hits,
                "cache_misses": cache_misses,
            }
        )
        accounting_summary: dict[str, Any] | None = None
        if runtime_state is not None:
            accounting_summary = _reconcile_active_session_accounting(
                self.project_root,
                self.run_dir,
                runtime_state,
            )
            authoritative_session_usage = runtime_state.used.model_copy(
                update={"active_wall_clock_ms": wall_clock_duration_ms}
            )
            accounting_summary["authoritative_session_usage"] = (
                authoritative_session_usage.model_dump(mode="json")
            )
            accounting_summary["accepted_projection_for_comparison"] = projected_usage.model_dump(
                mode="json"
            )
            usage = BudgetUsage(
                proposals=authoritative_session_usage.proposals,
                candidates=authoritative_session_usage.candidates,
                agent_calls=authoritative_session_usage.agent_calls,
                estimated_tokens=authoritative_session_usage.estimated_tokens,
                cumulative_agent_duration_ms=(runtime_state.cumulative_agent_duration_ms),
                wall_clock_duration_ms=wall_clock_duration_ms,
                queue_wait_ms=0,
                repairs=authoritative_session_usage.repairs,
                cache_hits=cache_hits,
                cache_misses=cache_misses,
            )
        else:
            usage = projected_usage
        limits = self.config.runtime_budget
        exhausted = {
            "agent_calls": usage.agent_calls > limits.max_agent_calls,
            "estimated_tokens": usage.estimated_tokens > limits.max_estimated_tokens,
            "wall_clock": usage.wall_clock_duration_ms > limits.max_wall_clock_seconds * 1000,
            "proposals": usage.proposals > limits.max_proposals,
            "candidates": usage.candidates > limits.max_candidates,
        }
        runtime = {
            "schema_version": "1.0.0",
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "start_time_source": "resolved-config.json filesystem mtime",
            "wall_clock_duration_ms": wall_clock_duration_ms,
            "calendar_elapsed_ms": calendar_elapsed_ms,
            "active_wall_clock_ms": wall_clock_duration_ms,
            "paused_ms": paused_ms,
            "cumulative_agent_duration_ms": usage.cumulative_agent_duration_ms,
            "parallelism_saved_wall_clock_is_not_usage_reduction": True,
            "max_concurrency": limits.max_concurrency,
            "dependency_barriers": ["executor", "independent_grader", "comparator"],
            "roles": roles,
            "proposal_and_reflection_calls": state.budget_usage.agent_calls,
            "evaluation_calls": evaluation_calls,
            "evaluation_failures": evaluation_failures,
            "usage_report_refs": usage_refs,
            "accounting": accounting_summary,
            "usage_authority": (
                accounting_summary["authority"]
                if accounting_summary is not None
                else "EvolutionRunState + accepted evaluation usage reports"
            ),
            "queue_wait_ms": 0,
            "queue_wait_observed": False,
            "queue_wait_note": (
                "Agent Host did not expose enqueue timestamps; no estimate was invented."
            ),
            "reference_cache": {
                "hits": cache_hits,
                "misses": cache_misses,
                "fresh_candidate_only": True,
                "reused_reference_agent_calls": 0,
            },
            "budget": limits.model_dump(mode="json"),
            "usage": usage.model_dump(mode="json"),
            "exhausted_axes": [key for key, value in exhausted.items() if value],
            "stop_semantics": limits.stop_semantics,
        }
        self._write("scheduler/runtime-report.json", runtime)
        stopped_reason = (
            "required_r4_chain_completed_with_audited_budget_overrun"
            if any(exhausted.values())
            else "required_r4_chain_completed_within_budget"
        )
        completed_state = state.model_copy(
            update={
                "phase": EvolutionPhase.COMPLETE,
                "budget_usage": usage,
                "stopped_reason": stopped_reason,
                "updated_at": completed_at,
            }
        )
        if self.config.active_session_budget_policy is not None:
            self._session().finish(status=RuntimeSessionStatus.COMPLETE, now=completed_at)
        self._save_state(completed_state)
        audit = self.audit()
        if not audit["valid"]:
            raise ValueError("R4 completion audit failed")
        return {
            "phase": EvolutionPhase.COMPLETE.value,
            "deployable_candidate_ids": list(completed_state.deployable_candidate_ids),
            "evaluated_candidate_ids": list(completed_state.evaluated_candidate_ids),
            "runtime_report_ref": (self.run_dir / "scheduler/runtime-report.json")
            .relative_to(self.project_root)
            .as_posix(),
            "exhausted_axes": runtime["exhausted_axes"],
            "audit_valid": True,
        }

    def seal(self) -> dict[str, Any]:
        """Content-index the terminal R4 run after all SQLite owners are closed."""

        if self.state().phase is not EvolutionPhase.COMPLETE:
            raise ValueError("only a complete R4 run may be sealed")
        store = ArtifactStore(self.run_dir)
        store.prune_missing()
        indexed = 0
        for path in sorted(item for item in self.run_dir.rglob("*") if item.is_file()):
            relative = path.relative_to(self.run_dir).as_posix()
            if relative == "artifact-index.json" or relative.endswith(
                (".sqlite3-wal", ".sqlite3-shm")
            ):
                continue
            media_type = (
                "application/json"
                if path.suffix == ".json"
                else "application/vnd.sqlite3"
                if path.suffix == ".sqlite3"
                else "application/octet-stream"
            )
            store.index_existing(relative, media_type)
            indexed += 1
        verification = store.verify().as_dict()
        verification["valid"] = bool(verification["valid"] and verification["unindexed_files"] == 0)
        return {"indexed_files": indexed, **verification}
