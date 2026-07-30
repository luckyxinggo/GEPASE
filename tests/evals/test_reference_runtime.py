from __future__ import annotations

import json
import tempfile
from pathlib import Path

from typer.testing import CliRunner

from gepase.cli.app import app
from gepase.evals.reference_runtime import load_reference_execution_config

ROOT = Path(__file__).resolve().parents[2]
REFERENCE_CONFIG = (
    ROOT / "configs/graph-hardening/slack-gif-creator-gh-e1-reference.yaml"
)


def test_plan_reference_compiles_fresh_static_package_before_planning() -> None:
    """The configured graph belongs to the new run, so it cannot pre-exist."""

    template = load_reference_execution_config(REFERENCE_CONFIG)
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gh-e1-reference-plan-", dir=local) as temp:
        root = Path(temp)
        run_dir = root / "fresh-reference"
        payload = template.model_dump(mode="json")
        payload["run_id"] = run_dir.name
        payload["package_graph_ref"] = (
            run_dir / "package/graph.json"
        ).relative_to(ROOT).as_posix()
        config = root / "reference.json"
        config.write_text(json.dumps(payload), encoding="utf-8")

        result = CliRunner().invoke(
            app,
            [
                "eval",
                "plan-reference",
                "--config",
                str(config),
                "--run-dir",
                str(run_dir),
            ],
        )

        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["fresh_package"]["static_graph_only"] is True
        assert output["fresh_package"]["file_count"] == 7
        assert output["planned_work_items"] == 16
        assert output["budget_checkpoint"]["barrier"] == "package_compiled"
        assert (run_dir / "package/snapshot.json").is_file()
        assert (run_dir / "package/package-ir.json").is_file()
        assert (run_dir / "package/graph.json").is_file()
        assert (run_dir / "package/diagnostics.json").is_file()
