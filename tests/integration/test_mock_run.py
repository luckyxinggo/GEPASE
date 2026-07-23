import json
from pathlib import Path

from gepase.services.mock_run import run_mock


def test_mock_vertical_slice_is_reproducible(tmp_path: Path) -> None:
    first = run_mock(Path("configs/examples/mock.yaml"), tmp_path / "first", Path.cwd())
    second = run_mock(Path("configs/examples/mock.yaml"), tmp_path / "second", Path.cwd())
    assert first == second
    assert first["summary"] == {"total": 2, "passed": 2}
    manifest = json.loads((tmp_path / "first/run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["provider_kind"] == "mock"
    assert len(manifest["config_hash"]) == 64

