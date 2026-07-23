from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from gepase.evals.engine import MultiFidelityEvalEngine, build_submission
from gepase.evals.evidence import TraceStep
from gepase.evals.work_items import EvalWorkItem

ROOT = Path(__file__).resolve().parents[2]
FROZEN_PLAN = ROOT / "artifacts/runs/r2-slack-gif-creator-evalplan/frozen-eval-plan.json"
SCORING_POLICY = ROOT / "configs/canaries/slack-gif-creator-r3-scoring.json"
PACKAGE_GRAPH = ROOT / "artifacts/runs/r2-slack-gif-creator-evalplan/package/graph.json"
SKILL_REF = "benchmarks/canaries/slack-gif-creator/package"


def _plan(engine: MultiFidelityEvalEngine) -> None:
    engine.plan_frozen_functional(
        FROZEN_PLAN,
        SCORING_POLICY,
        skill_ref=SKILL_REF,
        package_graph_ref=PACKAGE_GRAPH.relative_to(ROOT).as_posix(),
        splits=("train", "validation"),
        variants=("no-skill", "original"),
        host="codex",
        model="gpt-5.6-sol",
        seed=42,
        timeout_seconds=600,
    )


def test_frozen_plan_exports_sixteen_oracle_free_executor_views() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="r3-plan-test-", dir=local) as temporary:
        run_dir = Path(temporary)
        with MultiFidelityEvalEngine(ROOT, run_dir) as engine:
            _plan(engine)
            result = engine.export_work(run_dir / "executor-work.json")
            items = engine.ledger.work_items()
        assert result["exported"] == 16
        assert len(items) == 16
        pairs: dict[str, list[EvalWorkItem]] = {}
        for item in items:
            pairs.setdefault(item.pair_id, []).append(item)
        assert len(pairs) == 8
        assert all(len(values) == 2 for values in pairs.values())
        for values in pairs.values():
            left, right = values
            assert left.pairing == right.pairing
            assert {left.variant, right.variant} == {"no-skill", "original"}

        payload = json.loads((run_dir / "executor-work.json").read_text(encoding="utf-8"))
        forbidden = {
            "variant",
            "candidate_snapshot_hash",
            "pairing",
            "expectations",
            "rubric",
            "expected_output_zh",
            "oracle_ref",
        }
        for item in payload["work_items"]:
            assert forbidden.isdisjoint(item)
            assert item["role"] == "executor"


def test_real_gif_ingest_derives_content_level_e3() -> None:
    pillow = pytest.importorskip("PIL.Image")
    image_draw = pytest.importorskip("PIL.ImageDraw")
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="r3-e3-test-", dir=local) as temporary:
        run_dir = Path(temporary)
        with MultiFidelityEvalEngine(ROOT, run_dir) as engine:
            _plan(engine)
            item = next(
                value
                for value in engine.ledger.work_items()
                if value.task_id == "functional-train-emoji-bounce-001"
                and value.variant == "no-skill"
            )
            workspace = run_dir / f"workspaces/{item.work_id}"
            workspace.mkdir(parents=True)
            frames = []
            for index in range(10):
                frame = pillow.new("RGB", (128, 128), (18, 36, 76))
                draw = image_draw.Draw(frame)
                y = 20 + abs(4 - index) * 9
                draw.ellipse(
                    (44, y, 84, y + 40),
                    fill=(245, 196, 55),
                    outline=(20, 20, 25),
                    width=4,
                )
                draw.ellipse((54, y + 7, 59, y + 12), fill=(255, 245, 190))
                frames.append(frame)
            output = workspace / item.requested_output["filename"]
            frames[0].save(
                output,
                save_all=True,
                append_images=frames[1:],
                duration=90,
                loop=0,
                optimize=True,
            )
            transcript = workspace / "transcript.md"
            transcript.write_text("Created and reopened a real GIF fixture.\n", encoding="utf-8")
            submission = build_submission(
                ROOT,
                item,
                host="codex",
                model="gpt-5.6-sol",
                host_task_id="r3-e3-test-run",
                context_id="r3-e3-test-context",
                duration_ms=100,
                artifact_root=workspace,
                transcript_path=transcript,
                package_access=(),
                planned_trace=(),
                observed_trace=(
                    TraceStep(
                        sequence=0,
                        action="render_gif",
                        target=output.name,
                        tool="Pillow",
                        outcome="completed",
                    ),
                ),
                input_tokens=10,
                output_tokens=10,
                tool_calls=1,
                token_count_kind="estimated",
            )
            result = engine.ingest(submission)
            derived = engine.ledger.record_for_work(f"{item.work_id}-assertions")
        assert result["derived_record_id"]
        assert derived is not None
        assert derived.assertion_results
        assert all(value.evidence_refs for value in derived.assertion_results)
        assert all(value.measurements for value in derived.assertion_results)
        assert (run_dir / f"deterministic/{item.work_id}.json").is_file()
