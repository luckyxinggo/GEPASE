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
from gepase.evals.schema import EvidenceTier, TaskCase
from gepase.evals.work_items import (
    EvalWorkItem,
    PackageAccessEvent,
    PairingSnapshot,
    Variant,
    WorkSubmission,
    canonical_hash,
    executor_view,
    submission_id_for,
    work_id_for,
)
from gepase.package.ir import NodeKind, PackageGraph
from gepase.package.loader import load_package
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
    def __init__(self, project_root: Path, run_dir: Path) -> None:
        self.project_root = project_root.resolve()
        self.run_dir = run_dir.resolve()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.store = ArtifactStore(self.run_dir)
        self.ledger = EvalLedger(self.run_dir / "ledger.sqlite3")
        self.registry = ProviderRegistry()
        self.static_provider = StaticProvider()
        self.registry.register(self.static_provider)
        self.registry.register(SimulationProvider())
        self.registry.register(DelegatedProvider())
        self.artifact_provider = ArtifactProvider()
        self.assertion_provider = AssertionProvider()
        self.functional_assertion_provider = FunctionalAssertionProvider()
        self._closed = False

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
        return FunctionalEvalCoordinator(
            self.project_root, self.run_dir, self.ledger, self.store
        )

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
        policy_ref, policy = self._project_model_ref(
            scoring_policy_path, FunctionalScoringPolicy
        )
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
        node_map = {
            node.path: node.node_id for node in graph.nodes if node.kind is NodeKind.FILE
        }
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
                        key: str(value)
                        for key, value in case.requested_output.model_dump().items()
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
        policy_ref, policy = self._project_model_ref(
            scoring_policy_path, FunctionalScoringPolicy
        )
        key_ref, key = self._project_model_ref(reference_key_path, ReferenceEvidenceKey)
        cache_audit = audit_reference_cache(self.project_root, key)
        if not cache_audit.hit:
            self.store.write_json(
                "reference-cache-audit.json", cache_audit.model_dump(mode="json")
            )
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
        node_map = {
            node.path: node.node_id for node in graph.nodes if node.kind is NodeKind.FILE
        }
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
        self.store.write_json(
            "reference-cache-audit.json", cache_audit.model_dump(mode="json")
        )
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
        host_model = "none" if tier is EvidenceTier.E0_STATIC else canonical_hash(
            {"host": host, "model": model}
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
        items = self.ledger.export_ready(limit)
        exported_items = (
            [executor_view(item) for item in items]
            if self._is_functional_run()
            else list(items)
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
        return {"exported": len(items), "output": output.as_posix()}

    def ingest(self, submission: WorkSubmission, *, auto_assert: bool = True) -> dict[str, Any]:
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
        if (
            auto_assert
            and not failed
            and item.evidence_tier is EvidenceTier.E2_DELEGATED
        ):
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
        self.store.write_json(
            f"records/{stored.record_id}.json", stored.model_dump(mode="json")
        )
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
        self.snapshot_ledger()
        return {
            "record_id": stored.record_id,
            "duplicate": duplicate,
            "derived_record_id": derived.record_id if derived else None,
            "status": "failed" if failed else "completed",
        }

    def _failure_record(
        self, item: EvalWorkItem, submission: WorkSubmission
    ) -> EvaluationRecord:
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

    def _index_delegated_artifacts(
        self, artifact_root: Path, submission: WorkSubmission
    ) -> None:
        if artifact_root.is_relative_to(self.run_dir):
            prefix = artifact_root.relative_to(self.run_dir)
            for reference in submission.artifacts:
                path = artifact_root / reference.path
                relative = (prefix / reference.path).as_posix()
                self.store.write_bytes(relative, path.read_bytes(), reference.media_type)

    def snapshot_ledger(self) -> None:
        self.store.write_json("ledger-snapshot.json", self.ledger.status())

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
            record
            for record in records
            if record.evidence_tier is EvidenceTier.E2_DELEGATED
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
            if record.evidence_tier is EvidenceTier.E3_EXECUTABLE
            and record.source_record_refs
        }
        for path in sorted((self.run_dir / "records").glob("*.json")):
            historical = EvaluationRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )
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
        self.ledger.checkpoint()
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
        verification["valid"] = bool(
            verification["valid"] and verification["unindexed_files"] == 0
        )
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
    transcript_path: Path | None = None,
    package_access: tuple[PackageAccessEvent, ...] = (),
    planned_trace: tuple[TraceStep, ...],
    observed_trace: tuple[TraceStep, ...],
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    tool_calls: int | None = None,
    token_count_kind: Literal["reported", "estimated", "unavailable"] = "estimated",
    failure_kind: ProviderFailureKind | None = None,
    failure_detail: str | None = None,
) -> WorkSubmission:
    references: list[ArtifactRef] = []
    total_bytes = 0
    artifact_root_ref: str | None = None
    transcript_ref: ArtifactRef | None = None
    if artifact_root is not None:
        resolved = artifact_root.resolve()
        if not resolved.is_relative_to(project_root.resolve()):
            raise ValueError("artifact root must be inside the project")
        artifact_root_ref = resolved.relative_to(project_root.resolve()).as_posix()
        for path in sorted(item for item in resolved.rglob("*") if item.is_file()):
            data = path.read_bytes()
            total_bytes += len(data)
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
    elif transcript_path is not None:
        raise ValueError("transcript requires artifact_root")
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
        "failure_kind": failure_value,
    }
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
            input_tokens=(
                input_tokens if input_tokens is not None else max(1, len(item.prompt) // 4)
            ),
            output_tokens=(
                output_tokens
                if output_tokens is not None
                else max(1, total_bytes // 4 + len(str(payload)) // 4)
            ),
            tool_calls=(tool_calls if tool_calls is not None else len(observed_trace)),
            duration_ms=duration_ms,
            token_count_kind=token_count_kind,
        ),
        uncertainty=0.1 if failure_kind is None else 1.0,
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
        "valid": not any(
            (observed_violations, provenance_missing, tier_upgrades, invalid_records)
        ),
        "records": records,
        "observed_field_violations": observed_violations,
        "provenance_missing": provenance_missing,
        "tier_upgrade_without_evidence": tier_upgrades,
        "invalid_records": invalid_records,
    }
