from __future__ import annotations

# ruff: noqa: RUF001 -- Chinese test fixtures intentionally use Chinese punctuation.
import hashlib
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gepase.evals.eval_plan import (
    EvalDesignerSubmission,
    EvalDesignerWorkItem,
    EvalReviewDecision,
    EvalReviewSubmission,
    FixtureBinding,
    FunctionalEvalCase,
    FunctionalExpectation,
    RequestedOutput,
    ReviewDecisionKind,
    RoleRunProvenance,
    RubricCriterion,
    TriggerCaseKind,
    TriggerEvalCase,
)
from gepase.evals.evidence import UsageRecord
from gepase.evals.onboarding import EvalPlanOnboarding
from gepase.evals.work_items import PackageAccessEvent, PackageAccessKind

CANARY = Path("benchmarks/canaries/slack-gif-creator")
FAMILIES = (
    "emoji_animation",
    "message_gif",
    "input_image_animation",
    "text_readability",
    "easing_motion",
    "looping_timing",
    "quality_efficiency",
    "emoji_animation",
)
FIXTURES = (
    "emoji-bounce.json",
    "message-status.json",
    "input-badge.json",
    "message-readable.json",
    "easing-orbit.json",
    "loop-sparkles.json",
    "efficiency-burst.json",
    "emoji-pulse-text.json",
)


def _fixture(name: str) -> FixtureBinding:
    path = CANARY / "fixtures" / name
    return FixtureBinding(
        ref=path.as_posix(),
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        media_type="image/x-portable-pixmap" if name.endswith(".ppm") else "application/json",
        license="Apache-2.0 (GEPASE-generated fixture)",
        purpose_zh=f"{name} 的隔离测试输入",
    )


def _trigger_cases() -> tuple[TriggerEvalCase, ...]:
    rows: list[TriggerEvalCase] = []
    for kind in TriggerCaseKind:
        for index in range(6):
            expected = kind is TriggerCaseKind.POSITIVE or (
                kind is TriggerCaseKind.NEAR_BOUNDARY and index % 2 == 0
            )
            rows.append(
                TriggerEvalCase(
                    case_id=f"trigger-{kind.value}-{index}",
                    query=f"{kind.value} query {index} with a distinct user intent",
                    kind=kind,
                    expected_trigger=expected,
                    rationale_zh="覆盖明确请求、明确排除或能力近边界。",
                    split="train" if index < 4 else "validation",
                    risk="low",
                )
            )
    return tuple(rows)


def _functional_cases() -> tuple[FunctionalEvalCase, ...]:
    rows: list[FunctionalEvalCase] = []
    prompts = (
        "制作一枚星形落地后反弹一次的 128 像素 Slack emoji GIF。",
        "制作一个三阶段部署状态卡片的 480 像素 Slack message GIF。",
        "把给定徽章图片做成上浮、轻微过冲再稳定的 Slack emoji 动画。",
        "制作文字 SYNC 和 10:30 在整个循环中清晰可读的提醒 GIF。",
        "制作卫星沿弧线飞行并以 ease-out 平稳停靠的 emoji GIF。",
        "制作三颗闪光围绕圆环依次出现且首尾无跳变的循环 GIF。",
        "制作粒子庆祝动画，在 900KB 内兼顾流畅度与清晰轮廓。",
        "制作带 GO 文字的双脉冲提醒徽章，保证小尺寸下仍可读。",
    )
    for index, (family, fixture_name, prompt) in enumerate(
        zip(FAMILIES, FIXTURES, prompts, strict=True)
    ):
        fixtures = [_fixture(fixture_name)]
        if family == "input_image_animation":
            fixtures.append(_fixture("input-badge.ppm"))
        rows.append(
            FunctionalEvalCase(
                case_id=f"functional-{index:02d}",
                case_family=family,
                prompt=prompt,
                fixtures=tuple(fixtures),
                requested_output=RequestedOutput(
                    filename=f"functional-{index:02d}.gif",
                    media_type="image/gif",
                    description_zh="可由 Pillow/imageio 打开的任务原生 GIF 文件",
                ),
                expected_output_zh="动画语义符合 prompt，技术参数与 fixture 约束一致。",
                expectations=(
                    FunctionalExpectation(
                        expectation_id=f"metadata-{index}",
                        category="technical",
                        statement_zh="GIF 尺寸、帧数、时长和循环元数据满足 fixture。",
                        evidence_kind="artifact_metadata",
                        deterministic=True,
                        weight=0.55,
                    ),
                    FunctionalExpectation(
                        expectation_id=f"visual-{index}",
                        category="content",
                        statement_zh="关键视觉元素和动作阶段能从真实帧中识别。",
                        evidence_kind="visual_inspection",
                        deterministic=False,
                        weight=0.45,
                    ),
                ),
                rubric=(
                    RubricCriterion(
                        criterion_id="adherence",
                        label_zh="任务符合度",
                        description_zh="内容和动作忠实响应请求。",
                        weight=0.5,
                    ),
                    RubricCriterion(
                        criterion_id="polish",
                        label_zh="视觉完成度",
                        description_zh="构图、颜色与动画节奏专业可用。",
                        weight=0.5,
                    ),
                ),
                required_capabilities=("write_file", "python", "image_processing"),
                difficulty="medium" if index < 5 else "hard",
                risk="low" if index < 5 else "medium",
                leakage_group=f"family-{index}",
                split="train" if index < 5 else "validation",
            )
        )
    return tuple(rows)


def test_eval_plan_onboarding_review_freeze_and_resume() -> None:
    Path("artifacts/local").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="r2-onboarding-", dir="artifacts/local") as temp:
        onboarding = EvalPlanOnboarding(Path.cwd(), Path(temp))
        started = onboarding.start(
            package=CANARY / "package",
            provenance_path=CANARY / "source-provenance.json",
            design_brief_path=CANARY / "design-brief.json",
        )
        assert started["state"] == "package_parsed"
        work = EvalDesignerWorkItem.model_validate_json(
            (Path(temp) / "designer-work-item.json").read_text(encoding="utf-8")
        )
        now = datetime.now(UTC)
        submission = EvalDesignerSubmission(
            submission_id="designer-test-submission",
            work_id=work.work_id,
            skill_id=work.skill_id,
            package_snapshot_hash=work.package_snapshot_hash,
            role_run=RoleRunProvenance(
                host="test-agent",
                model="test-model",
                context_id="isolated-test-context",
                host_task_id="test-task",
                usage=UsageRecord(
                    output_tokens=800,
                    tool_calls=8,
                    duration_ms=2_000,
                    token_count_kind="estimated",
                ),
                started_at=now,
                finished_at=now + timedelta(seconds=2),
            ),
            package_access=tuple(
                PackageAccessEvent(
                    sequence=index,
                    kind=PackageAccessKind.READ,
                    path=path,
                    bytes_loaded=(CANARY / "package" / path).stat().st_size,
                    tokens_loaded=max(1, (CANARY / "package" / path).stat().st_size // 4),
                )
                for index, path in enumerate(work.required_package_reads)
            ),
            trigger_cases=_trigger_cases(),
            functional_cases=_functional_cases(),
            design_notes_zh=("测试用隔离 Designer submission。",),
        )
        ingested = onboarding.ingest_design(submission)
        assert ingested["state"] == "awaiting_review"
        review_html = (Path(temp) / "review.html").read_text(encoding="utf-8")
        for token in (
            "批量确认低风险 Train",
            "request-regeneration",
            "Package Graph",
            "导出 review.json",
            'id="risk-filter"',
        ):
            assert token in review_html
        assert "JSON.stringify(payload,null,2)+'\\n'" in review_html
        assert "https://" not in review_html

        all_cases = (*submission.trigger_cases, *submission.functional_cases)
        decisions = []
        for index, case in enumerate(all_cases):
            decision = ReviewDecisionKind.EDIT if index == 0 else ReviewDecisionKind.APPROVE
            decisions.append(
                EvalReviewDecision(
                    case_id=case.case_id,
                    case_type=case.case_type,
                    decision=decision,
                    edited_case=case.model_dump(mode="json") if index == 0 else None,
                    comment_zh="保留原意并验证 edit 回路" if index == 0 else "逐项确认",
                )
            )
        review = EvalReviewSubmission(
            review_id="review-test-complete",
            plan_id="evalplan-slack-gif-creator-r1",
            draft_hash=str(ingested["draft_hash"]),
            reviewer_id="test-maintainer",
            reviewer_kind="maintainer",
            decisions=tuple(decisions),
        )
        frozen = onboarding.import_review(review)
        assert frozen["valid"] is True
        assert frozen["unresolved_review_decisions"] == 0
        resumed = onboarding.resume()
        assert resumed["resumed"] is True
        assert onboarding.status()["state"] == "execution_ready"
        rerendered = onboarding.render_review()
        assert rerendered["state"] == "execution_ready"
        final_review_html = (Path(temp) / "review.html").read_text(encoding="utf-8")
        assert "GEPASE / R2 / execution_ready" in final_review_html
        assert 'value="test-maintainer"' in final_review_html
        assert 'const seededDecisions=[{"case_id":"trigger-positive-0' in final_review_html
        verification = onboarding.status()["artifact_verification"]
        assert isinstance(verification, dict)
        assert verification["valid"] is True


def test_executor_view_does_not_expose_functional_oracles() -> None:
    view = _functional_cases()[0].executor_view()
    assert set(view).isdisjoint(
        {"expectations", "rubric", "expected_output_zh", "candidate_identity", "sibling_output"}
    )
    assert view["evidence_tier"] == "E2"


def test_role_run_provenance_marks_a_bounded_repair_explicitly() -> None:
    now = datetime.now(UTC)
    run = RoleRunProvenance(
        host="test-agent",
        model="test-model",
        context_id="repair-context",
        host_task_id="repair-task",
        usage=UsageRecord(
            output_tokens=1,
            duration_ms=1,
            token_count_kind="estimated",
        ),
        repair_attempt=True,
        started_at=now,
        finished_at=now,
    )
    assert run.repair_attempt is True
