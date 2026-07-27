from __future__ import annotations

import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def load_s10_gates():
    spec = spec_from_file_location("run_s10_gates", Path("scripts/run_s10_gates.py"))
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_structured_evidence_redacts_project_and_home_paths() -> None:
    gates = load_s10_gates()
    payload = {
        "deploy": {
            "output": str(gates.ROOT / "artifacts/local/release/deployed-package"),
        },
        "items": [str(Path.home() / "private/input.txt"), True],
    }

    assert gates.redacted_value(payload) == {
        "deploy": {
            "output": "<PROJECT_ROOT>/artifacts/local/release/deployed-package",
        },
        "items": ["<HOME>/private/input.txt", True],
    }


def test_learning_guide_matches_current_release_contract() -> None:
    gates = load_s10_gates()

    audit = gates.learning_guide_audit()

    assert audit["valid"] is True
    assert audit["missing_markers"] == []
    assert audit["present_prohibited_markers"] == []
    assert audit["duplicate_ids"] == []
    assert audit["missing_anchors"] == []
    assert audit["missing_local_references"] == []


def test_learning_course_is_complete_linked_and_claim_bounded() -> None:
    gates = load_s10_gates()

    audit = gates.learning_course_audit()

    assert audit["valid"] is True
    assert audit["html_pages"] == 14
    assert audit["total_html_bytes"] >= 200_000
    assert audit["quiz_options"] >= 25
    assert audit["interview_questions"] >= 40
    assert audit["algorithm_steps"] >= 25
    assert audit["missing_files"] == []
    assert audit["missing_markers"] == []
    assert audit["present_prohibited_claims"] == []
    assert audit["duplicate_ids"] == {}
    assert audit["missing_anchors"] == []
    assert audit["missing_local_references"] == []
