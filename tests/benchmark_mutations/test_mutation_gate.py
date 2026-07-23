from pathlib import Path

from gepase.benchmarks.lifecycle import mutation_test


def test_all_assertion_families_kill_registered_mutants(tmp_path: Path) -> None:
    result = mutation_test(
        Path.cwd(), Path("benchmarks/manifest-draft.json"), tmp_path / "mutants"
    )
    assert result["valid"] is True
    assert result["mutants"] == 1300
    assert result["kill_rate"] >= 0.9
    assert result["control_failures"] == []
    assert all(
        metrics["kill_rate"] >= 0.8
        for metrics in result["family_metrics"].values()
    )
