from __future__ import annotations

import json
from pathlib import Path

import pytest

from gepase.evals.evidence import EvaluationRecord
from gepase.store.artifacts import canonical_json_bytes


@pytest.fixture
def package_graph_evidence_run(tmp_path: Path) -> Path:
    """Create the smallest typed E1/E2 run needed by PackageGraph overlay tests."""

    records = tmp_path / "graph-evidence" / "records"
    records.mkdir(parents=True)
    fixture_root = Path("tests/fixtures/evidence")
    payloads = (
        (
            "e1.json",
            {
                "record_id": "graph-overlay-e1",
                "work_id": "graph-overlay-work-e1",
                "pair_id": "graph-overlay-pair-e1",
                "task_id": "graph-overlay-plan",
                "skill_id": "structured-report-builder",
                "planned_trace": [
                    {
                        "sequence": 0,
                        "action": "inspect skill instructions",
                        "target": "SKILL.md",
                        "tool": "read",
                        "outcome": None,
                    }
                ],
                "source_record_refs": [],
            },
        ),
        (
            "e2.json",
            {
                "record_id": "graph-overlay-e2",
                "work_id": "graph-overlay-work-e2",
                "pair_id": "graph-overlay-pair-e2",
                "task_id": "graph-overlay-execute",
                "skill_id": "structured-report-builder",
                "observed_trace": [
                    {
                        "sequence": 0,
                        "action": "execute bundled renderer",
                        "target": "scripts/render_report.py",
                        "tool": "python scripts/render_report.py",
                        "outcome": "completed",
                    }
                ],
                "source_record_refs": [],
            },
        ),
    )
    for source_name, updates in payloads:
        raw = json.loads((fixture_root / source_name).read_text(encoding="utf-8"))
        raw.update(updates)
        record = EvaluationRecord.model_validate(raw)
        (records / f"{record.record_id}.json").write_bytes(
            canonical_json_bytes(record.model_dump(mode="json"))
        )
    return records.parent
