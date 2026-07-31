from __future__ import annotations

import json
from pathlib import Path

from gepase.evals.reference_runtime import load_reference_execution_config
from gepase.optimizer.runtime import load_r4_config
from gepase.optimizer.session_runtime import RuntimeBarrier
from gepase.reporting.outcome import EvolutionOutcomeReportConfig

ROOT = Path(__file__).resolve().parents[2]


def test_reference_config_fits_frozen_historical_tranche() -> None:
    config = load_reference_execution_config(
        ROOT
        / "configs/graph-hardening/slack-gif-creator-gh-e1-reference.yaml"
    )
    policy = config.active_session_budget_policy
    batches = {
        "executor": 16,
        "independent_grader": 16,
        "comparator": 6,
        "analyzer": 8,
    }
    estimates = [policy.role_estimates[role] for role in batches]
    calls = sum(batches.values())
    tokens = sum(
        batches[role] * policy.role_estimates[role].max_estimated_tokens_per_work
        for role in batches
    )
    assert calls == 46 <= policy.initial_tranche.agent_calls
    assert tokens == 848_000 <= policy.initial_tranche.estimated_tokens
    assert policy.initial_tranche.active_wall_clock_ms == 10_800_000
    assert policy.max_concurrency == 3
    assert estimates


def test_evolution_config_binds_lifecycle_budget_merge_and_scope() -> None:
    _hash, config = load_r4_config(
        ROOT,
        ROOT
        / "configs/graph-hardening/slack-gif-creator-gh-e1-evolution.json",
    )
    assert config.lifecycle_policy is not None
    assert config.active_session_budget_policy is not None
    assert config.conditional_merge_policy is not None
    assert config.runtime_budget.max_agent_calls == 80
    assert config.runtime_budget.max_estimated_tokens == 2_000_000
    assert config.runtime_budget.max_wall_clock_seconds == 10_800
    assert config.selector_target_limit == 2
    assert config.patch_budget.max_operations == 2
    assert config.patch_budget.max_changed_files == 2
    assert RuntimeBarrier.BEFORE_FINAL_REPORT in (
        config.active_session_budget_policy.required_barriers
    )


def test_report_config_has_reference_fallback_without_creating_runs() -> None:
    path = (
        ROOT
        / "configs/graph-hardening/slack-gif-creator-gh-e1-report.json"
    )
    config = EvolutionOutcomeReportConfig.model_validate_json(
        path.read_text(encoding="utf-8")
    )
    assert len(config.input_refs) == 2
    # This validates the pre-registered fallback shape, not the mutable lifecycle
    # state of a real GH-E1 run.  Absence is enforced by the E0.5 gate before the
    # phase starts; a test suite must remain runnable after the phase has begun.
    assert all(not Path(reference).is_absolute() for reference in config.input_refs)


def test_versioned_future_defaults_do_not_reinterpret_sealed_configs() -> None:
    legacy_hash, legacy = load_r4_config(
        ROOT,
        ROOT / "configs/canaries/slack-gif-creator-r4.json",
    )
    assert legacy_hash == "3a224bcb9f3887b6af9974915b51407be7d757535b71b18913af70ebcc757572"
    assert legacy.schema_version == "1.0.0"
    assert legacy.efficiency_policy_mode == "v1_legacy"
    assert legacy.relative_efficiency_policy is None

    example_path = ROOT / "configs/examples/r4-evolution-v2.json"
    _future_hash, future = load_r4_config(ROOT, example_path)
    assert future.schema_version == "2.0.0"
    assert future.efficiency_policy_mode == "relative_v2"
    assert future.relative_efficiency_policy is not None
    assert future.relative_efficiency_policy.policy_id == "relative_efficiency_v2"
    assert future.relative_efficiency_policy.max_relative_cost_ratio == 2.0
    assert "slack-gif-creator" not in example_path.read_text(encoding="utf-8")

    explicit_legacy = json.loads(example_path.read_text(encoding="utf-8"))
    explicit_legacy["efficiency_policy_mode"] = "v1_legacy"
    explicit = type(future).model_validate(explicit_legacy)
    assert explicit.efficiency_policy_mode == "v1_legacy"
    assert explicit.relative_efficiency_policy is None


def test_versioned_report_defaults_are_future_narrative_and_legacy_classic() -> None:
    future = EvolutionOutcomeReportConfig.model_validate_json(
        (ROOT / "configs/examples/evolution-report-v2.json").read_text(encoding="utf-8")
    )
    assert future.schema_version == "2.0.0"
    assert future.presentation_mode == "narrative_v1"
    assert "slack-gif-creator" not in (
        ROOT / "configs/examples/evolution-report-v2.json"
    ).read_text(encoding="utf-8")

    explicit_classic = future.model_copy(update={"presentation_mode": "classic"})
    assert explicit_classic.presentation_mode == "classic"

    historical_path = ROOT / "configs/graph-hardening/slack-gif-creator-gh-e1-report.json"
    historical_bytes = historical_path.read_bytes()
    historical = EvolutionOutcomeReportConfig.model_validate_json(historical_bytes)
    assert historical.schema_version == "1.0.0"
    assert historical.presentation_mode == "classic"
    assert historical_path.read_bytes() == historical_bytes
