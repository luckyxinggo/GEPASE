"""Provider-neutral multi-fidelity planning, dispatch, ingest, replay, and aggregation."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from gepase.benchmarks.loader import load_cases, load_manifest
from gepase.evals.cache import cache_key_for
from gepase.evals.errors import WorkTimeout
from gepase.evals.eval_plan import FrozenEvalPlan, FunctionalEvalCase
from gepase.evals.evidence import (
    EvaluationRecord,
    EvidenceProvenance,
    ProviderFailureKind,
    TraceCompleteness,
    TraceStep,
    UsageRecord,
)
from gepase.evals.functional import DeterministicGradingBundle, FunctionalScoringPolicy
from gepase.evals.functional_pipeline import FunctionalEvalCoordinator
from gepase.evals.ingest import validate_submission_contract
from gepase.evals.ledger import EvalLedger
from gepase.evals.paired import aggregate_pairs
from gepase.evals.policy import EvalPolicy
from gepase.evals.providers.artifact import ArtifactProvider
from gepase.evals.providers.assertion import AssertionProvider
from gepase.evals.providers.base import ProviderRegistry
from gepase.evals.providers.delegated import DelegatedProvider
from gepase.evals.providers.functional_assertion import FunctionalAssertionProvider
from gepase.evals.providers.simulation import SIMULATION_PROMPT, SimulationProvider
from gepase.evals.providers.static import StaticProvider
from gepase.evals.recovery import (
    RecoveryDisposition,
    RepairExhaustionTerminalization,
    WorkRecoveryAudit,
    validate_recovery_attempt_binding,
)
from gepase.evals.schema import EvidenceTier, TaskCase
from gepase.evals.work_items import (
    EvalWorkItem,
    PackageAccessEvent,
    PairingSnapshot,
    Variant,
    WorkStatus,
    WorkSubmission,
    canonical_hash,
    executor_view,
    submission_id_for,
    work_id_for,
)
from gepase.optimizer.session_runtime import (
    ActiveSessionBudgetPolicy,
    ActiveSessionRuntime,
    BudgetCheckpoint,
    BudgetContinuationDecision,
    HostAttemptAccounting,
    MeasurementKind,
    RuntimeBarrier,
    RuntimeBudgetBinding,
    RuntimeSessionStatus,
)
from gepase.package.ir import NodeKind, PackageGraph
from gepase.package.loader import load_package
from gepase.run_lifecycle import (
    RunLifecycle,
    RunLifecycleMode,
    RunLifecycleRecord,
    RunLifecycleStatus,
)
from gepase.schemas.common import ArtifactRef
from gepase.store.artifacts import ArtifactStore, atomic_write, canonical_json_bytes, sha256_bytes

PROVIDER_BY_TIER = {
    EvidenceTier.E0_STATIC: "static-v1",
    EvidenceTier.E1_SIMULATED: "agent-simulation-v1",
    EvidenceTier.E2_DELEGATED: "agent-delegated-v1",
}


def _candidate_hash(root: Path, skill_id: str, variant: Variant) -> str:
    if variant == "no-skill":
        return hashlib.sha256(b"no-skill").hexdigest()
    provenance = root / f"benchmarks/skills/{skill_id}/provenance.json"
    if provenance.is_file():
        value = json.loads(provenance.read_text(encoding="utf-8"))
        snapshot = value.get("source_snapshot_hash")
        if isinstance(snapshot, str) and len(snapshot) == 64:
            return snapshot
    return hashlib.sha256(skill_id.encode()).hexdigest()


class MultiFidelityEvalEngine:
    def __init__(
        self,
        project_root: Path,
        run_dir: Path,
        *,
        lifecycle_mode: RunLifecycleMode | None = None,
        expected_config_hash: str | None = None,
        run_id: str | None = None,
        allow_empty_initialization_recovery: bool = False,
    ) -> None:
        self.project_root = project_root.resolve()
        self.run_dir = run_dir.resolve()
        if lifecycle_mode is not None and not self.run_dir.is_relative_to(self.project_root):
            raise ValueError("evaluation run must remain inside the project")
        self.lifecycle: RunLifecycle | None = None
        self.lifecycle_config_hash = expected_config_hash
        create_ledger = True
        if lifecycle_mode is None:
            self.run_dir.mkdir(parents=True, exist_ok=True)
        else:
            expected_hash = expected_config_hash
            metadata_path = self.run_dir / "run-metadata.json"
            record_path = self.run_dir / RunLifecycle.RECORD_NAME
            if (
                lifecycle_mode is not RunLifecycleMode.CREATE_NEW
                and expected_hash is None
                and record_path.is_file()
            ):
                expected_hash = RunLifecycleRecord.model_validate_json(
                    record_path.read_text(encoding="utf-8")
                ).config_hash
            if (
                lifecycle_mode is not RunLifecycleMode.CREATE_NEW
                and expected_hash is None
                and metadata_path.is_file()
            ):
                expected_hash = sha256_bytes(metadata_path.read_bytes())
            lifecycle = RunLifecycle(
                self.run_dir,
                run_id=run_id or self.run_dir.name,
                owner="eval",
                expected_config_hash=expected_hash,
            )
            recovering_empty_initialization = (
                lifecycle_mode is RunLifecycleMode.CREATE_NEW
                and allow_empty_initialization_recovery
                and self.run_dir.exists()
            )
            if recovering_empty_initialization:
                bootstrap_files = (
                    "artifact-index.json",
                    "ledger.sqlite3",
                    "run-lifecycle.json",
                    "runtime-budget-binding.json",
                )
                lifecycle.recover_empty_initialization(
                    required_files=bootstrap_files,
                    allowed_files=bootstrap_files,
                )
                with EvalLedger(
                    self.run_dir / "ledger.sqlite3", create=False, read_only=True
                ) as bootstrap_ledger:
                    if any(bootstrap_ledger.status().values()):
                        raise ValueError("initialization recovery requires an empty Eval ledger")
                record = lifecycle.record()
            else:
                record = lifecycle.prepare(
                    lifecycle_mode,
                    required_files=(
                        "run-metadata.json",
                        "ledger.sqlite3",
                        "ledger-snapshot.json",
                        "artifact-index.json",
                    ),
                    allow_legacy_open=True,
                )
            self.lifecycle = lifecycle if record is not None else None
            self.lifecycle_config_hash = expected_hash
            create_ledger = (
                lifecycle_mode is RunLifecycleMode.CREATE_NEW
                and not recovering_empty_initialization
            )
        self.store = ArtifactStore(self.run_dir)
        self.ledger = EvalLedger(self.run_dir / "ledger.sqlite3", create=create_ledger)
        self.registry = ProviderRegistry()
        self.static_provider = StaticProvider()
        self.registry.register(self.static_provider)
        self.registry.register(SimulationProvider())
        self.registry.register(DelegatedProvider())
        self.artifact_provider = ArtifactProvider()
        self.assertion_provider = AssertionProvider()
        self.functional_assertion_provider = FunctionalAssertionProvider()
        self._closed = False

    def configure_runtime_budget(
        self,
        *,
        policy: ActiveSessionBudgetPolicy,
        config_hash: str,
    ) -> RuntimeBudgetBinding:
        if self.lifecycle is None or self.lifecycle_config_hash != config_hash:
            raise ValueError("runtime budget must bind the strict lifecycle config hash")
        runtime = ActiveSessionRuntime(
            self.run_dir,
            run_id=self.lifecycle.run_id,
            config_hash=config_hash,
            policy=policy,
        )
        runtime.create()
        binding = RuntimeBudgetBinding(
            owner_run_id=self.lifecycle.run_id,
            owner_run_ref=self.run_dir.relative_to(self.project_root).as_posix(),
            config_hash=config_hash,
            policy=policy,
        )
        self.store.write_json("runtime-budget-binding.json", binding.model_dump(mode="json"))
        self.snapshot_ledger()
        return binding

    def bind_runtime_budget(self, binding: RuntimeBudgetBinding) -> None:
        owner = (self.project_root / binding.owner_run_ref).resolve(strict=True)
        if not owner.is_relative_to(self.project_root) or not owner.is_dir():
            raise ValueError("runtime budget owner escapes the project")
        session = ActiveSessionRuntime(
            owner,
            run_id=binding.owner_run_id,
            config_hash=binding.config_hash,
            policy=binding.policy,
        )
        session.state()
        self.store.write_json("runtime-budget-binding.json", binding.model_dump(mode="json"))

    def _budget_runtime(self) -> ActiveSessionRuntime | None:
        path = self.run_dir / "runtime-budget-binding.json"
        if not path.is_file():
            return None
        binding = RuntimeBudgetBinding.model_validate_json(path.read_text(encoding="utf-8"))
        owner = (self.project_root / binding.owner_run_ref).resolve(strict=True)
        if not owner.is_relative_to(self.project_root):
            raise ValueError("runtime budget binding escapes the project")
        runtime = ActiveSessionRuntime(
            owner,
            run_id=binding.owner_run_id,
            config_hash=binding.config_hash,
            policy=binding.policy,
        )
        runtime.state()
        return runtime

    def _owns_runtime_budget(self) -> bool:
        path = self.run_dir / "runtime-budget-binding.json"
        if not path.is_file():
            return False
        binding = RuntimeBudgetBinding.model_validate_json(path.read_text(encoding="utf-8"))
        owner = (self.project_root / binding.owner_run_ref).resolve(strict=True)
        return owner == self.run_dir

    def close(self) -> None:
        if not self._closed:
            self.ledger.close()
            self._closed = True

    def functional_coordinator(self) -> FunctionalEvalCoordinator:
        metadata_path = self.run_dir / "run-metadata.json"
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("mode") == "frozen-candidate":
                from gepase.evals.candidate_pipeline import CandidateFunctionalCoordinator

                return cast(
                    FunctionalEvalCoordinator,
                    CandidateFunctionalCoordinator(
                        self.project_root, self.run_dir, self.ledger, self.store
                    ),
                )
        return FunctionalEvalCoordinator(self.project_root, self.run_dir, self.ledger, self.store)

    def __enter__(self) -> MultiFidelityEvalEngine:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def plan_cases(
        self,
        manifest_path: Path,
        *,
        splits: tuple[str, ...],
        tiers: tuple[EvidenceTier, ...],
        variants: tuple[str, ...],
        host: str,
        model: str,
        case_ids: set[str] | None = None,
        seed: int = 42,
        candidate_ref: str | None = None,
        candidate_snapshot_hash: str | None = None,
    ) -> dict[str, Any]:
        resolved_manifest = manifest_path.resolve(strict=True)
        if not resolved_manifest.is_relative_to(self.project_root):
            raise ValueError("benchmark manifest must be inside the project")
        manifest_ref = resolved_manifest.relative_to(self.project_root).as_posix()
        if (candidate_ref is None) != (candidate_snapshot_hash is None):
            raise ValueError("candidate_ref and candidate_snapshot_hash must be provided together")
        if candidate_ref is not None:
            reference = Path(candidate_ref)
            if reference.is_absolute() or ".." in reference.parts:
                raise ValueError("candidate_ref must be repository-relative")
            if not (self.project_root / reference).resolve().is_relative_to(self.project_root):
                raise ValueError("candidate_ref escapes project root")
        manifest = load_manifest(resolved_manifest)
        cases = load_cases(self.project_root, manifest)
        selected = [
            case
            for case in cases
            if case.split in splits and (case_ids is None or case.id in case_ids)
        ]
        policy = EvalPolicy(
            allowed_tiers=tiers,
            minimum_acceptance_tier=max(tiers, key=lambda tier: int(tier.value[1])),
            seed=seed,
        )
        statuses: list[str] = []
        planned_items = 0
        for case in selected:
            for tier in tiers:
                if tier is EvidenceTier.E3_EXECUTABLE:
                    continue
                for variant in variants:
                    if variant not in {"no-skill", "original", "candidate"}:
                        raise ValueError(f"unsupported variant: {variant}")
                    item, cache_key = self._work_item(
                        case,
                        tier,
                        cast(Variant, variant),
                        policy,
                        host,
                        model,
                        candidate_ref=candidate_ref,
                        candidate_snapshot_hash=candidate_snapshot_hash,
                    )
                    status = self.ledger.plan(item, cache_key)
                    statuses.append(status)
                    self.store.write_json(
                        f"work-items/{item.work_id}.json", item.model_dump(mode="json")
                    )
                    planned_items += 1
                    if tier is EvidenceTier.E0_STATIC and status != "completed":
                        record = self.static_provider.evaluate(item, self.project_root)
                        self.ledger.complete_internal(item, record)
                        self.store.write_json(
                            f"records/{record.record_id}.json", record.model_dump(mode="json")
                        )
        metadata = {
            "schema_version": "1.0.0",
            "manifest": manifest_ref,
            "splits": list(splits),
            "tiers": [tier.value for tier in tiers],
            "variants": list(variants),
            "host": host,
            "model": model,
            "seed": seed,
            "selected_case_ids": sorted(case.id for case in selected),
            "candidate_ref": candidate_ref,
            "candidate_snapshot_hash": candidate_snapshot_hash,
        }
        self.store.write_json("run-metadata.json", metadata)
        self.snapshot_ledger()
        return {
            "selected_cases": len(selected),
            "planned_work_items": planned_items,
            "already_completed": statuses.count("completed"),
            "status": self.ledger.status(),
        }

    def plan_frozen_functional(
        self,
        frozen_plan_path: Path,
        scoring_policy_path: Path,
        *,
        skill_ref: str,
        package_graph_ref: str,
        splits: tuple[str, ...],
        variants: tuple[str, ...],
        host: str,
        model: str,
        seed: int = 42,
        timeout_seconds: int = 600,
    ) -> dict[str, Any]:
        """Plan paired E2 work directly from one immutable R2 EvalPlan."""
        frozen_ref, frozen = self._project_model_ref(frozen_plan_path, FrozenEvalPlan)
        policy_ref, policy = self._project_model_ref(scoring_policy_path, FunctionalScoringPolicy)
        if policy.frozen_plan_hash != frozen.plan_hash:
            raise ValueError("scoring policy is not bound to the frozen EvalPlan")
        skill_path = (self.project_root / skill_ref).resolve(strict=True)
        if not skill_path.is_relative_to(self.project_root):
            raise ValueError("skill_ref must remain inside the project")
        package = load_package(skill_path)
        if package.snapshot_hash != frozen.package_snapshot_hash:
            raise ValueError("skill_ref PackageSnapshot differs from the frozen EvalPlan")
        graph_path = (self.project_root / package_graph_ref).resolve(strict=True)
        if not graph_path.is_relative_to(self.project_root):
            raise ValueError("package_graph_ref must remain inside the project")
        graph = PackageGraph.model_validate_json(graph_path.read_text(encoding="utf-8"))
        if graph.snapshot_hash != frozen.package_snapshot_hash:
            raise ValueError("PackageGraph snapshot differs from the frozen EvalPlan")
        node_map = {node.path: node.node_id for node in graph.nodes if node.kind is NodeKind.FILE}
        selected = [case for case in frozen.functional_cases if case.split in splits]
        if not selected:
            raise ValueError("frozen Functional selection is empty")
        if any(value not in {"no-skill", "original"} for value in variants):
            raise ValueError("R3 frozen planning accepts only no-skill and original")
        if set(variants) != {"no-skill", "original"}:
            raise ValueError("R3 requires a complete no-skill/original pair")
        statuses: list[str] = []
        planned_items = 0
        policy_hash = canonical_hash(policy)
        oracle_module_ref = policy.oracle_ref.split(":", 1)[0]
        oracle_path = (self.project_root / oracle_module_ref).resolve(strict=True)
        if not oracle_path.is_relative_to(self.project_root):
            raise ValueError("functional oracle escapes the project")
        oracle_sha256 = sha256_bytes(oracle_path.read_bytes())
        host_model = canonical_hash({"host": host, "model": model})
        for case in selected:
            fixture_hash = self._validate_functional_fixtures(case)
            pairing = PairingSnapshot(
                prompt_hash=hashlib.sha256(case.prompt.encode()).hexdigest(),
                fixture_hash=fixture_hash,
                policy_hash=policy_hash,
                provider_snapshot=PROVIDER_BY_TIER[EvidenceTier.E2_DELEGATED],
                host_model_snapshot=host_model,
                seed=seed,
            )
            pair_identity = {
                "task": case.case_id,
                "pairing": pairing.model_dump(mode="json"),
            }
            pair_id = f"pair-{canonical_hash(pair_identity)[:24]}"
            for variant_value in variants:
                variant = cast(Variant, variant_value)
                candidate_hash = (
                    hashlib.sha256(b"no-skill").hexdigest()
                    if variant == "no-skill"
                    else frozen.package_snapshot_hash
                )
                identity: dict[str, object] = {
                    "pair_id": pair_id,
                    "task_id": case.case_id,
                    "variant": variant,
                    "tier": EvidenceTier.E2_DELEGATED.value,
                    "candidate_snapshot_hash": candidate_hash,
                    "frozen_plan_hash": frozen.plan_hash,
                    "pairing": pairing.model_dump(mode="json"),
                }
                item = EvalWorkItem(
                    work_id=work_id_for(identity),
                    pair_id=pair_id,
                    task_id=case.case_id,
                    skill_id=frozen.package_id,
                    variant=variant,
                    evidence_tier=EvidenceTier.E2_DELEGATED,
                    provider_id=PROVIDER_BY_TIER[EvidenceTier.E2_DELEGATED],
                    prompt=case.prompt,
                    fixture_ref=case.fixtures[0].ref,
                    fixture_refs=tuple(binding.ref for binding in case.fixtures),
                    skill_ref=None if variant == "no-skill" else skill_ref,
                    package_graph_ref=(None if variant == "no-skill" else package_graph_ref),
                    package_node_map=({} if variant == "no-skill" else node_map),
                    requested_output={
                        key: str(value) for key, value in case.requested_output.model_dump().items()
                    },
                    candidate_snapshot_hash=candidate_hash,
                    frozen_plan_hash=frozen.plan_hash,
                    case_contract_hash=canonical_hash(case),
                    split=case.split,
                    pairing=pairing,
                    required_capabilities=case.required_capabilities,
                    timeout_seconds=timeout_seconds,
                )
                status = self.ledger.plan(item, cache_key_for(item))
                statuses.append(status)
                canonical_item = self.ledger.get_work(item.work_id)
                self.store.write_json(
                    f"work-items/{item.work_id}.json",
                    canonical_item.model_dump(mode="json"),
                )
                self.store.write_json(
                    f"executor-work-items/{item.work_id}.json",
                    executor_view(canonical_item).model_dump(mode="json"),
                )
                planned_items += 1
        e0 = {
            "schema_version": "1.0.0",
            "package_id": frozen.package_id,
            "package_snapshot_hash": frozen.package_snapshot_hash,
            "file_count": len(package.files),
            "graph_nodes": len(graph.nodes),
            "graph_edges": len(graph.edges),
            "diagnostics": [item.model_dump(mode="json") for item in graph.diagnostics],
            "package_quality": self._package_quality(graph),
            "valid": not any(item.severity == "error" for item in graph.diagnostics),
        }
        self.store.write_json("e0-package-record.json", e0)
        metadata = {
            "schema_version": "1.0.0",
            "mode": "frozen-functional",
            "frozen_plan_ref": frozen_ref,
            "frozen_plan_hash": frozen.plan_hash,
            "scoring_policy_ref": policy_ref,
            "scoring_policy_hash": policy_hash,
            "oracle_sha256": oracle_sha256,
            "skill_ref": skill_ref,
            "package_graph_ref": package_graph_ref,
            "splits": list(splits),
            "variants": list(variants),
            "host": host,
            "model": model,
            "seed": seed,
            "timeout_seconds": timeout_seconds,
            "selected_case_ids": [case.case_id for case in selected],
        }
        self.store.write_json("run-metadata.json", metadata)
        self.snapshot_ledger()
        return {
            "selected_cases": len(selected),
            "planned_work_items": planned_items,
            "already_completed": statuses.count("completed"),
            "frozen_plan_hash": frozen.plan_hash,
            "status": self.ledger.status(),
        }

    def plan_frozen_candidate(
        self,
        frozen_plan_path: Path,
        scoring_policy_path: Path,
        reference_key_path: Path,
        *,
        candidate_id: str,
        candidate_content_hash: str,
        candidate_ref: str,
        package_graph_ref: str,
        split: Literal["train", "validation"],
        host: str,
        model: str,
        seed: int = 42,
        timeout_seconds: int = 600,
    ) -> dict[str, Any]:
        """Plan only the fresh candidate side against one verified reference anchor."""

        from gepase.optimizer.runtime import (
            ReferenceEvidenceKey,
            audit_reference_cache,
        )

        frozen_ref, frozen = self._project_model_ref(frozen_plan_path, FrozenEvalPlan)
        policy_ref, policy = self._project_model_ref(scoring_policy_path, FunctionalScoringPolicy)
        key_ref, key = self._project_model_ref(reference_key_path, ReferenceEvidenceKey)
        cache_audit = audit_reference_cache(self.project_root, key)
        if not cache_audit.hit:
            self.store.write_json("reference-cache-audit.json", cache_audit.model_dump(mode="json"))
            raise ValueError("reference evidence cache miss; refresh the reference anchor")
        if policy.frozen_plan_hash != frozen.plan_hash or key.frozen_plan_hash != frozen.plan_hash:
            raise ValueError("candidate run inputs disagree on the frozen EvalPlan")
        policy_hash = canonical_hash(policy)
        if key.scoring_policy_hash != policy_hash:
            raise ValueError("candidate scoring policy differs from the reference anchor")
        if (host, model, seed, timeout_seconds) != (
            key.host,
            key.model,
            key.seed,
            key.timeout_seconds,
        ):
            raise ValueError("candidate host/model/seed/timeout differs from reference anchor")
        skill_path = (self.project_root / candidate_ref).resolve(strict=True)
        if not skill_path.is_relative_to(self.project_root):
            raise ValueError("candidate_ref must remain inside the project")
        package = load_package(skill_path)
        if package.snapshot_hash != candidate_content_hash:
            raise ValueError("materialized candidate content differs from candidate identity")
        graph_path = (self.project_root / package_graph_ref).resolve(strict=True)
        if not graph_path.is_relative_to(self.project_root):
            raise ValueError("candidate PackageGraph must remain inside the project")
        graph = PackageGraph.model_validate_json(graph_path.read_text(encoding="utf-8"))
        if graph.snapshot_hash != candidate_content_hash:
            raise ValueError("candidate PackageGraph differs from candidate content")
        node_map = {node.path: node.node_id for node in graph.nodes if node.kind is NodeKind.FILE}
        selected = [case for case in frozen.functional_cases if case.split == split]
        if not selected:
            raise ValueError(f"frozen candidate split is empty: {split}")
        oracle_module_ref = policy.oracle_ref.split(":", 1)[0]
        oracle_path = (self.project_root / oracle_module_ref).resolve(strict=True)
        oracle_sha256 = sha256_bytes(oracle_path.read_bytes())
        host_model = canonical_hash({"host": host, "model": model})
        statuses: list[str] = []
        for case in selected:
            fixture_hash = self._validate_functional_fixtures(case)
            if key.case_contract_hashes.get(case.case_id) != canonical_hash(case):
                raise ValueError(f"reference case contract mismatch: {case.case_id}")
            pairing = PairingSnapshot(
                prompt_hash=hashlib.sha256(case.prompt.encode()).hexdigest(),
                fixture_hash=fixture_hash,
                policy_hash=policy_hash,
                provider_snapshot=PROVIDER_BY_TIER[EvidenceTier.E2_DELEGATED],
                host_model_snapshot=host_model,
                seed=seed,
            )
            pair_identity = {"task": case.case_id, "reference_key": key.key_hash}
            pair_id = f"pair-{canonical_hash(pair_identity)[:24]}"
            identity: dict[str, object] = {
                "pair_id": pair_id,
                "task_id": case.case_id,
                "variant": "candidate",
                "tier": EvidenceTier.E2_DELEGATED.value,
                "candidate_id": candidate_id,
                "candidate_content_hash": candidate_content_hash,
                "frozen_plan_hash": frozen.plan_hash,
                "reference_key_hash": key.key_hash,
                "pairing": pairing.model_dump(mode="json"),
            }
            item = EvalWorkItem(
                work_id=work_id_for(identity),
                pair_id=pair_id,
                task_id=case.case_id,
                skill_id=frozen.package_id,
                variant="candidate",
                evidence_tier=EvidenceTier.E2_DELEGATED,
                provider_id=PROVIDER_BY_TIER[EvidenceTier.E2_DELEGATED],
                prompt=case.prompt,
                fixture_ref=case.fixtures[0].ref,
                fixture_refs=tuple(binding.ref for binding in case.fixtures),
                skill_ref=candidate_ref,
                package_graph_ref=package_graph_ref,
                package_node_map=node_map,
                requested_output={
                    key_name: str(value)
                    for key_name, value in case.requested_output.model_dump().items()
                },
                candidate_snapshot_hash=candidate_content_hash,
                frozen_plan_hash=frozen.plan_hash,
                case_contract_hash=canonical_hash(case),
                split=case.split,
                pairing=pairing,
                required_capabilities=case.required_capabilities,
                timeout_seconds=timeout_seconds,
            )
            status = self.ledger.plan(item, cache_key_for(item))
            statuses.append(status)
            canonical_item = self.ledger.get_work(item.work_id)
            self.store.write_json(
                f"work-items/{item.work_id}.json", canonical_item.model_dump(mode="json")
            )
            self.store.write_json(
                f"executor-work-items/{item.work_id}.json",
                executor_view(canonical_item).model_dump(mode="json"),
            )
        e0 = {
            "schema_version": "1.0.0",
            "package_id": frozen.package_id,
            "candidate_id": candidate_id,
            "candidate_content_hash": candidate_content_hash,
            "file_count": len(package.files),
            "graph_nodes": len(graph.nodes),
            "graph_edges": len(graph.edges),
            "diagnostics": [item.model_dump(mode="json") for item in graph.diagnostics],
            "package_quality": self._package_quality(graph),
            "valid": not any(item.severity == "error" for item in graph.diagnostics),
        }
        self.store.write_json("e0-package-record.json", e0)
        self.store.write_json("reference-cache-audit.json", cache_audit.model_dump(mode="json"))
        self.store.write_json(
            "run-metadata.json",
            {
                "schema_version": "1.0.0",
                "mode": "frozen-candidate",
                "candidate_id": candidate_id,
                "candidate_content_hash": candidate_content_hash,
                "candidate_ref": candidate_ref,
                "package_graph_ref": package_graph_ref,
                "frozen_plan_ref": frozen_ref,
                "frozen_plan_hash": frozen.plan_hash,
                "scoring_policy_ref": policy_ref,
                "scoring_policy_hash": policy_hash,
                "oracle_sha256": oracle_sha256,
                "reference_key_ref": key_ref,
                "reference_key_hash": key.key_hash,
                "reference_run_ref": key.reference_run_ref,
                "reference_variant": key.reference_variant,
                "split": split,
                "host": host,
                "model": model,
                "seed": seed,
                "timeout_seconds": timeout_seconds,
                "selected_case_ids": [case.case_id for case in selected],
            },
        )
        self.snapshot_ledger()
        return {
            "selected_cases": len(selected),
            "planned_work_items": len(selected),
            "already_completed": statuses.count("completed"),
            "reference_cache_hit": True,
            "reference_key_hash": key.key_hash,
            "status": self.ledger.status(),
        }

    def _project_model_ref(self, path: Path, model: type[Any]) -> tuple[str, Any]:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.project_root):
            raise ValueError("input model must remain inside the project")
        reference = resolved.relative_to(self.project_root).as_posix()
        return reference, model.model_validate_json(resolved.read_text(encoding="utf-8"))

    def _validate_functional_fixtures(self, case: FunctionalEvalCase) -> str:
        values: list[dict[str, str]] = []
        for binding in case.fixtures:
            path = (self.project_root / binding.ref).resolve(strict=True)
            if not path.is_relative_to(self.project_root):
                raise ValueError("functional fixture escapes the project")
            actual = sha256_bytes(path.read_bytes())
            if actual != binding.sha256:
                raise ValueError(f"functional fixture hash mismatch: {binding.ref}")
            values.append({"ref": binding.ref, "sha256": binding.sha256})
        return canonical_hash(values)

    @staticmethod
    def _package_quality(graph: PackageGraph) -> float:
        penalty = sum(
            1.0 if item.severity == "error" else 0.2 if item.severity == "warning" else 0.02
            for item in graph.diagnostics
        )
        return max(0.0, min(1.0, 1.0 - penalty / max(1, len(graph.nodes))))

    def _work_item(
        self,
        case: TaskCase,
        tier: EvidenceTier,
        variant: Variant,
        policy: EvalPolicy,
        host: str,
        model: str,
        *,
        candidate_ref: str | None = None,
        candidate_snapshot_hash: str | None = None,
    ) -> tuple[EvalWorkItem, str]:
        if tier not in case.allowed_evidence_tiers:
            raise ValueError(f"{case.id} does not allow {tier.value}")
        prompt = case.prompt + (
            f"\n\n{SIMULATION_PROMPT}" if tier is EvidenceTier.E1_SIMULATED else ""
        )
        provider_id = PROVIDER_BY_TIER[tier]
        host_model = (
            "none"
            if tier is EvidenceTier.E0_STATIC
            else canonical_hash({"host": host, "model": model})
        )
        pairing = PairingSnapshot(
            prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
            fixture_hash=case.fixture_sha256,
            policy_hash=canonical_hash(policy),
            provider_snapshot=provider_id,
            host_model_snapshot=host_model,
            seed=policy.seed,
        )
        candidate_hash = (
            candidate_snapshot_hash
            if variant == "candidate" and candidate_snapshot_hash is not None
            else _candidate_hash(self.project_root, case.skill_id, variant)
        )
        pair_identity = {
            "task": case.id,
            "tier": tier.value,
            "pairing": pairing.model_dump(mode="json"),
        }
        pair_id = f"pair-{canonical_hash(pair_identity)[:24]}"
        required = (
            ("package_structure",)
            if tier is EvidenceTier.E0_STATIC
            else ("plan_task",)
            if tier is EvidenceTier.E1_SIMULATED
            else case.required_capability
        )
        skill_ref = (
            None
            if variant == "no-skill"
            else candidate_ref
            if variant == "candidate" and candidate_ref is not None
            else f"benchmarks/skills/{case.skill_id}"
        )
        output = {str(key): str(value) for key, value in case.input["requested_output"].items()}
        identity: dict[str, object] = {
            "pair_id": pair_id,
            "task_id": case.id,
            "skill_id": case.skill_id,
            "variant": variant,
            "tier": tier.value,
            "provider_id": provider_id,
            "candidate_snapshot_hash": candidate_hash,
            "pairing": pairing.model_dump(mode="json"),
        }
        item = EvalWorkItem(
            work_id=work_id_for(identity),
            pair_id=pair_id,
            task_id=case.id,
            skill_id=case.skill_id,
            variant=variant,
            evidence_tier=tier,
            provider_id=provider_id,
            prompt=prompt,
            fixture_ref=case.fixture_ref,
            skill_ref=skill_ref,
            requested_output=output,
            candidate_snapshot_hash=candidate_hash,
            pairing=pairing,
            required_capabilities=required,
            timeout_seconds=policy.timeout_seconds,
        )
        cache_key = cache_key_for(item)
        self.registry.get(provider_id, item)
        return item, cache_key

    def export_work(self, output: Path, limit: int | None = None) -> dict[str, Any]:
        preview = self.ledger.ready_items(limit)
        runtime = self._budget_runtime()
        if runtime is not None and preview:
            work_ids = tuple(item.work_id for item in preview)
            batch_id = f"executor:{sha256_bytes(canonical_json_bytes(work_ids))[:24]}"
            runtime.reserve(
                batch_id=batch_id,
                role="executor",
                work_ids=work_ids,
            )
        items = self.ledger.export_ready(limit)
        if tuple(item.work_id for item in items) != tuple(item.work_id for item in preview):
            raise ValueError("evaluation export changed after budget reservation")
        exported_items = (
            [executor_view(item) for item in items] if self._is_functional_run() else list(items)
        )
        payload = {
            "schema_version": "1.0.0",
            "work_items": [item.model_dump(mode="json") for item in exported_items],
            "count": len(items),
        }
        atomic_write(output, canonical_json_bytes(payload))
        if output.resolve().is_relative_to(self.run_dir):
            relative = output.resolve().relative_to(self.run_dir).as_posix()
            self.store.write_json(relative, payload)
        self.snapshot_ledger()
        return {"exported": len(items), "output": output.as_posix()}

    def ingest(self, submission: WorkSubmission, *, auto_assert: bool = True) -> dict[str, Any]:
        return self._ingest(
            submission,
            auto_assert=auto_assert,
            preaccounted_host_attempt_ids=(),
        )

    def ingest_recovered_submission(
        self,
        submission: WorkSubmission,
        audit: WorkRecoveryAudit,
        *,
        auto_assert: bool = True,
    ) -> dict[str, Any]:
        """Ingest deterministic repackaging without charging a new Agent call."""

        if audit.work_id != submission.work_id:
            raise ValueError("recovery audit belongs to another submission")
        if audit.disposition is not RecoveryDisposition.RECOVERABLE_WITHOUT_AGENT:
            raise ValueError("only recoverable evidence can use deterministic ingest")
        if not audit.host_attempt_accounting_ids:
            raise ValueError("recovered submission must cite preaccounted HostAttempts")
        runtime = self._budget_runtime()
        if runtime is None:
            raise ValueError("recovered submission requires a bound runtime")
        item = self.ledger.get_work(submission.work_id)
        host_attempts = tuple(
            HostAttemptAccounting.model_validate_json(
                (
                    runtime.run_dir
                    / "host-attempt-accounting"
                    / f"{accounting_id}.json"
                ).read_text(encoding="utf-8")
            )
            for accounting_id in audit.host_attempt_accounting_ids
        )
        binding = validate_recovery_attempt_binding(
            self.project_root,
            audit,
            expected_run_id=runtime.run_id,
            expected_task_id=item.task_id,
            host_attempt_accountings=host_attempts,
        )
        if (
            submission.work_id != binding.work_id
            or submission.host_task_id != binding.host_task_id
            or (submission.context_id or submission.host_task_id) != binding.context_id
            or submission.repair_attempt != binding.repair_attempt
        ):
            raise ValueError("recovered submission disagrees with the bound source attempt")
        runtime.validate_preaccounted_failure(
            work_id=submission.work_id,
            host_attempt_accounting_ids=audit.host_attempt_accounting_ids,
        )
        self.store.write_json_append_only(
            f"recovery-audits/{audit.audit_id}.json",
            audit.model_dump(mode="json"),
        )
        return self._ingest(
            submission,
            auto_assert=auto_assert,
            preaccounted_host_attempt_ids=audit.host_attempt_accounting_ids,
        )

    def _ingest(
        self,
        submission: WorkSubmission,
        *,
        auto_assert: bool,
        preaccounted_host_attempt_ids: tuple[str, ...],
    ) -> dict[str, Any]:
        item = self.ledger.get_work(submission.work_id)
        validate_submission_contract(item, submission)
        elapsed = (submission.finished_at - submission.started_at).total_seconds()
        if elapsed > item.timeout_seconds and submission.failure_kind is None:
            raise WorkTimeout(f"submission exceeded {item.timeout_seconds}s timeout")
        provider = self.registry.get(submission.provider_id, item)
        if submission.failure_kind is None:
            if item.evidence_tier is EvidenceTier.E2_DELEGATED:
                artifact_root = self.artifact_provider.verify(self.project_root, submission)
                self._index_delegated_artifacts(artifact_root, submission)
            provider.validate_submission(item, submission)
            record = provider.normalize_evidence(item, submission)
            failed = False
        else:
            record = self._failure_record(item, submission)
            failed = True
        derived_candidate: EvaluationRecord | None = None
        deterministic_bundle: DeterministicGradingBundle | None = None
        if auto_assert and not failed and item.evidence_tier is EvidenceTier.E2_DELEGATED:
            case = self._case(item.task_id)
            if isinstance(case, FunctionalEvalCase):
                derived_candidate, deterministic_bundle = (
                    self.functional_assertion_provider.evaluate(
                        self.project_root,
                        self.run_dir,
                        case,
                        record,
                        self._functional_scoring_policy(),
                    )
                )
            else:
                derived_candidate = self.assertion_provider.evaluate(
                    self.project_root, case, record
                )
        stored, duplicate = self.ledger.store_submission(submission, record, failed=failed)
        self.store.write_json(f"records/{stored.record_id}.json", stored.model_dump(mode="json"))
        derived: EvaluationRecord | None = None
        if derived_candidate is not None:
            existing_derived = self.ledger.record_for_work(derived_candidate.work_id)
            if existing_derived is None:
                self.ledger.store_derived_record(derived_candidate)
                derived = derived_candidate
                self.store.write_json(
                    f"records/{derived.record_id}.json", derived.model_dump(mode="json")
                )
                if deterministic_bundle is not None:
                    self.store.write_json(
                        f"deterministic/{item.work_id}.json",
                        deterministic_bundle.model_dump(mode="json"),
                    )
            else:
                derived = existing_derived
        runtime = self._budget_runtime()
        if runtime is not None and (not duplicate or preaccounted_host_attempt_ids):
            if preaccounted_host_attempt_ids:
                runtime.settle_preaccounted_work(
                    work_id=item.work_id,
                    host_attempt_accounting_ids=preaccounted_host_attempt_ids,
                    require_post_recovery_checkpoint=True,
                )
            else:
                runtime.settle(
                    work_id=item.work_id,
                    actual_tokens=(submission.usage.input_tokens or 0)
                    + (submission.usage.output_tokens or 0),
                    actual_duration_ms=submission.usage.duration_ms,
                    repairs=1 if submission.repair_attempt else 0,
                    token_count_kind=MeasurementKind(submission.usage.token_count_kind),
                )
        self.snapshot_ledger()
        result = {
            "record_id": stored.record_id,
            "duplicate": duplicate,
            "derived_record_id": derived.record_id if derived else None,
            "status": "failed" if failed else "completed",
        }
        if (
            runtime is not None
            and self._owns_runtime_budget()
            and runtime.state().status is RuntimeSessionStatus.ACTIVE
            and not any(
                status in {WorkStatus.PENDING, WorkStatus.EXPORTED}
                for status in self.ledger.work_statuses().values()
            )
        ):
            result["budget_checkpoint"] = self.pause_at_barrier(
                barrier=RuntimeBarrier.REFERENCE_EXECUTION_COMPLETE,
                next_role="independent_grader",
                next_work_count=len(self.ledger.submissions()),
                continuation_risk_zh=(
                    "fresh paired Executor work 已完整 ingest; "
                    "继续后将导出隔离 Independent Grader 批次。"
                ),
            )
        return result

    def pause_after_recovery(
        self,
        *,
        recovered_work_ids: tuple[str, ...],
        next_role: str,
        next_work_count: int,
        continuation_risk_zh: str,
    ) -> dict[str, Any]:
        """Write a recovery evidence snapshot and force a fresh continuation checkpoint."""

        runtime = self._budget_runtime()
        if runtime is None:
            raise ValueError("post-recovery checkpoint requires a bound runtime")
        state = runtime.state()
        if set(recovered_work_ids) != set(state.uncheckpointed_recovery_work_ids):
            raise ValueError("checkpoint work set differs from uncheckpointed recovery ingest")
        statuses = self.ledger.work_statuses()
        if any(statuses.get(work_id) is not WorkStatus.COMPLETED for work_id in recovered_work_ids):
            raise ValueError("post-recovery checkpoint requires completed recovered work")
        self.snapshot_ledger()
        audit_rows: list[dict[str, str]] = []
        for work_id in sorted(recovered_work_ids):
            matches: list[tuple[Path, WorkRecoveryAudit]] = []
            for path in sorted((self.run_dir / "recovery-audits").glob("*.json")):
                audit = WorkRecoveryAudit.model_validate_json(path.read_text(encoding="utf-8"))
                if audit.work_id == work_id:
                    matches.append((path, audit))
            if len(matches) != 1:
                raise ValueError("each recovered work must have exactly one committed audit")
            audit_path, audit = matches[0]
            if audit.disposition is not RecoveryDisposition.RECOVERABLE_WITHOUT_AGENT:
                raise ValueError("post-recovery checkpoint cites a non-recoverable audit")
            audit_rows.append(
                {
                    "work_id": work_id,
                    "audit_id": audit.audit_id,
                    "ref": audit_path.relative_to(self.project_root).as_posix(),
                    "sha256": sha256_bytes(audit_path.read_bytes()),
                }
            )
        settlement_rows: list[dict[str, str]] = []
        for work_id in sorted(recovered_work_ids):
            path = runtime.run_dir / "reservation-settlements" / f"{work_id}.json"
            if not path.is_file():
                raise ValueError("recovered work is missing its reservation settlement")
            settlement_rows.append(
                {
                    "work_id": work_id,
                    "ref": path.relative_to(self.project_root).as_posix(),
                    "sha256": sha256_bytes(path.read_bytes()),
                }
            )
        ledger_snapshot = self.run_dir / "ledger-snapshot.json"
        evidence = {
            "schema_version": "1.0.0",
            "kind": "post_recovery_checkpoint_evidence",
            "run_id": runtime.run_id,
            "config_hash": runtime.config_hash,
            "eval_run_ref": self.run_dir.relative_to(self.project_root).as_posix(),
            "recovered_work_ids": sorted(recovered_work_ids),
            "recovery_audits": audit_rows,
            "ledger_snapshot": {
                "ref": ledger_snapshot.relative_to(self.project_root).as_posix(),
                "sha256": sha256_bytes(ledger_snapshot.read_bytes()),
                "status": self.ledger.status(),
                "work_statuses": {
                    work_id: status.value
                    for work_id, status in sorted(statuses.items())
                },
            },
            "runtime_state_before_checkpoint": {
                "sha256": sha256_bytes(runtime.state_path.read_bytes()),
                "state": state.model_dump(mode="json"),
            },
            "reservation_settlements": settlement_rows,
            "prior_checkpoint_id": state.latest_checkpoint_id,
            "new_agent_calls": 0,
            "new_agent_tokens": 0,
            "new_agent_duration_ms": 0,
        }
        evidence_hash = sha256_bytes(canonical_json_bytes(evidence))
        evidence_ref = f"post-recovery-checkpoints/evidence-{evidence_hash[:24]}.json"
        owner_store = ArtifactStore(runtime.run_dir)
        owner_store.write_json_append_only(evidence_ref, evidence)
        complete_states = {WorkStatus.COMPLETED, WorkStatus.FAILED}
        checkpoint = runtime.pause(
            barrier=RuntimeBarrier.POST_RECOVERY_CHECKPOINT,
            evidence_hash=evidence_hash,
            completed_work_ids=tuple(
                work_id for work_id, status in statuses.items() if status in complete_states
            ),
            not_exported_work_ids=tuple(
                work_id for work_id, status in statuses.items() if status is WorkStatus.PENDING
            ),
            candidate_gate_summary={
                "recovery": "two deterministic ingests complete; candidate Gate not run"
            },
            next_batch_estimate=runtime.estimate_batch(next_role, next_work_count),
            continuation_risk_zh=continuation_risk_zh,
        )
        freshness = runtime.audit_checkpoint_freshness(checkpoint.checkpoint_id)
        freshness_ref = (
            f"post-recovery-checkpoints/{freshness.audit_id}.json"
        )
        owner_store.write_json_append_only(freshness_ref, freshness.model_dump(mode="json"))
        stale_checkpoint_rejected = False
        stale_checkpoint_reason = "no prior checkpoint"
        if state.latest_checkpoint_id is not None:
            try:
                runtime.audit_checkpoint_freshness(state.latest_checkpoint_id)
            except ValueError as error:
                stale_checkpoint_rejected = True
                stale_checkpoint_reason = str(error)
        self.snapshot_ledger()
        return {
            "checkpoint": checkpoint.model_dump(mode="json"),
            "checkpoint_sha256": sha256_bytes(
                (
                    runtime.run_dir
                    / "budget-checkpoints"
                    / f"{checkpoint.checkpoint_id}.json"
                ).read_bytes()
            ),
            "freshness_audit": freshness.model_dump(mode="json"),
            "freshness_audit_ref": (
                runtime.run_dir / freshness_ref
            ).relative_to(self.project_root).as_posix(),
            "evidence_ref": (runtime.run_dir / evidence_ref)
            .relative_to(self.project_root)
            .as_posix(),
            "evidence_hash": evidence_hash,
            "prior_checkpoint_rejected": stale_checkpoint_rejected,
            "prior_checkpoint_rejection_reason": stale_checkpoint_reason,
            "agent_calls": 0,
        }

    def terminalize_repair_exhaustion(
        self,
        terminalization: RepairExhaustionTerminalization,
    ) -> dict[str, Any]:
        """Commit one typed failure after all allowed Agent attempts are exhausted.

        Host contexts must already be represented by append-only
        HostAttemptAccounting in the bound runtime.  This method records no new
        Agent call/token/time; it only closes the reservation and makes the
        existing Candidate pipeline's typed-failure path reachable.
        """

        runtime = self._budget_runtime()
        if runtime is None:
            raise ValueError("repair exhaustion requires a bound active-session runtime")
        if (
            terminalization.run_id != runtime.run_id
            or terminalization.config_hash != runtime.config_hash
        ):
            raise ValueError("terminalization belongs to another runtime/config")
        item = self.ledger.get_work(terminalization.work_id)
        source_path = (self.project_root / terminalization.source_submission_ref).resolve(
            strict=True
        )
        if not source_path.is_relative_to(self.project_root):
            raise ValueError("terminalization source submission escapes project root")
        source_bytes = source_path.read_bytes()
        if sha256_bytes(source_bytes) != terminalization.source_submission_sha256:
            raise ValueError("terminalization source submission hash mismatch")
        source = WorkSubmission.model_validate_json(source_bytes)
        if source.work_id != item.work_id:
            raise ValueError("terminalization source belongs to another work")
        runtime.validate_preaccounted_failure(
            work_id=item.work_id,
            host_attempt_accounting_ids=terminalization.host_attempt_accounting_ids,
        )
        failure_identity: dict[str, object] = {
            "terminalization_id": terminalization.terminalization_id,
            "source_submission_id": source.submission_id,
            "failure_kind": terminalization.failure_kind.value,
        }
        failure_submission = source.model_copy(
            update={
                "submission_id": submission_id_for(failure_identity),
                "artifact_root": None,
                "artifacts": (),
                "transcript": None,
                "failure_kind": terminalization.failure_kind,
                "failure_detail": terminalization.failure_detail,
                "uncertainty": 1.0,
            }
        )
        validate_submission_contract(item, failure_submission)
        record = self._failure_record(item, failure_submission)
        self.store.write_json_append_only(
            f"recovery-terminalizations/{terminalization.terminalization_id}.json",
            terminalization.model_dump(mode="json"),
        )
        stored, duplicate = self.ledger.store_submission(
            failure_submission,
            record,
            failed=True,
        )
        self.store.write_json(
            f"records/{stored.record_id}.json",
            stored.model_dump(mode="json"),
        )
        runtime_state = runtime.settle_preaccounted_failure(
            work_id=item.work_id,
            host_attempt_accounting_ids=terminalization.host_attempt_accounting_ids,
        )
        self.snapshot_ledger()
        return {
            "terminalization_id": terminalization.terminalization_id,
            "record_id": stored.record_id,
            "work_id": item.work_id,
            "status": "failed",
            "failure_kind": terminalization.failure_kind.value,
            "duplicate": duplicate,
            "agent_usage_added": False,
            "runtime_used": runtime_state.used.model_dump(mode="json"),
        }

    def _failure_record(self, item: EvalWorkItem, submission: WorkSubmission) -> EvaluationRecord:
        provenance = EvidenceProvenance(
            origin=(
                "simulation" if item.evidence_tier is EvidenceTier.E1_SIMULATED else "agent-native"
            ),
            provider_id=item.provider_id,
            host=submission.host,
            model=submission.model,
            host_task_id=submission.host_task_id,
            context_id=submission.context_id,
            submission_id=submission.submission_id,
            generated_by="gepase.eval.ingest.failure",
        )
        failure_identity = {
            "submission": submission.submission_id,
            "failure": submission.failure_kind,
        }
        return EvaluationRecord(
            record_id=f"record-{canonical_hash(failure_identity)[:24]}",
            work_id=item.work_id,
            pair_id=item.pair_id,
            task_id=item.task_id,
            skill_id=item.skill_id,
            variant=item.variant,
            evidence_tier=item.evidence_tier,
            candidate_snapshot_hash=item.candidate_snapshot_hash,
            prompt_hash=item.pairing.prompt_hash,
            fixture_hash=item.pairing.fixture_hash,
            policy_hash=item.pairing.policy_hash,
            provider_snapshot=item.pairing.provider_snapshot,
            host_model_snapshot=item.pairing.host_model_snapshot,
            seed=item.pairing.seed,
            planned_trace=submission.planned_trace,
            observed_trace=submission.observed_trace,
            trace_completeness=(
                TraceCompleteness.PARTIAL
                if submission.observed_trace
                else TraceCompleteness.PLANNED_ONLY
                if submission.planned_trace
                else TraceCompleteness.NONE
            ),
            artifact_root=submission.artifact_root,
            artifacts=submission.artifacts,
            usage=submission.usage,
            uncertainty=submission.uncertainty,
            provenance=provenance,
            failure_kind=submission.failure_kind,
            failure_detail=submission.failure_detail,
        )

    def _case(self, task_id: str) -> TaskCase | FunctionalEvalCase:
        metadata = json.loads((self.run_dir / "run-metadata.json").read_text(encoding="utf-8"))
        if metadata.get("mode") in {"frozen-functional", "frozen-candidate"}:
            plan = FrozenEvalPlan.model_validate_json(
                (self.project_root / metadata["frozen_plan_ref"]).read_text(encoding="utf-8")
            )
            for case in plan.functional_cases:
                if case.case_id == task_id:
                    return case
            raise KeyError(f"frozen functional case not found: {task_id}")
        manifest = load_manifest(self.project_root / metadata["manifest"])
        for case in load_cases(self.project_root, manifest):
            if case.id == task_id:
                return case
        raise KeyError(f"task case not found: {task_id}")

    def _functional_scoring_policy(self) -> FunctionalScoringPolicy:
        metadata = json.loads((self.run_dir / "run-metadata.json").read_text(encoding="utf-8"))
        if metadata.get("mode") not in {"frozen-functional", "frozen-candidate"}:
            raise ValueError("run is not bound to a frozen Functional EvalPlan")
        path = self.project_root / metadata["scoring_policy_ref"]
        policy = FunctionalScoringPolicy.model_validate_json(path.read_text(encoding="utf-8"))
        if canonical_hash(policy) != metadata["scoring_policy_hash"]:
            raise ValueError("functional scoring policy has drifted")
        oracle_path = self.project_root / policy.oracle_ref.split(":", 1)[0]
        if sha256_bytes(oracle_path.read_bytes()) != metadata.get("oracle_sha256"):
            raise ValueError("functional oracle has drifted from the run snapshot")
        return policy

    def _is_functional_run(self) -> bool:
        metadata = self.run_dir / "run-metadata.json"
        if not metadata.is_file():
            return False
        value = json.loads(metadata.read_text(encoding="utf-8"))
        return value.get("mode") in {"frozen-functional", "frozen-candidate"}

    def _index_delegated_artifacts(self, artifact_root: Path, submission: WorkSubmission) -> None:
        if artifact_root.is_relative_to(self.run_dir):
            prefix = artifact_root.relative_to(self.run_dir)
            for reference in submission.artifacts:
                path = artifact_root / reference.path
                relative = (prefix / reference.path).as_posix()
                self.store.write_bytes(relative, path.read_bytes(), reference.media_type)

    def snapshot_ledger(self) -> None:
        self.store.write_json("ledger-snapshot.json", self.ledger.status())
        if self.lifecycle is not None and (self.run_dir / "run-metadata.json").is_file():
            self.ledger.checkpoint()
            config_hash = self.lifecycle_config_hash or sha256_bytes(
                (self.run_dir / "run-metadata.json").read_bytes()
            )
            self.lifecycle_config_hash = config_hash
            self.lifecycle.checkpoint(
                config_hash=config_hash,
                status=self._lifecycle_status(),
                critical_files=self._lifecycle_critical_files(),
            )

    def _lifecycle_critical_files(self) -> tuple[str, ...]:
        files = ["run-metadata.json", "ledger.sqlite3", "ledger-snapshot.json"]
        for relative in (
            "resolved-reference-config.json",
            "runtime-budget-binding.json",
            "runtime-session.json",
        ):
            if (self.run_dir / relative).is_file():
                files.append(relative)
        return tuple(files)

    def _lifecycle_status(self) -> RunLifecycleStatus:
        runtime = self._budget_runtime()
        if runtime is None:
            return RunLifecycleStatus.ACTIVE
        status = runtime.state().status
        if status in {
            RuntimeSessionStatus.PAUSED,
            RuntimeSessionStatus.AWAITING_CONTINUATION,
            RuntimeSessionStatus.STOPPED,
        }:
            return RunLifecycleStatus.PAUSED
        if status is RuntimeSessionStatus.ABORTED:
            return RunLifecycleStatus.ABORTED
        if status is RuntimeSessionStatus.COMPLETE:
            return RunLifecycleStatus.COMPLETE
        return RunLifecycleStatus.ACTIVE

    def pause_at_barrier(
        self,
        *,
        barrier: RuntimeBarrier,
        evidence_hash: str | None = None,
        candidate_gate_summary: dict[str, str] | None = None,
        next_role: str | None = None,
        next_work_count: int = 0,
        continuation_risk_zh: str,
    ) -> dict[str, Any]:
        runtime = self._budget_runtime()
        if runtime is None:
            raise ValueError("evaluation run has no active-session budget")
        statuses = self.ledger.work_statuses()
        complete_states = {WorkStatus.COMPLETED, WorkStatus.FAILED}
        completed = tuple(
            work_id for work_id, status in statuses.items() if status in complete_states
        )
        not_exported = tuple(
            work_id for work_id, status in statuses.items() if status is WorkStatus.PENDING
        )
        estimate = (
            runtime.estimate_batch(next_role, next_work_count)
            if next_role is not None and next_work_count > 0
            else None
        )
        if evidence_hash is None:
            evidence_hash = sha256_bytes(
                canonical_json_bytes(
                    {
                        "ledger": self.ledger.status(),
                        "run_metadata_sha256": sha256_bytes(
                            (self.run_dir / "run-metadata.json").read_bytes()
                        ),
                    }
                )
            )
        checkpoint = runtime.pause(
            barrier=barrier,
            evidence_hash=evidence_hash,
            completed_work_ids=completed,
            not_exported_work_ids=not_exported,
            candidate_gate_summary=candidate_gate_summary or {},
            next_batch_estimate=estimate,
            continuation_risk_zh=continuation_risk_zh,
        )
        self.snapshot_ledger()
        return checkpoint.model_dump(mode="json")

    def apply_continuation_decision(self, decision: BudgetContinuationDecision) -> dict[str, Any]:
        runtime = self._budget_runtime()
        if runtime is None:
            raise ValueError("evaluation run has no active-session budget")
        state = runtime.apply_decision(decision)
        self.snapshot_ledger()
        return state.model_dump(mode="json")

    def record_host_attempt_accounting(self, accounting: HostAttemptAccounting) -> dict[str, Any]:
        """Record a real host context that never became a WorkSubmission.

        The owner-run runtime is the only mutable ledger.  The typed record is
        indexed in its artifact inventory after the runtime accepts the charge.
        """

        runtime = self._budget_runtime()
        if runtime is None:
            raise ValueError("host attempt accounting requires an active-session runtime binding")
        state = runtime.record_host_attempt(accounting)
        owner_store = ArtifactStore(runtime.run_dir)
        owner_store.index_existing(
            f"host-attempt-accounting/{accounting.accounting_id}.json",
            "application/json",
        )
        self.snapshot_ledger()
        return {
            "accounting_id": accounting.accounting_id,
            "owner_run_ref": runtime.run_dir.relative_to(self.project_root).as_posix(),
            "runtime_status": state.status.value,
            "used": state.used.model_dump(mode="json"),
            "cumulative_agent_duration_ms": state.cumulative_agent_duration_ms,
        }

    def resume(self) -> dict[str, Any]:
        runtime = self._budget_runtime()
        if runtime is not None and runtime.state().status is not RuntimeSessionStatus.ACTIVE:
            raise ValueError(
                "evaluation resume requires an active hash-bound continuation decision"
            )
        resumed = self.ledger.resume_interrupted()
        self.snapshot_ledger()
        return {"resumed": resumed, "status": self.ledger.status()}

    def status(self) -> dict[str, Any]:
        return {"status": self.ledger.status(), "run_dir": self.run_dir.name}

    def aggregate(self) -> dict[str, Any]:
        records = self.ledger.records()
        by_tier: dict[str, int] = {}
        for record in records:
            by_tier[record.evidence_tier.value] = by_tier.get(record.evidence_tier.value, 0) + 1
        return {"records": len(records), "by_tier": by_tier, **aggregate_pairs(records)}

    def replay_assertions(self) -> dict[str, Any]:
        records = self.ledger.records()
        sources = [
            record for record in records if record.evidence_tier is EvidenceTier.E2_DELEGATED
        ]
        existing = {
            record.source_record_refs[0]: record
            for record in records
            if record.evidence_tier is EvidenceTier.E3_EXECUTABLE and record.source_record_refs
        }
        mismatches = 0
        repaired = 0
        replaced = 0
        manifest_path = self.run_dir / "replay-superseded-e3.json"
        superseded: list[dict[str, str]] = []
        if manifest_path.is_file():
            raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw_items = raw_manifest.get("items", []) if isinstance(raw_manifest, dict) else []
            superseded.extend(
                {
                    "source_record_id": str(item["source_record_id"]),
                    "previous_e3_record_id": str(item["previous_e3_record_id"]),
                    "current_e3_record_id": str(item["current_e3_record_id"]),
                }
                for item in raw_items
                if isinstance(item, dict)
                and {
                    "source_record_id",
                    "previous_e3_record_id",
                    "current_e3_record_id",
                }.issubset(item)
            )
        for source in sources:
            case = self._case(source.task_id)
            bundle: DeterministicGradingBundle | None = None
            if isinstance(case, FunctionalEvalCase):
                replayed, bundle = self.functional_assertion_provider.evaluate(
                    self.project_root,
                    self.run_dir,
                    case,
                    source,
                    self._functional_scoring_policy(),
                )
            else:
                replayed = self.assertion_provider.evaluate(
                    self.project_root,
                    case,
                    source,
                )
            if source.record_id not in existing:
                self.ledger.store_derived_record(replayed)
                self.store.write_json(
                    f"records/{replayed.record_id}.json", replayed.model_dump(mode="json")
                )
                if bundle is not None:
                    self.store.write_json(
                        f"deterministic/{source.work_id}.json",
                        bundle.model_dump(mode="json"),
                    )
                repaired += 1
            elif (
                existing[source.record_id].record_id != replayed.record_id
                or existing[source.record_id].provider_snapshot != replayed.provider_snapshot
            ):
                previous = existing[source.record_id]
                superseded.append(
                    {
                        "source_record_id": source.record_id,
                        "previous_e3_record_id": previous.record_id,
                        "current_e3_record_id": replayed.record_id,
                    }
                )
                self.ledger.store_derived_record(replayed)
                self.store.write_json(
                    f"records/{replayed.record_id}.json", replayed.model_dump(mode="json")
                )
                if bundle is not None:
                    self.store.write_json(
                        f"deterministic/{source.work_id}.json",
                        bundle.model_dump(mode="json"),
                    )
                replaced += 1

        # A previous stable replay used to overwrite the superseded manifest
        # with an empty list. Recover any historical E3 file that is no longer
        # canonical in the ledger so every durable record remains classified.
        canonical_records = self.ledger.records()
        canonical_ids = {record.record_id for record in canonical_records}
        current_e3_by_source = {
            record.source_record_refs[0]: record
            for record in canonical_records
            if record.evidence_tier is EvidenceTier.E3_EXECUTABLE and record.source_record_refs
        }
        for path in sorted((self.run_dir / "records").glob("*.json")):
            historical = EvaluationRecord.model_validate_json(path.read_text(encoding="utf-8"))
            if (
                historical.record_id in canonical_ids
                or historical.evidence_tier is not EvidenceTier.E3_EXECUTABLE
                or not historical.source_record_refs
            ):
                continue
            source_id = historical.source_record_refs[0]
            current = current_e3_by_source.get(source_id)
            if current is not None:
                superseded.append(
                    {
                        "source_record_id": source_id,
                        "previous_e3_record_id": historical.record_id,
                        "current_e3_record_id": current.record_id,
                    }
                )

        deduplicated = {
            (
                item["source_record_id"],
                item["previous_e3_record_id"],
                item["current_e3_record_id"],
            ): item
            for item in superseded
        }
        superseded = [deduplicated[key] for key in sorted(deduplicated)]
        self.store.write_json(
            "replay-superseded-e3.json",
            {"schema_version": "1.0.0", "items": superseded},
        )
        self.snapshot_ledger()
        return {
            "sources": len(sources),
            "repaired_missing": repaired,
            "replaced_drifted": replaced,
            "mismatches": mismatches,
            "superseded_history": len(superseded),
            "valid": mismatches == 0,
        }

    def seal_run(self) -> dict[str, Any]:
        """Finalize a run and index every durable file, including Agent outputs.

        This is a terminal operation for the engine instance: checkpointing and closing
        SQLite first guarantees that the indexed database hash is stable.
        """
        runtime = self._budget_runtime()
        if runtime is not None and self._owns_runtime_budget():
            runtime_state = runtime.state()
            if runtime_state.status is not RuntimeSessionStatus.ACTIVE:
                raise ValueError("reference seal requires an approved active session")
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
            if latest is None or latest.barrier is not RuntimeBarrier.REFERENCE_SEALED:
                checkpoint = self.pause_at_barrier(
                    barrier=RuntimeBarrier.REFERENCE_SEALED,
                    continuation_risk_zh=(
                        "Grader/Comparator/Analyzer 证据已收口; 继续后将终态封存 reference run。"
                    ),
                )
                return {
                    "valid": True,
                    "sealed": False,
                    "status": "awaiting_reference_seal_approval",
                    "budget_checkpoint": checkpoint,
                }
        self.ledger.checkpoint()
        if runtime is not None:
            binding = RuntimeBudgetBinding.model_validate_json(
                (self.run_dir / "runtime-budget-binding.json").read_text(encoding="utf-8")
            )
            owner = (self.project_root / binding.owner_run_ref).resolve(strict=True)
            if owner == self.run_dir:
                runtime.finish(status=RuntimeSessionStatus.COMPLETE)
        if self.lifecycle is not None:
            self.lifecycle.checkpoint(
                config_hash=self.lifecycle_config_hash
                or sha256_bytes((self.run_dir / "run-metadata.json").read_bytes()),
                status=RunLifecycleStatus.COMPLETE,
                critical_files=self._lifecycle_critical_files(),
            )
        self.close()
        indexed = 0
        for path in sorted(item for item in self.run_dir.rglob("*") if item.is_file()):
            relative = path.relative_to(self.run_dir).as_posix()
            if relative == "artifact-index.json" or relative.endswith(
                (".sqlite3-wal", ".sqlite3-shm")
            ):
                continue
            media_type = (
                "application/vnd.sqlite3"
                if path.suffix == ".sqlite3"
                else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            )
            self.store.index_existing(relative, media_type)
            indexed += 1
        verification = self.store.verify().as_dict()
        verification["valid"] = bool(verification["valid"] and verification["unindexed_files"] == 0)
        return {"indexed_files": indexed, **verification}


def build_submission(
    project_root: Path,
    item: EvalWorkItem,
    *,
    host: str,
    model: str,
    host_task_id: str,
    context_id: str | None = None,
    duration_ms: int,
    artifact_root: Path | None,
    artifact_relative_paths: tuple[str, ...] | None = None,
    transcript_path: Path | None = None,
    package_access: tuple[PackageAccessEvent, ...] = (),
    planned_trace: tuple[TraceStep, ...],
    observed_trace: tuple[TraceStep, ...],
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    tool_calls: int | None = None,
    token_count_kind: Literal["reported", "estimated", "unavailable"] = "unavailable",
    repair_attempt: bool = False,
    failure_kind: ProviderFailureKind | None = None,
    failure_detail: str | None = None,
) -> WorkSubmission:
    references: list[ArtifactRef] = []
    artifact_root_ref: str | None = None
    transcript_ref: ArtifactRef | None = None
    if artifact_root is not None:
        resolved = artifact_root.resolve()
        if not resolved.is_relative_to(project_root.resolve()):
            raise ValueError("artifact root must be inside the project")
        artifact_root_ref = resolved.relative_to(project_root.resolve()).as_posix()
        if artifact_relative_paths is None:
            selected_paths = sorted(item for item in resolved.rglob("*") if item.is_file())
        else:
            if not artifact_relative_paths or len(artifact_relative_paths) != len(
                set(artifact_relative_paths)
            ):
                raise ValueError("explicit evidence paths must be non-empty and unique")
            selected_paths = []
            for relative in sorted(artifact_relative_paths):
                relative_path = Path(relative)
                if relative_path.is_absolute() or ".." in relative_path.parts:
                    raise ValueError("explicit evidence path must be artifact-root-relative")
                path = (resolved / relative_path).resolve(strict=True)
                if not path.is_relative_to(resolved) or not path.is_file():
                    raise ValueError(f"explicit evidence file is missing: {relative}")
                selected_paths.append(path)
        for path in selected_paths:
            data = path.read_bytes()
            references.append(
                ArtifactRef(
                    path=path.relative_to(resolved).as_posix(),
                    sha256=sha256_bytes(data),
                    media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    size_bytes=len(data),
                )
            )
        if transcript_path is not None:
            transcript = transcript_path.resolve(strict=True)
            if not transcript.is_relative_to(resolved):
                raise ValueError("transcript must be inside artifact_root")
            relative_transcript = transcript.relative_to(resolved).as_posix()
            transcript_ref = next(
                (reference for reference in references if reference.path == relative_transcript),
                None,
            )
            if transcript_ref is None:
                raise ValueError("transcript was not indexed as an artifact")
    elif transcript_path is not None or artifact_relative_paths is not None:
        raise ValueError("transcript/explicit evidence paths require artifact_root")
    finished = datetime.now(UTC)
    started = finished - timedelta(milliseconds=duration_ms)
    failure_value = failure_kind.value if failure_kind is not None else None
    payload: dict[str, object] = {
        "work_id": item.work_id,
        "host": host,
        "model": model,
        "host_task_id": host_task_id,
        "context_id": context_id or host_task_id,
        "artifacts": [reference.model_dump() for reference in references],
        "transcript": transcript_ref.model_dump() if transcript_ref else None,
        "package_access": [event.model_dump(mode="json") for event in package_access],
        "planned_trace": [step.model_dump() for step in planned_trace],
        "observed_trace": [step.model_dump() for step in observed_trace],
        "repair_attempt": repair_attempt,
        "failure_kind": failure_value,
    }
    if token_count_kind == "unavailable":
        resolved_input_tokens = 0
        resolved_output_tokens = 0
    else:
        resolved_input_tokens = (
            input_tokens if input_tokens is not None else max(1, len(item.prompt) // 4)
        )
        # Artifact bytes are task evidence, not model text.  Estimate only the
        # small typed control payload when the host explicitly selects estimated
        # telemetry; never convert task-native output size into tokens.
        resolved_output_tokens = (
            output_tokens if output_tokens is not None else max(1, len(str(payload)) // 4)
        )
    return WorkSubmission(
        submission_id=submission_id_for(payload),
        work_id=item.work_id,
        provider_id=item.provider_id,
        host=host,
        model=model,
        host_task_id=host_task_id,
        context_id=context_id or host_task_id,
        artifact_root=artifact_root_ref,
        artifacts=tuple(references),
        transcript=transcript_ref,
        package_access=package_access,
        planned_trace=planned_trace,
        observed_trace=observed_trace,
        usage=UsageRecord(
            input_tokens=resolved_input_tokens,
            output_tokens=resolved_output_tokens,
            tool_calls=(tool_calls if tool_calls is not None else len(observed_trace)),
            duration_ms=duration_ms,
            token_count_kind=token_count_kind,
        ),
        uncertainty=0.1 if failure_kind is None else 1.0,
        repair_attempt=repair_attempt,
        failure_kind=failure_kind,
        failure_detail=failure_detail,
        started_at=started,
        finished_at=finished,
    )


def audit_fidelity(paths: Iterable[Path]) -> dict[str, Any]:
    observed_violations = 0
    provenance_missing = 0
    tier_upgrades = 0
    records = 0
    invalid_records = 0
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict) or "evidence_tier" not in raw:
            continue
        records += 1
        tier = raw.get("evidence_tier")
        if tier in {"E0", "E1"} and raw.get("observed_trace"):
            observed_violations += 1
        if tier in {"E2", "E3"}:
            provenance = raw.get("provenance", {})
            required = ("host", "model", "host_task_id", "submission_id")
            if not isinstance(provenance, dict) or any(not provenance.get(key) for key in required):
                provenance_missing += 1
        if tier == "E3" and not raw.get("source_record_refs"):
            tier_upgrades += 1
        try:
            EvaluationRecord.model_validate(raw)
        except ValueError:
            invalid_records += 1
    return {
        "valid": not any((observed_violations, provenance_missing, tier_upgrades, invalid_records)),
        "records": records,
        "observed_field_violations": observed_violations,
        "provenance_missing": provenance_missing,
        "tier_upgrade_without_evidence": tier_upgrades,
        "invalid_records": invalid_records,
    }
