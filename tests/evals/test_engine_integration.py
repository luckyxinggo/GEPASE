import json
import subprocess
import sys
import tempfile
from pathlib import Path

from gepase.evals.engine import MultiFidelityEvalEngine, build_submission
from gepase.evals.evidence import TraceStep
from gepase.evals.schema import EvidenceTier


def test_e2_ingest_produces_replayable_e3_assertions() -> None:
    root = Path.cwd()
    local = root / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="engine-e2-", dir=local) as temporary:
        run_dir = Path(temporary) / "run"
        with MultiFidelityEvalEngine(root, run_dir) as engine:
            plan = engine.plan_cases(
                Path("benchmarks/manifest-draft.json"),
                splits=("validation",),
                tiers=(EvidenceTier.E2_DELEGATED,),
                variants=("original",),
                host="test-host",
                model="test-model",
                case_ids={"structured-report-06-00"},
            )
            assert plan["planned_work_items"] == 1
            item = engine.ledger.export_ready()[0]
            workspace = run_dir / "workspaces" / item.work_id
            workspace.mkdir(parents=True)
            process = subprocess.run(
                [
                    sys.executable,
                    "benchmarks/skills/structured-report-builder/scripts/render_report.py",
                    "--input",
                    item.fixture_ref,
                    "--output",
                    str(workspace / "report.html"),
                ],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )
            assert process.returncode == 0, process.stderr
            submission = build_submission(
                root,
                item,
                host="test-host",
                model="test-model",
                host_task_id="integration-worker",
                duration_ms=250,
                artifact_root=workspace,
                planned_trace=(TraceStep(sequence=0, action="use_skill_renderer"),),
                observed_trace=(
                    TraceStep(
                        sequence=0,
                        action="render_report",
                        target=item.fixture_ref,
                        tool="python",
                        outcome="completed",
                    ),
                ),
            )
            result = engine.ingest(submission)
            assert result["derived_record_id"] is not None
            records = engine.ledger.records()
            assert {record.evidence_tier for record in records} == {
                EvidenceTier.E2_DELEGATED,
                EvidenceTier.E3_EXECUTABLE,
            }
            e3 = next(
                record
                for record in records
                if record.evidence_tier is EvidenceTier.E3_EXECUTABLE
            )
            assert e3.score == 1.0
            with engine.ledger.connection:
                engine.ledger.connection.execute(
                    "DELETE FROM records WHERE work_id = ?", (e3.work_id,)
                )
            replay = engine.replay_assertions()
            assert replay["valid"] is True
            assert replay["repaired_missing"] == 1
            canonical = engine.ledger.record_for_work(e3.work_id)
            assert canonical is not None

            historical = canonical.model_copy(
                update={"record_id": "record-000000000000000000000000"}
            )
            engine.store.write_json(
                f"records/{historical.record_id}.json",
                historical.model_dump(mode="json"),
            )
            classified = engine.replay_assertions()
            assert classified["superseded_history"] == 1
            manifest = run_dir / "replay-superseded-e3.json"
            first_manifest = manifest.read_bytes()
            stable = engine.replay_assertions()
            assert stable["superseded_history"] == 1
            assert manifest.read_bytes() == first_manifest
            assert engine.store.verify().valid is True


def test_repair_attempt_is_distinct_in_execution_submission_identity() -> None:
    root = Path.cwd()
    local = root / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="engine-repair-", dir=local) as temporary:
        run_dir = Path(temporary) / "run"
        with MultiFidelityEvalEngine(root, run_dir) as engine:
            engine.plan_cases(
                Path("benchmarks/manifest-draft.json"),
                splits=("validation",),
                tiers=(EvidenceTier.E1_SIMULATED,),
                variants=("no-skill",),
                host="test-host",
                model="test-model",
                case_ids={"policy-evidence-06-00"},
            )
            item = engine.ledger.export_ready()[0]
            first = build_submission(
                root,
                item,
                host="test-host",
                model="test-model",
                host_task_id="first-context",
                duration_ms=250,
                artifact_root=None,
                planned_trace=(TraceStep(sequence=0, action="plan"),),
                observed_trace=(),
            )
            repaired = build_submission(
                root,
                item,
                host="test-host",
                model="test-model",
                host_task_id="repair-context",
                duration_ms=250,
                artifact_root=None,
                planned_trace=(TraceStep(sequence=0, action="plan"),),
                observed_trace=(),
                repair_attempt=True,
            )
            assert first.repair_attempt is False
            assert repaired.repair_attempt is True
            assert first.submission_id != repaired.submission_id


def test_task_native_binary_bytes_never_become_submission_tokens() -> None:
    root = Path.cwd()
    local = root / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="binary-telemetry-", dir=local) as temporary:
        run_dir = Path(temporary) / "run"
        with MultiFidelityEvalEngine(root, run_dir) as engine:
            engine.plan_cases(
                Path("benchmarks/manifest-draft.json"),
                splits=("validation",),
                tiers=(EvidenceTier.E1_SIMULATED,),
                variants=("no-skill",),
                host="test-host",
                model="test-model",
                case_ids={"policy-evidence-06-00"},
            )
            item = engine.ledger.export_ready()[0]
            workspace = run_dir / "workspaces" / item.work_id
            workspace.mkdir(parents=True)
            output = workspace / "large.gif"
            output.write_bytes(b"GIF89a" + b"\x00" * 3_500_000)

            unavailable = build_submission(
                root,
                item,
                host="test-host",
                model="test-model",
                host_task_id="binary-unavailable",
                duration_ms=100,
                artifact_root=workspace,
                artifact_relative_paths=("large.gif",),
                planned_trace=(),
                observed_trace=(),
                token_count_kind="unavailable",
            )
            estimated = build_submission(
                root,
                item,
                host="test-host",
                model="test-model",
                host_task_id="binary-estimated",
                duration_ms=100,
                artifact_root=workspace,
                artifact_relative_paths=("large.gif",),
                planned_trace=(),
                observed_trace=(),
                token_count_kind="estimated",
            )

    assert unavailable.usage.token_count_kind == "unavailable"
    assert unavailable.usage.input_tokens == 0
    assert unavailable.usage.output_tokens == 0
    assert unavailable.artifacts[0].size_bytes == 3_500_006
    assert estimated.usage.output_tokens < 1_000



def test_exported_work_hides_assertions_and_expected_labels() -> None:
    root = Path.cwd()
    local = root / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="engine-blind-", dir=local) as temporary:
        run_dir = Path(temporary) / "run"
        export = run_dir / "exports/work.json"
        with MultiFidelityEvalEngine(root, run_dir) as engine:
            engine.plan_cases(
                Path("benchmarks/manifest-draft.json"),
                splits=("validation",),
                tiers=(EvidenceTier.E1_SIMULATED,),
                variants=("no-skill", "original"),
                host="test-host",
                model="test-model",
                case_ids={"policy-evidence-06-00"},
            )
            engine.export_work(export)
        text = export.read_text(encoding="utf-8")
        payload = json.loads(text)
        assert payload["count"] == 2
        assert "assertions" not in text
        serialized_keys = {
            key
            for item in payload["work_items"]
            for key in item
        }
        assert "expected" not in serialized_keys
        no_skill = next(item for item in payload["work_items"] if item["variant"] == "no-skill")
        original = next(item for item in payload["work_items"] if item["variant"] == "original")
        assert no_skill["skill_ref"] is None
        assert original["skill_ref"].endswith("policy-evidence-evaluator")


def test_seal_run_indexes_ledger_and_agent_written_files(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    engine = MultiFidelityEvalEngine(Path.cwd(), run_dir)
    external = run_dir / "traces/agent-native.json"
    external.parent.mkdir(parents=True)
    external.write_text('{"host": "codex"}\n', encoding="utf-8")

    result = engine.seal_run()

    assert result["valid"] is True
    assert result["unindexed_files"] == 0
    index = json.loads((run_dir / "artifact-index.json").read_text(encoding="utf-8"))
    paths = {item["path"] for item in index["artifacts"]}
    assert {"ledger.sqlite3", "traces/agent-native.json"} <= paths


def test_run_metadata_uses_portable_manifest_reference(tmp_path: Path) -> None:
    run_dir = tmp_path / "portable-metadata"
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        engine.plan_cases(
            Path.cwd() / "benchmarks/manifest-draft.json",
            splits=("train",),
            tiers=(EvidenceTier.E1_SIMULATED,),
            variants=("original",),
            host="test-host",
            model="test-model",
            case_ids={"structured-report-00-00"},
        )
    metadata = json.loads((run_dir / "run-metadata.json").read_text(encoding="utf-8"))
    assert metadata["manifest"] == "benchmarks/manifest-draft.json"
