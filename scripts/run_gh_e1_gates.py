#!/usr/bin/env python3
"""Read-only verifier for the completed, sealed GH-E1 evidence chain."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from stage_gate_support import (
    load_json_object,
    verify_artifact_stores,
    verify_machine_gate_set,
    verify_valid_json_refs,
)

from gepase.store.artifacts import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "artifacts/stages/GH-E1"
REFERENCE = ROOT / "artifacts/runs/gh-e1-slack-gif-creator-reference"
EVOLUTION = ROOT / "artifacts/runs/gh-e1-slack-gif-creator-evolution"
REPORT = ROOT / "artifacts/runs/gh-e1-slack-gif-creator-report"

GATE_IDS = tuple(f"GHE1-G{index:02d}" for index in range(10))
REQUIRED_REFS = {
    "GHE1-G00": ("preflight.json",),
    "GHE1-G01": ("package-contract-audit.json",),
    "GHE1-G02": ("fresh-reference-audit.json",),
    "GHE1-G03": ("observed-selector-graph-audit.json",),
    "GHE1-G04": ("search-reachability-audit.json",),
    "GHE1-G05": ("candidate-train-gate-audit.json",),
    "GHE1-G06": ("held-out-merge-audit.json",),
    "GHE1-G07": ("effect-outcome-audit.json",),
    "GHE1-G08": ("report-reproduction-audit.json",),
    "GHE1-G09": ("verification.json", "protected-tree-audit.json"),
}


def formal_audit() -> dict[str, Any]:
    """Recompute the final Gate projection without opening any runtime role."""

    machine, machine_valid = verify_machine_gate_set(
        STAGE / "machine-gates.json",
        root=ROOT,
        expected_gate_ids=GATE_IDS,
    )
    gate_results, refs_valid = verify_valid_json_refs(
        STAGE,
        REQUIRED_REFS,
        root=ROOT,
    )
    seals, seals_valid = verify_artifact_stores(
        {
            "reference": REFERENCE,
            "evolution": EVOLUTION,
            "report_root": REPORT,
            "report_final": REPORT / "final",
            "stage": STAGE,
        }
    )
    outcome = load_json_object(STAGE / "effect-outcome-audit.json", root=ROOT)
    formal_gate_passed = machine_valid and refs_valid and seals_valid
    return {
        "schema_version": "1.0.0",
        "stage_id": "GH-E1",
        "mode": "read_only_formal_verification",
        "formal_gate_passed": formal_gate_passed,
        "machine_gate_valid": machine_valid,
        "machine_gate": {
            "status": machine.get("status"),
            "passed": machine.get("passed"),
            "total": machine.get("total"),
        },
        "gate_results": gate_results,
        "run_seals": seals,
        "effect_outcome": outcome.get("outcome"),
        "agent_calls": 0,
        "evidence_mutated": False,
    }


def main() -> None:
    result = formal_audit()
    print(canonical_json_bytes(result).decode(), end="")
    if not result["formal_gate_passed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
