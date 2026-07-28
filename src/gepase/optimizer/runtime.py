"""Frozen R4 runtime, reference-cache, budget, and phase contracts.

These models describe the one Core-owned evolution run.  They deliberately do
not execute an Agent Runtime and do not duplicate candidate, evaluation, patch,
or acceptance state.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from gepase.evals.eval_plan import FrozenEvalPlan
from gepase.evals.functional import FunctionalScoringPolicy
from gepase.evals.scores import TaskScoreVector
from gepase.mutation.schema import PatchEditBudget
from gepase.optimizer.acceptance.minibatch import MinibatchPolicy
from gepase.optimizer.acceptance.validation import ValidationPolicy
from gepase.optimizer.candidate import build_seed_candidate
from gepase.schemas.common import FrozenModel
from gepase.store.artifacts import canonical_json_bytes, sha256_bytes


class RoleTimeouts(FrozenModel):
    proposal: int = Field(ge=1)
    executor: int = Field(ge=1)
    independent_grader: int = Field(ge=1)
    comparator: int = Field(ge=1)
    reflection: int = Field(ge=1)


class RuntimeBudget(FrozenModel):
    max_concurrency: int = Field(ge=1, le=32)
    role_timeout_seconds: RoleTimeouts
    max_repair_attempts_per_work: int = Field(ge=0, le=2)
    max_proposals: int = Field(ge=2)
    max_candidates: int = Field(ge=3)
    max_agent_calls: int = Field(ge=1)
    max_estimated_tokens: int = Field(ge=1)
    max_wall_clock_seconds: int = Field(ge=1)
    stop_semantics: str = Field(min_length=1)


class SelectorGraphPolicy(FrozenModel):
    """Controls which trusted graph layers the mutation selector may consume."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    mode: Literal["static", "static_observed"] = "static"
    require_sealed_typed_access: Literal[True] = True
    require_observed_when_access_present: Literal[True] = True
    semantic_hypotheses_enabled: Literal[False] = False
    top_k_audit_limit: int = Field(default=10, ge=1, le=50)

    @property
    def policy_hash(self) -> str:
        return canonical_fingerprint(self.model_dump(mode="json"))


class R4EvolutionConfig(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    run_id: str
    package_ref: str
    package_graph_ref: str
    frozen_plan_ref: str
    scoring_policy_ref: str
    reference_run_ref: str
    reference_variant: Literal["original", "candidate"]
    host: str
    model: str
    provider_snapshot: str
    runtime_environment_fingerprint: str
    tool_policy_fingerprint: str
    host_policy: str
    seed: int
    timeout_seconds: int = Field(ge=1)
    branch_count: int = Field(ge=2)
    selector: Literal["graph", "trace", "round_robin", "random"]
    selector_target_limit: int = Field(ge=1, le=2)
    selector_graph_policy: SelectorGraphPolicy = SelectorGraphPolicy()
    runtime_budget: RuntimeBudget
    patch_budget: PatchEditBudget
    train_policy: MinibatchPolicy
    validation_policy: ValidationPolicy
    enable_e1: Literal[False] = False
    require_ab_ba_comparator: Literal[True] = True
    candidate_reflection_limit: Literal[1] = 1
    require_same_package_merge: Literal[True] = True
    forbid_cross_package_merge: Literal[True] = True

    @model_validator(mode="after")
    def paths_and_budget(self) -> R4EvolutionConfig:
        for value in (
            self.package_ref,
            self.package_graph_ref,
            self.frozen_plan_ref,
            self.scoring_policy_ref,
            self.reference_run_ref,
        ):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("R4 config refs must be repository-relative")
        if self.runtime_budget.max_proposals < self.branch_count:
            raise ValueError("proposal budget cannot cover the required mutation branches")
        if self.runtime_budget.max_candidates < self.branch_count + 1:
            raise ValueError("candidate budget must reserve one same-package merge child")
        if self.selector_graph_policy.mode == "static_observed":
            if self.selector != "graph":
                raise ValueError("static_observed graph policy requires graph selector")
            if self.reference_variant != "original":
                raise ValueError(
                    "seed static_observed graph must bind original reference evidence"
                )
        return self


class ReferenceEvidenceKey(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    reference_run_ref: str
    reference_variant: Literal["original", "candidate"]
    reference_package_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_package_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_plan_artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    case_contract_hashes: dict[str, str]
    fixture_hashes: dict[str, str]
    scoring_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_snapshot: str
    host: str
    model: str
    host_model_snapshot: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_environment_fingerprint: str
    tool_policy_fingerprint: str
    seed: int
    timeout_seconds: int = Field(ge=1)
    host_policy: str
    source_run_artifact_index_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    bound_artifact_hashes: dict[str, str]

    @model_validator(mode="after")
    def complete_anchor(self) -> ReferenceEvidenceKey:
        if not self.case_contract_hashes or not self.fixture_hashes:
            raise ValueError("reference key requires case and fixture hashes")
        required_suffixes = {
            "run-metadata.json",
            "functional-run-summary.json",
            "score-independent-verification.json",
            "package-access-audit.json",
            "isolation-audit.json",
        }
        if not required_suffixes <= set(self.bound_artifact_hashes):
            missing = sorted(required_suffixes - set(self.bound_artifact_hashes))
            raise ValueError(f"reference key misses sealed artifacts: {missing}")
        return self

    @property
    def key_hash(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))


class ReferenceCacheAudit(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    requested_key_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_run_ref: str
    hit: bool
    verified_artifacts: tuple[str, ...]
    mismatches: tuple[str, ...]
    partial_match_used: Literal[False] = False
    stale_evidence_used: Literal[False] = False
    verified_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EvolutionPhase(StrEnum):
    INITIALIZED = "initialized"
    PROPOSAL = "proposal"
    TRAIN_EXECUTION = "train_execution"
    TRAIN_GRADING = "train_grading"
    TRAIN_GATE = "train_gate"
    VALIDATION_EXECUTION = "validation_execution"
    VALIDATION_GRADING = "validation_grading"
    VALIDATION_COMPARISON = "validation_comparison"
    REFLECTION = "reflection"
    MERGE = "merge"
    COMPLETE = "complete"
    BUDGET_EXHAUSTED = "budget_exhausted"


class BudgetUsage(FrozenModel):
    proposals: int = Field(default=0, ge=0)
    candidates: int = Field(default=0, ge=0)
    agent_calls: int = Field(default=0, ge=0)
    estimated_tokens: int = Field(default=0, ge=0)
    cumulative_agent_duration_ms: int = Field(default=0, ge=0)
    wall_clock_duration_ms: int = Field(default=0, ge=0)
    queue_wait_ms: int = Field(default=0, ge=0)
    repairs: int = Field(default=0, ge=0)
    cache_hits: int = Field(default=0, ge=0)
    cache_misses: int = Field(default=0, ge=0)


class EvolutionRunState(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    run_id: str
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    phase: EvolutionPhase
    seed_candidate_id: str
    branch_candidate_ids: tuple[str, ...] = ()
    merge_candidate_ids: tuple[str, ...] = ()
    evaluated_candidate_ids: tuple[str, ...] = ()
    reflected_candidate_ids: tuple[str, ...] = ()
    deployable_candidate_ids: tuple[str, ...] = ()
    budget_usage: BudgetUsage = BudgetUsage()
    stopped_reason: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def load_r4_config(project_root: Path, path: Path) -> tuple[str, R4EvolutionConfig]:
    root = project_root.resolve()
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("R4 config must remain inside the project")
    config = R4EvolutionConfig.model_validate_json(resolved.read_text(encoding="utf-8"))
    payload = config.model_dump(mode="json")
    # A field added with a backward-compatible default must not silently change
    # the fingerprint of a sealed legacy config that never declared it.  New
    # graph-aware configs include the explicit policy in their fingerprint.
    if "selector_graph_policy" not in config.model_fields_set:
        payload.pop("selector_graph_policy")
    return sha256_bytes(canonical_json_bytes(payload)), config


def canonical_fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _safe_project_path(project_root: Path, reference: str, *, directory: bool = False) -> Path:
    path = (project_root / reference).resolve(strict=True)
    if not path.is_relative_to(project_root):
        raise ValueError(f"reference escapes project root: {reference}")
    if directory and not path.is_dir():
        raise ValueError(f"reference is not a directory: {reference}")
    return path


def build_reference_evidence_key(
    project_root: Path,
    config: R4EvolutionConfig,
) -> ReferenceEvidenceKey:
    """Build and fully verify the immutable R3 reference anchor for R4."""

    root = project_root.resolve()
    run = _safe_project_path(root, config.reference_run_ref, directory=True)
    metadata_path = run / "run-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    expected_metadata = {
        "mode": "frozen-functional",
        "host": config.host,
        "model": config.model,
        "seed": config.seed,
        "timeout_seconds": config.timeout_seconds,
        "frozen_plan_ref": config.frozen_plan_ref,
        "scoring_policy_ref": config.scoring_policy_ref,
    }
    mismatches = [
        key for key, expected in expected_metadata.items() if metadata.get(key) != expected
    ]
    if mismatches:
        raise ValueError(f"R3 reference metadata mismatch: {sorted(mismatches)}")

    plan_path = _safe_project_path(root, config.frozen_plan_ref)
    plan = FrozenEvalPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    policy_path = _safe_project_path(root, config.scoring_policy_ref)
    policy = FunctionalScoringPolicy.model_validate_json(
        policy_path.read_text(encoding="utf-8")
    )
    policy_hash = canonical_fingerprint(policy.model_dump(mode="json"))
    if policy.frozen_plan_hash != plan.plan_hash:
        raise ValueError("R4 scoring policy is not bound to the frozen EvalPlan")
    if metadata.get("frozen_plan_hash") != plan.plan_hash:
        raise ValueError("R3 reference run uses another frozen EvalPlan")
    if metadata.get("scoring_policy_hash") != policy_hash:
        raise ValueError("R3 reference run uses another scoring policy")

    seed = build_seed_candidate(root, config.package_ref, run_id=config.run_id)
    if seed.snapshot_hash != plan.package_snapshot_hash:
        raise ValueError("source PackageSnapshot differs from the frozen EvalPlan")

    index_path = run / "artifact-index.json"
    raw_index = json.loads(index_path.read_text(encoding="utf-8"))
    rows = raw_index.get("artifacts") if isinstance(raw_index, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("R3 reference artifact index is empty or invalid")
    bound: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("R3 artifact index contains a non-object row")
        relative = str(row.get("path", ""))
        expected = str(row.get("sha256", ""))
        path = (run / relative).resolve(strict=True)
        if not path.is_relative_to(run):
            raise ValueError("R3 artifact index path escapes its run")
        actual = sha256_bytes(path.read_bytes())
        if actual != expected:
            raise ValueError(f"R3 sealed artifact hash mismatch: {relative}")
        bound[relative] = expected

    original_vectors: dict[str, TaskScoreVector] = {}
    for relative in sorted(key for key in bound if key.startswith("task-score-vectors/")):
        vector = TaskScoreVector.model_validate_json((run / relative).read_text(encoding="utf-8"))
        if vector.variant != config.reference_variant:
            continue
        if vector.task_id in original_vectors:
            raise ValueError(f"duplicate reference vector: {vector.task_id}")
        if vector.candidate_snapshot_hash != seed.content_hash:
            raise ValueError(f"reference vector snapshot mismatch: {vector.task_id}")
        original_vectors[vector.task_id] = vector
    case_ids = {case.case_id for case in plan.functional_cases}
    if set(original_vectors) != case_ids:
        raise ValueError("R3 reference vectors do not cover the complete frozen Functional plan")

    fixture_hashes = {
        binding.ref: binding.sha256
        for case in plan.functional_cases
        for binding in case.fixtures
    }
    for reference, expected in fixture_hashes.items():
        actual = sha256_bytes(_safe_project_path(root, reference).read_bytes())
        if actual != expected:
            raise ValueError(f"frozen fixture hash mismatch: {reference}")
    return ReferenceEvidenceKey(
        reference_run_ref=config.reference_run_ref,
        reference_variant=config.reference_variant,
        reference_package_snapshot_hash=seed.snapshot_hash,
        reference_package_content_hash=seed.content_hash,
        frozen_plan_hash=plan.plan_hash,
        frozen_plan_artifact_hash=sha256_bytes(plan_path.read_bytes()),
        case_contract_hashes={
            case.case_id: canonical_fingerprint(case.model_dump(mode="json"))
            for case in plan.functional_cases
        },
        fixture_hashes=fixture_hashes,
        scoring_policy_hash=policy_hash,
        provider_snapshot=config.provider_snapshot,
        host=config.host,
        model=config.model,
        host_model_snapshot=canonical_fingerprint(
            {"host": config.host, "model": config.model}
        ),
        runtime_environment_fingerprint=config.runtime_environment_fingerprint,
        tool_policy_fingerprint=config.tool_policy_fingerprint,
        seed=config.seed,
        timeout_seconds=config.timeout_seconds,
        host_policy=config.host_policy,
        source_run_artifact_index_hash=sha256_bytes(index_path.read_bytes()),
        bound_artifact_hashes=bound,
    )


def audit_reference_cache(
    project_root: Path,
    key: ReferenceEvidenceKey,
) -> ReferenceCacheAudit:
    """Re-hash every bound reference artifact; partial matches are never hits."""

    root = project_root.resolve()
    mismatches: list[str] = []
    verified: list[str] = []
    try:
        run = _safe_project_path(root, key.reference_run_ref, directory=True)
    except (FileNotFoundError, ValueError) as error:
        return ReferenceCacheAudit(
            requested_key_hash=key.key_hash,
            source_run_ref=key.reference_run_ref,
            hit=False,
            verified_artifacts=(),
            mismatches=(f"reference_run:{error}",),
        )
    index = run / "artifact-index.json"
    if (
        not index.is_file()
        or sha256_bytes(index.read_bytes()) != key.source_run_artifact_index_hash
    ):
        mismatches.append("artifact-index.json")
    for relative, expected in sorted(key.bound_artifact_hashes.items()):
        path = (run / relative).resolve()
        if not path.is_relative_to(run) or not path.is_file():
            mismatches.append(relative)
            continue
        if sha256_bytes(path.read_bytes()) != expected:
            mismatches.append(relative)
            continue
        verified.append(relative)
    return ReferenceCacheAudit(
        requested_key_hash=key.key_hash,
        source_run_ref=key.reference_run_ref,
        hit=not mismatches and len(verified) == len(key.bound_artifact_hashes),
        verified_artifacts=tuple(verified),
        mismatches=tuple(mismatches),
    )
