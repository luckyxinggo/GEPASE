"""S7 validation-gate diagnostics, audit, and report CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from gepase.cli.app_support import emit
from gepase.optimizer.acceptance.diagnostics import (
    early_gate_fault_suite,
    regression_floor_diagnostic,
    rejected_memory_diagnostic,
    variance_policy_diagnostic,
)
from gepase.optimizer.acceptance.engine import GateDecisionStore
from gepase.optimizer.acceptance.models import GateDecision
from gepase.reporting.gates import gate_audit_payload, render_gate_report
from gepase.store.artifacts import atomic_write, canonical_json_bytes

gate_app = typer.Typer(no_args_is_help=True, help="Validate and audit candidate Gate 0-4 funnels.")


@gate_app.command("fault-suite")
def fault_suite(
    fixture_dir: Annotated[Path, typer.Argument()] = Path("tests/fixtures/gate_faults"),
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    if not fixture_dir.exists():
        raise typer.BadParameter(f"gate fault fixture directory is missing: {fixture_dir}")
    result = early_gate_fault_suite(Path.cwd())
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@gate_app.command("regression-floor-test")
def regression_floor_test(
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    result = regression_floor_diagnostic()
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@gate_app.command("variance-test")
def variance_test(
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    result = variance_policy_diagnostic()
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@gate_app.command("rejected-memory-test")
def rejected_memory_test(
    mutation_run: Annotated[Path, typer.Option("--mutation-run")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    result = rejected_memory_diagnostic(Path.cwd(), mutation_run)
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


def _load_decisions(path: Path) -> list[GateDecision]:
    if path.is_dir():
        return [
            GateDecision.model_validate_json(item.read_text(encoding="utf-8"))
            for item in sorted(path.glob("gate-decision-*.json"))
        ]
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("rows", raw) if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError("gate audit input must contain a list of decisions")
    return [GateDecision.model_validate(item) for item in rows]


@gate_app.command("audit")
def audit(
    path: Path,
    output: Annotated[Path | None, typer.Option("--output")] = None,
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    decisions = _load_decisions(path)
    payload = gate_audit_payload(decisions)
    accepted = [item for item in decisions if item.frontier_eligible]
    accepted_complete = all(
        all(
            next(
                (
                    gate.outcome.value == "passed"
                    for gate in item.gates
                    if gate.level.value == f"gate_{level}_{name}"
                ),
                False,
            )
            for level, name in enumerate(("schema", "static", "minibatch", "validation"))
        )
        for item in accepted
    )
    payload.update(
        {
            "valid": bool(decisions)
            and accepted_complete
            and all(item.test_access_count == 0 for item in decisions),
            "accepted_gates_complete": accepted_complete,
            "missing_decision": 0 if decisions else 1,
            "test_access": sum(item.test_access_count for item in decisions),
        }
    )
    if output is not None:
        atomic_write(output, canonical_json_bytes(payload))
    emit(payload, output_format)
    if not payload["valid"]:
        raise typer.Exit(2)


@gate_app.command("report")
def report(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output: Annotated[Path, typer.Option("--output")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    with GateDecisionStore(run_dir / "gate-decisions.sqlite3") as store:
        decisions = store.all()
    atomic_write(output, render_gate_report(decisions).encode())
    emit(
        {"valid": bool(decisions), "decisions": len(decisions), "output": output.as_posix()},
        output_format,
    )
