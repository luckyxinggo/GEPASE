from pathlib import Path

import pytest
from pydantic import ValidationError

from gepase.config.loader import load_project_config
from gepase.config.models import EvalPolicyConfig, ProjectConfig
from gepase.config.redaction import redact


def test_mock_config_is_typed_and_stable() -> None:
    first = load_project_config(Path("configs/examples/mock.yaml"))
    second = load_project_config(Path("configs/examples/mock.yaml"))
    assert first.config_hash == second.config_hash
    assert first.config.provider.kind == "mock"
    assert "credential" not in str(first.redacted).lower()


def test_minimum_tier_must_be_allowed() -> None:
    with pytest.raises(ValidationError):
        EvalPolicyConfig(allowed_tiers=("E0",), minimum_acceptance_tier="E2")


def test_usage_token_counts_are_not_mistaken_for_credentials() -> None:
    value = redact(
        {
            "max_tokens": 50_000,
            "input_tokens": 123,
            "access_token": "sensitive",
            "credential_env": "PROVIDER_KEY",
        }
    )
    assert value["max_tokens"] == 50_000
    assert value["input_tokens"] == 123
    assert value["access_token"] == "***REDACTED***"
    assert value["credential_env"] == "***REDACTED***"


def test_role_scoped_headless_config_is_typed_but_does_not_resolve_secret() -> None:
    loaded = load_project_config(Path("configs/examples/headless-roles.yaml"))
    assert loaded.config.role_providers is not None
    executor = loaded.config.role_providers.roles["executor"]
    assert executor.kind == "headless"
    assert executor.model == "replace-with-provider-model"
    assert executor.credential_env == "GEPASE_PROVIDER_API_KEY"
    assert loaded.redacted["role_providers"]["roles"]["executor"]["credential_env"] == (
        "***REDACTED***"
    )


def test_headless_provider_requires_only_secret_reference_not_secret_value() -> None:
    with pytest.raises(ValidationError, match="credential_env"):
        ProjectConfig.model_validate(
            {
                "dataset": {"manifest": "benchmarks/manifest-v1.json"},
                "provider": {
                    "kind": "headless",
                    "name": "incomplete",
                    "model": "model",
                    "endpoint": "https://provider.example/v1",
                },
            }
        )
