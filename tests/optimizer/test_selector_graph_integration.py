from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest

import gepase.optimizer.evolution_controller as controller_module
from gepase.optimizer.candidate import PackageCandidate
from gepase.optimizer.evolution_controller import R4EvolutionController
from gepase.optimizer.runtime import ReferenceEvidenceKey, load_r4_config
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.dynamic_graph import overlay_package_access
from gepase.store.artifacts import atomic_write, canonical_json_bytes

ROOT = Path(__file__).resolve().parents[2]
STATIC_CONFIG = ROOT / "configs/canaries/slack-gif-creator-r4.json"
OBSERVED_CONFIG = ROOT / "configs/graph-hardening/slack-gif-creator-gh-e0.json"
R4 = ROOT / "artifacts/runs/r4-slack-gif-creator-evolution"


def _local_controller(
    config: Path,
) -> tuple[tempfile.TemporaryDirectory[str], R4EvolutionController]:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix="gh-e0-", dir=local)
    _hash, resolved = load_r4_config(ROOT, config)
    run_dir = Path(temporary.name) / resolved.run_id
    return temporary, R4EvolutionController(ROOT, run_dir, config)


def _dynamic_value(target: dict[str, object]) -> float:
    selection = target["selection"]
    assert isinstance(selection, dict)
    contributions = selection["contributions"]
    assert isinstance(contributions, list)
    return max(
        float(item["raw_value"])
        for item in contributions
        if item["feature"] == "dynamic_access"
    )


def test_old_r4_config_defaults_to_static_selector_behavior() -> None:
    temporary, controller = _local_controller(STATIC_CONFIG)
    try:
        assert controller.config_hash == (
            "3a224bcb9f3887b6af9974915b51407be7d757535b71b18913af70ebcc757572"
        )
        controller.initialize()
        works = sorted((controller.run_dir / "proposal-work-items").glob("*.json"))
        payloads = [json.loads(path.read_text(encoding="utf-8")) for path in works]
        assert controller.config.selector_graph_policy.mode == "static"
        assert all(item["selector_graph"] is None for item in payloads)
        assert all(item["selector_ranking"] is None for item in payloads)
        assert not (controller.run_dir / "selector-graphs").exists()
    finally:
        temporary.cleanup()


def test_initialize_consumes_persisted_parent_observed_graph() -> None:
    temporary, controller = _local_controller(OBSERVED_CONFIG)
    try:
        result = controller.initialize()
        assert result["proposal_work_items"] == 2
        works = sorted((controller.run_dir / "proposal-work-items").glob("*.json"))
        payloads = [json.loads(path.read_text(encoding="utf-8")) for path in works]
        assert len(payloads) == 2
        graph_refs = {item["selector_graph"]["selector_graph_ref"] for item in payloads}
        assert len(graph_refs) == 1
        for item in payloads:
            binding = item["selector_graph"]
            ranking = item["selector_ranking"]
            assert binding["mode"] == "static_observed"
            assert binding["evidence_variant"] == "original"
            assert binding["layer_counts"]["observed"] > 0
            assert binding["layer_counts"]["planned"] == 0
            assert binding["semantic_hypothesis_edges"] == 0
            assert binding["mapped_access_events"] > 0
            assert binding["rejected_access_events"] == 0
            assert (ROOT / binding["selector_graph_ref"]).is_file()
            assert ranking["total_ranked"] > len(ranking["top_k"]) >= len(item["targets"])
            assert ranking["executable_alternative"]["path"].endswith(".py")
            assert 1 <= len(item["targets"]) <= 2
            assert all(_dynamic_value(target) > 0 for target in item["targets"])
            assert len({target["path"] for target in item["targets"]}) <= 2
            assert len(item["allowed_operations"]) <= 2
        state = controller.state()
        assert state.budget_usage.agent_calls == 0
        assert state.budget_usage.proposals == 0
        assert state.budget_usage.candidates == 0
    finally:
        temporary.cleanup()


def test_selector_graph_cache_is_parent_and_evidence_scope_bound() -> None:
    temporary, controller = _local_controller(OBSERVED_CONFIG)
    try:
        controller.initialize()
        seed = PackageCandidate.model_validate_json(
            (controller.run_dir / "seed-candidate.json").read_text(encoding="utf-8")
        )
        key = ReferenceEvidenceKey.model_validate_json(
            (controller.run_dir / "reference-evidence-key.json").read_text(
                encoding="utf-8"
            )
        )
        evidence_run = ROOT / controller.config.reference_run_ref
        tasks = controller._train_task_ids(evidence_run)
        first = controller.build_selector_graph_view(
            seed,
            package_ref=seed.source_package_ref,
            evidence_run_ref=controller.config.reference_run_ref,
            expected_graph_ref=controller.config.package_graph_ref,
            evidence_variant="original",
            allowed_task_ids=tasks,
            reference_key_hash=key.key_hash,
        )
        narrowed = controller.build_selector_graph_view(
            seed,
            package_ref=seed.source_package_ref,
            evidence_run_ref=controller.config.reference_run_ref,
            expected_graph_ref=controller.config.package_graph_ref,
            evidence_variant="original",
            allowed_task_ids=(tasks[0],),
            reference_key_hash=key.key_hash,
        )
        assert first.binding is not None and first.cache_hit
        assert first.cache_audit_ref is not None
        first_cache_audit = json.loads(
            (ROOT / first.cache_audit_ref).read_text(encoding="utf-8")
        )
        assert first_cache_audit["hit"] is True
        assert first_cache_audit["cache_key"] == first.binding.cache_key
        assert narrowed.binding is not None and not narrowed.cache_hit
        assert narrowed.cache_audit_ref is not None
        narrowed_cache_audit = json.loads(
            (ROOT / narrowed.cache_audit_ref).read_text(encoding="utf-8")
        )
        assert narrowed_cache_audit["hit"] is False
        assert first.binding.cache_key != narrowed.binding.cache_key
        assert first.binding.evidence_scope_hash != narrowed.binding.evidence_scope_hash
        atomic_write(ROOT / first.binding.selector_graph_ref, b"{}\n")
        with pytest.raises(ValueError, match="cached selector artifact hash mismatch"):
            controller.build_selector_graph_view(
                seed,
                package_ref=seed.source_package_ref,
                evidence_run_ref=controller.config.reference_run_ref,
                expected_graph_ref=controller.config.package_graph_ref,
                evidence_variant="original",
                allowed_task_ids=tasks,
                reference_key_hash=key.key_hash,
            )
    finally:
        temporary.cleanup()


def test_evaluated_candidate_binds_only_its_own_train_evidence() -> None:
    temporary, controller = _local_controller(OBSERVED_CONFIG)
    candidate_id = "candidate-2dad7a05ce4a6460dd71f470"
    try:
        candidate = PackageCandidate.model_validate_json(
            (R4 / f"candidates/{candidate_id}/candidate.json").read_text(encoding="utf-8")
        )
        application = json.loads(
            (R4 / f"candidates/{candidate_id}/application.json").read_text(
                encoding="utf-8"
            )
        )
        key = ReferenceEvidenceKey.model_validate_json(
            (R4 / "reference-evidence-key.json").read_text(encoding="utf-8")
        )
        evidence_ref = f"{R4.relative_to(ROOT).as_posix()}/evals/{candidate_id}/train"
        evidence_run = ROOT / evidence_ref
        view = controller.build_selector_graph_view(
            candidate,
            package_ref=str(application["workspace_ref"]),
            evidence_run_ref=evidence_ref,
            expected_graph_ref=(
                f"{R4.relative_to(ROOT).as_posix()}/candidates/{candidate_id}/graph.json"
            ),
            evidence_variant="candidate",
            allowed_task_ids=controller._train_task_ids(evidence_run),
            reference_key_hash=key.key_hash,
        )
        assert view.binding is not None
        assert view.binding.evidence_variant == "candidate"
        assert view.binding.accepted_work_ids
        assert view.binding.filtered_work_ids == ()
        assert view.graph.snapshot_hash == candidate.content_hash
        overlay_audit = json.loads(
            (ROOT / view.binding.overlay_audit_ref).read_text(encoding="utf-8")
        )
        assert overlay_audit["source_run"] == evidence_ref

        sibling = "candidate-edf5f1aa07926ba5415f0442"
        sibling_ref = f"{R4.relative_to(ROOT).as_posix()}/evals/{sibling}/train"
        with pytest.raises(ValueError, match="another parent"):
            controller.build_selector_graph_view(
                candidate,
                package_ref=str(application["workspace_ref"]),
                evidence_run_ref=sibling_ref,
                expected_graph_ref=(
                    f"{R4.relative_to(ROOT).as_posix()}/candidates/{sibling}/graph.json"
                ),
                evidence_variant="candidate",
                allowed_task_ids=controller._train_task_ids(ROOT / sibling_ref),
                reference_key_hash=key.key_hash,
            )

        validation_ref = f"{R4.relative_to(ROOT).as_posix()}/evals/{candidate_id}/validation"
        validation_metadata = json.loads(
            (ROOT / validation_ref / "run-metadata.json").read_text(encoding="utf-8")
        )
        with pytest.raises(ValueError, match="train split"):
            controller.build_selector_graph_view(
                candidate,
                package_ref=str(application["workspace_ref"]),
                evidence_run_ref=validation_ref,
                expected_graph_ref=(
                    f"{R4.relative_to(ROOT).as_posix()}/candidates/{candidate_id}/graph.json"
                ),
                evidence_variant="candidate",
                allowed_task_ids=tuple(validation_metadata["selected_case_ids"]),
                reference_key_hash=key.key_hash,
            )
    finally:
        temporary.cleanup()


def test_recovery_selector_path_reuses_the_parent_bound_observed_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary, controller = _local_controller(OBSERVED_CONFIG)
    try:
        shutil.copytree(R4, controller.run_dir, dirs_exist_ok=True)
        state_path = controller.run_dir / "evolution-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["run_id"] = controller.config.run_id
        state["config_hash"] = controller.config_hash
        state["budget_usage"]["candidates"] = 3
        atomic_write(state_path, canonical_json_bytes(state))
        for admission_path in (controller.run_dir / "train-admission").glob(
            "candidate-*.json"
        ):
            if admission_path.stem == "candidate-edf5f1aa07926ba5415f0442":
                continue
            admission = json.loads(admission_path.read_text(encoding="utf-8"))
            admission["passed"] = False
            atomic_write(admission_path, canonical_json_bytes(admission))

        cache_hits: list[bool] = []
        original_builder = controller.build_selector_graph_view

        def capture_cache(parent: PackageCandidate, **kwargs: Any) -> Any:
            view = original_builder(parent, **kwargs)
            cache_hits.append(view.cache_hit)
            return view

        monkeypatch.setattr(controller, "build_selector_graph_view", capture_cache)
        work = controller.prepare_recovery_proposal(
            "candidate-edf5f1aa07926ba5415f0442"
        )
        assert work.selector_graph is not None
        assert work.selector_ranking is not None
        assert work.selector_graph.layer_counts["observed"] > 0
        assert work.selector_graph.evidence_variant == "original"
        assert all(
            max(
                item.raw_value
                for item in target.selection.contributions
                if item.feature == "dynamic_access"
            )
            > 0
            for target in work.targets
        )
        cached = controller.prepare_recovery_proposal(
            "candidate-edf5f1aa07926ba5415f0442"
        )
        assert cached.selector_graph is not None
        assert cached.selector_graph.cache_key == work.selector_graph.cache_key
        assert cache_hits == [False, True]
        cache_audits = sorted(
            (controller.run_dir / "selector-graph-cache-audits").glob("*/*/*.json")
        )
        assert [json.loads(path.read_text(encoding="utf-8"))["hit"] for path in cache_audits] == [
            False,
            True,
        ]
    finally:
        temporary.cleanup()


def test_valid_typed_access_without_observed_edges_fails_before_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary, controller = _local_controller(OBSERVED_CONFIG)
    try:
        seed = PackageCandidate.model_validate_json(
            (R4 / "seed-candidate.json").read_text(encoding="utf-8")
        )
        key = ReferenceEvidenceKey.model_validate_json(
            (R4 / "reference-evidence-key.json").read_text(encoding="utf-8")
        )
        evidence_run = ROOT / controller.config.reference_run_ref
        tasks = controller._train_task_ids(evidence_run)
        static = PackageAnalyzer().analyze(ROOT / seed.source_package_ref).graph
        _graph, audit = overlay_package_access(
            static,
            evidence_run,
            allowed_task_ids=set(tasks),
            expected_graph_ref=controller.config.package_graph_ref,
        )
        empty_audit = audit.model_copy(update={"observed_edges": 0})
        monkeypatch.setattr(
            controller_module,
            "overlay_package_access",
            lambda *args, **kwargs: (static, empty_audit),
        )
        with pytest.raises(ValueError, match="no observed selector edges"):
            controller.build_selector_graph_view(
                seed,
                package_ref=seed.source_package_ref,
                evidence_run_ref=controller.config.reference_run_ref,
                expected_graph_ref=controller.config.package_graph_ref,
                evidence_variant="original",
                allowed_task_ids=tasks,
                reference_key_hash=key.key_hash,
            )
        assert not list((controller.run_dir / "proposal-work-items").glob("*.json"))
    finally:
        temporary.cleanup()
