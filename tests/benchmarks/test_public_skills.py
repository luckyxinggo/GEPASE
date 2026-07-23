import subprocess
import sys
from pathlib import Path

import pytest

from gepase.benchmarks.loader import load_cases, load_manifest
from gepase.evals.assertions import AssertionContext, evaluate_assertion

MANIFEST = Path("benchmarks/manifest-draft.json")


@pytest.mark.parametrize(
    ("skill_id", "script", "output_argument", "output_name"),
    [
        (
            "structured-report-builder",
            "scripts/render_report.py",
            "--output",
            "report.html",
        ),
        (
            "tabular-context-builder",
            "scripts/build_context_pack.py",
            "--output-dir",
            ".",
        ),
        (
            "policy-evidence-evaluator",
            "scripts/evaluate_policy.py",
            "--output-dir",
            ".",
        ),
    ],
)
def test_public_skill_script_satisfies_case_assertions(
    tmp_path: Path,
    skill_id: str,
    script: str,
    output_argument: str,
    output_name: str,
) -> None:
    manifest = load_manifest(MANIFEST)
    case = next(case for case in load_cases(Path.cwd(), manifest) if case.skill_id == skill_id)
    command = [
        sys.executable,
        str(Path("benchmarks/skills") / skill_id / script),
        "--input",
        case.fixture_ref,
        output_argument,
        str(tmp_path / output_name),
    ]
    process = subprocess.run(command, check=False, capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    context = AssertionContext(tmp_path)
    assert all(evaluate_assertion(spec, context) for spec in case.assertions)
