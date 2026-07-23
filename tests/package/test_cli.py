from pathlib import Path

from typer.testing import CliRunner

from gepase.cli.app import app


def test_root_version_is_available_without_a_subcommand() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "0.1.0"


def test_root_without_arguments_still_shows_help() -> None:
    result = CliRunner().invoke(app, [])
    assert result.exit_code == 0, result.output
    assert "GEPASE Skill Package evolution core" in result.output


def test_analyze_and_graph_validate_cli_vertical_slice(
    tmp_path: Path,
    package_graph_evidence_run: Path,
) -> None:
    runner = CliRunner()
    output = tmp_path / "analysis"
    analyzed = runner.invoke(
        app,
        [
            "analyze",
            "benchmarks/skills/structured-report-builder",
            "--output-dir",
            str(output),
            "--evidence-run",
            str(package_graph_evidence_run),
            "--format",
            "json",
        ],
    )
    assert analyzed.exit_code == 0, analyzed.output
    graph = output / "structured-report-builder/graph.json"
    assert graph.is_file()
    validated = runner.invoke(app, ["graph", "validate", str(graph), "--format", "json"])
    assert validated.exit_code == 0, validated.output
