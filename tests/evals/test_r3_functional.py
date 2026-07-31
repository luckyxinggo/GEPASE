from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest

from gepase.evals.engine import MultiFidelityEvalEngine, build_submission
from gepase.evals.eval_plan import FrozenEvalPlan
from gepase.evals.evidence import EvaluationRecord, TraceStep, UsageRecord
from gepase.evals.functional import FunctionalScoringPolicy
from gepase.evals.functional_pipeline import FunctionalEvalCoordinator
from gepase.evals.work_items import EvalWorkItem
from gepase.schemas.common import ArtifactRef

ROOT = Path(__file__).resolve().parents[2]
FROZEN_PLAN = ROOT / "artifacts/runs/r2-slack-gif-creator-evalplan/frozen-eval-plan.json"
SCORING_POLICY = ROOT / "configs/canaries/slack-gif-creator-r3-scoring.json"
PACKAGE_GRAPH = ROOT / "artifacts/runs/r2-slack-gif-creator-evalplan/package/graph.json"
SKILL_REF = "benchmarks/canaries/slack-gif-creator/package"
TokenCountKind = Literal["reported", "estimated", "unavailable"]


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


def test_unavailable_tokens_are_excluded_without_double_counting_binary_size() -> None:
    frozen = FrozenEvalPlan.model_validate_json(FROZEN_PLAN.read_text(encoding="utf-8"))
    policy = FunctionalScoringPolicy.model_validate_json(
        SCORING_POLICY.read_text(encoding="utf-8")
    )
    case = frozen.functional_cases[0]
    item = cast(EvalWorkItem, SimpleNamespace(task_id=case.case_id))
    artifact = ArtifactRef(
        path=case.requested_output.filename,
        sha256="a" * 64,
        media_type=case.requested_output.media_type,
        size_bytes=3_500_000,
    )
    coordinator = object.__new__(FunctionalEvalCoordinator)
    coordinator.cases = {case.case_id: case}
    coordinator.policy = policy
    left = cast(
        EvaluationRecord,
        SimpleNamespace(
            artifacts=(artifact,),
            usage=UsageRecord(
                input_tokens=31,
                output_tokens=875_000,
                duration_ms=0,
                tool_calls=0,
                token_count_kind="unavailable",
            ),
        ),
    )
    right = cast(
        EvaluationRecord,
        SimpleNamespace(
            artifacts=(artifact,),
            usage=UsageRecord(
                input_tokens=0,
                output_tokens=0,
                duration_ms=0,
                tool_calls=0,
                token_count_kind="unavailable",
            ),
        ),
    )

    left_score = coordinator._efficiency(left, item, include_token_usage=False)
    right_score = coordinator._efficiency(right, item, include_token_usage=False)

    assert left_score == pytest.approx(2 / 3)
    assert right_score == left_score
    with pytest.raises(ValueError, match="unavailable token telemetry"):
        coordinator._efficiency(left, item, include_token_usage=True)


def _efficiency_fixture() -> tuple[
    FunctionalEvalCoordinator,
    EvalWorkItem,
    ArtifactRef,
]:
    frozen = FrozenEvalPlan.model_validate_json(FROZEN_PLAN.read_text(encoding="utf-8"))
    policy = FunctionalScoringPolicy.model_validate_json(
        SCORING_POLICY.read_text(encoding="utf-8")
    )
    case = frozen.functional_cases[0]
    coordinator = object.__new__(FunctionalEvalCoordinator)
    coordinator.cases = {case.case_id: case}
    coordinator.policy = policy
    return (
        coordinator,
        cast(EvalWorkItem, SimpleNamespace(task_id=case.case_id)),
        ArtifactRef(
            path=case.requested_output.filename,
            sha256="b" * 64,
            media_type=case.requested_output.media_type,
            size_bytes=policy.artifact_size_budget_bytes // 2,
        ),
    )


def _efficiency_source(
    artifact: ArtifactRef,
    kind: TokenCountKind,
    *,
    input_tokens: int = 100,
    output_tokens: int = 100,
) -> EvaluationRecord:
    return cast(
        EvaluationRecord,
        SimpleNamespace(
            artifacts=(artifact,),
            usage=UsageRecord(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=0,
                tool_calls=0,
                token_count_kind=kind,
            ),
        ),
    )


def test_reference_unavailable_candidate_estimated_uses_common_non_token_basis() -> None:
    coordinator, item, artifact = _efficiency_fixture()
    candidate = _efficiency_source(artifact, "estimated", input_tokens=800_000)
    original = _efficiency_source(artifact, "unavailable")
    no_skill = _efficiency_source(artifact, "unavailable")

    common = coordinator._common_token_count_kind(candidate, original, no_skill)

    assert common is None
    scores = [
        coordinator._efficiency(source, item, include_token_usage=common is not None)
        for source in (candidate, original, no_skill)
    ]
    assert scores == pytest.approx([5 / 6, 5 / 6, 5 / 6])


def test_reference_estimated_candidate_unavailable_uses_common_non_token_basis() -> None:
    coordinator, item, artifact = _efficiency_fixture()
    candidate = _efficiency_source(artifact, "unavailable")
    original = _efficiency_source(artifact, "estimated", input_tokens=800_000)
    no_skill = _efficiency_source(artifact, "estimated", input_tokens=400_000)

    common = coordinator._common_token_count_kind(candidate, original, no_skill)

    assert common is None
    scores = [
        coordinator._efficiency(source, item, include_token_usage=common is not None)
        for source in (candidate, original, no_skill)
    ]
    assert scores == pytest.approx([5 / 6, 5 / 6, 5 / 6])


@pytest.mark.parametrize("kind", ["estimated", "reported"])
def test_three_available_compatible_sources_include_token_axis(
    kind: TokenCountKind,
) -> None:
    coordinator, item, artifact = _efficiency_fixture()
    sources = tuple(
        _efficiency_source(artifact, kind, input_tokens=10, output_tokens=10)
        for _ in range(3)
    )

    common = coordinator._common_token_count_kind(*sources)

    assert common == kind
    with_tokens = coordinator._efficiency(sources[0], item, include_token_usage=True)
    without_tokens = coordinator._efficiency(sources[0], item, include_token_usage=False)
    assert with_tokens != without_tokens


def test_incompatible_available_measurement_kinds_exclude_token_axis() -> None:
    coordinator, item, artifact = _efficiency_fixture()
    candidate = _efficiency_source(artifact, "reported", input_tokens=800_000)
    original = _efficiency_source(artifact, "estimated", input_tokens=200_000)
    no_skill = _efficiency_source(artifact, "reported", input_tokens=400_000)

    common = coordinator._common_token_count_kind(candidate, original, no_skill)

    assert common is None
    scores = [
        coordinator._efficiency(source, item, include_token_usage=common is not None)
        for source in (candidate, original, no_skill)
    ]
    assert scores == pytest.approx([5 / 6, 5 / 6, 5 / 6])
