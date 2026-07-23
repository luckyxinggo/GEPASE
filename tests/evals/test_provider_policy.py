from pathlib import Path

import pytest

from gepase.evals.cache import cache_key_for
from gepase.evals.engine import MultiFidelityEvalEngine
from gepase.evals.errors import UnsupportedCapability
from gepase.evals.providers.base import ProviderRegistry
from gepase.evals.schema import EvidenceTier


def test_provider_registry_rejects_unknown_provider() -> None:
    with pytest.raises(UnsupportedCapability):
        ProviderRegistry().get("unknown")


def test_cache_key_changes_with_host_model_snapshot(tmp_path: Path) -> None:
    with MultiFidelityEvalEngine(Path.cwd(), tmp_path / "first") as first:
        first.plan_cases(
            Path("benchmarks/manifest-draft.json"),
            splits=("validation",),
            tiers=(EvidenceTier.E1_SIMULATED,),
            variants=("original",),
            host="host-a",
            model="model-a",
            case_ids={"policy-evidence-06-00"},
        )
        first_item = first.ledger.export_ready()[0]
    with MultiFidelityEvalEngine(Path.cwd(), tmp_path / "second") as second:
        second.plan_cases(
            Path("benchmarks/manifest-draft.json"),
            splits=("validation",),
            tiers=(EvidenceTier.E1_SIMULATED,),
            variants=("original",),
            host="host-a",
            model="model-b",
            case_ids={"policy-evidence-06-00"},
        )
        second_item = second.ledger.export_ready()[0]
    assert cache_key_for(first_item) != cache_key_for(second_item)
