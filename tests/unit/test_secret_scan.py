import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


def load_scanner():
    spec = spec_from_file_location("check_secrets", Path("scripts/check_secrets.py"))
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_canary_key_and_private_path_are_detected(tmp_path: Path) -> None:
    scanner = load_scanner()
    canary = tmp_path / "canary.txt"
    fake_key = "sk-" + "canary012345678901234567890"
    private_path = "/Users/" + "example/Desktop/private/file"
    canary.write_text(
        f"password='{fake_key}'\n/path={private_path}\n",
        encoding="utf-8",
    )
    findings = scanner.scan_files(tmp_path, [canary])
    assert {finding.kind for finding in findings} >= {"api_key", "assigned_secret", "private_path"}


def test_schema_enum_is_not_misclassified_as_an_assigned_secret(tmp_path: Path) -> None:
    scanner = load_scanner()
    schema = tmp_path / "schema.py"
    schema.write_text('REQUIRES_SECRET = "requires_secret"\n', encoding="utf-8")
    assert scanner.scan_files(tmp_path, [schema]) == []


def test_embedded_task_identifier_is_not_misclassified_as_api_key(tmp_path: Path) -> None:
    scanner = load_scanner()
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        '{"operation_id":"op-task-first-bounded-validation",'
        '"context_id":"task-r4-cmp-loop-ab-41cdacb862112d239730a0f6"}\n',
        encoding="utf-8",
    )
    assert scanner.scan_files(tmp_path, [evidence]) == []


def test_ignored_local_artifacts_are_not_release_evidence(tmp_path: Path) -> None:
    scanner = load_scanner()
    local = tmp_path / "artifacts/local/upstream/example.py"
    local.parent.mkdir(parents=True)
    assigned = "pass" + 'word="not-release-evidence-value"\n'
    local.write_text(assigned, encoding="utf-8")
    generated = scanner.generated_files(tmp_path)
    assert local not in generated
