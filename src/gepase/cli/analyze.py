"""Package analysis, graph validation, fault diagnosis, and localization CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from gepase.cli.app_support import emit
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.faults import evaluate_fault_corpus, evaluate_localization
from gepase.package.graph import validate_graphs

graph_app = typer.Typer(no_args_is_help=True, help="Validate and inspect PackageGraph artifacts.")


def analyze_packages(
    packages: Annotated[list[Path], typer.Argument(help="One or more Skill package roots.")],
    output_dir: Annotated[Path, typer.Option("--output-dir")] = Path("artifacts/analysis"),
    evidence_run: Annotated[Path | None, typer.Option("--evidence-run")] = None,
    output_format: Annotated[str, typer.Option("--format")] = "text",
) -> None:
    analyzer = PackageAnalyzer()
    rows: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    for package in packages:
        try:
            result = analyzer.analyze(package, evidence_run=evidence_run)
            rows.append(analyzer.write(result, output_dir / result.snapshot.package_id))
        except (OSError, ValueError) as error:
            failures.append(
                {
                    "package": package.as_posix(),
                    "error_type": type(error).__name__,
                    "message": str(error),
                }
            )
    valid = not failures and len(rows) == len(packages)
    payload = {
        "valid": valid,
        "packages": len(rows),
        "parse_crash": len(failures),
        "results": rows,
        "failures": failures,
    }
    emit(payload, output_format)
    if not valid:
        raise typer.Exit(2)


@graph_app.command("validate")
def validate_graph(
    path: Path,
    recursive: Annotated[bool, typer.Option("--recursive")] = False,
    output_format: Annotated[str, typer.Option("--format")] = "text",
) -> None:
    paths = (
        sorted(path.rglob("graph.json"))
        if recursive and path.is_dir()
        else [path]
    )
    result = validate_graphs(paths)
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


def analyze_faults(
    corpus: Annotated[Path, typer.Argument()] = Path(
        "benchmarks/fault_localization.jsonl"
    ),
    output_format: Annotated[str, typer.Option("--format")] = "text",
) -> None:
    result = evaluate_fault_corpus(Path.cwd(), corpus)
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


def localize_faults(
    corpus: Annotated[Path, typer.Option("--fault-corpus")] = Path(
        "benchmarks/fault_localization.jsonl"
    ),
    selector: Annotated[str, typer.Option("--selector")] = "reverse-slice",
    output_format: Annotated[str, typer.Option("--format")] = "text",
) -> None:
    if selector != "reverse-slice":
        raise typer.BadParameter("only reverse-slice is available in S3")
    result = evaluate_localization(Path.cwd(), corpus)
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)
