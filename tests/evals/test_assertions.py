import json
from pathlib import Path

import pytest

from gepase.evals.assertions import AssertionContext, evaluate_assertion
from gepase.evals.schema import AssertionSpec


def test_assertion_families_are_replayable(tmp_path: Path) -> None:
    (tmp_path / "payload.json").write_text(
        json.dumps({"summary": {"count": 3, "score": 0.75}}), encoding="utf-8"
    )
    (tmp_path / "report.html").write_text(
        "<html><body><h1>Result</h1><table></table></body></html>", encoding="utf-8"
    )
    context = AssertionContext(tmp_path)
    specs = [
        AssertionSpec(
            assertion_id="exists",
            family="file_exists",
            parameters={"path": "payload.json"},
        ),
        AssertionSpec(
            assertion_id="equals",
            family="json_equals",
            parameters={"path": "payload.json", "pointer": "/summary/count", "expected": 3},
        ),
        AssertionSpec(
            assertion_id="range",
            family="json_range",
            parameters={
                "path": "payload.json",
                "pointer": "/summary/score",
                "minimum": 0.7,
                "maximum": 0.8,
            },
        ),
        AssertionSpec(
            assertion_id="html",
            family="html_contract",
            parameters={
                "path": "report.html",
                "regex": ["<h1>", "<table"],
                "no_remote_assets": True,
            },
        ),
    ]
    assert all(evaluate_assertion(spec, context) for spec in specs)


def test_assertion_cannot_escape_artifact_root(tmp_path: Path) -> None:
    spec = AssertionSpec(
        assertion_id="escape",
        family="file_exists",
        parameters={"path": "../outside"},
    )
    with pytest.raises(ValueError, match="escapes"):
        evaluate_assertion(spec, AssertionContext(tmp_path))


@pytest.mark.parametrize(
    ("payload", "pointer"),
    [
        ("not-json", "/summary/count"),
        (json.dumps({"summary": {}}), "/summary/count"),
        (json.dumps({"summary": []}), "/summary/not-an-index"),
    ],
)
def test_json_assertion_treats_malformed_candidate_as_failure(
    tmp_path: Path, payload: str, pointer: str
) -> None:
    (tmp_path / "payload.json").write_text(payload, encoding="utf-8")
    spec = AssertionSpec(
        assertion_id="malformed-candidate",
        family="json_equals",
        parameters={"path": "payload.json", "pointer": pointer, "expected": 3},
    )
    assert evaluate_assertion(spec, AssertionContext(tmp_path)) is False
