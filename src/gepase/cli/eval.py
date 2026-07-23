"""Multi-fidelity evaluation CLI vertical slice."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, cast

import typer

from gepase.cli.app_support import emit
from gepase.evals.diagnostics import (
    cache_resume_diagnostic,
    fault_injection_diagnostic,
    mock_pair_diagnostic,
)
from gepase.evals.engine import MultiFidelityEvalEngine, audit_fidelity, build_submission
from gepase.evals.eval_plan import EvalDesignerSubmission, EvalReviewSubmission
from gepase.evals.evidence import ProviderFailureKind, TraceStep
from gepase.evals.functional import (
    AnalyzerSubmission,
    ComparatorSubmission,
    IndependentGraderSubmission,
)
from gepase.evals.onboarding import EvalPlanOnboarding, has_onboarding_checkpoint
from gepase.evals.schema import EvidenceTier
from gepase.evals.work_items import ExecutionBundle, PackageAccessEvent
from gepase.store.artifacts import ArtifactStore, atomic_write, canonical_json_bytes

eval_app = typer.Typer(no_args_is_help=True, help="Plan and ingest multi-fidelity evidence.")


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _trace(path: Path | None, key: str) -> tuple[TraceStep, ...]:
    if path is None:
        return ()
    raw = json.loads(path.read_text(encoding="utf-8"))
    value = raw.get(key, []) if isinstance(raw, dict) else raw
    if not isinstance(value, list):
        raise typer.BadParameter(f"{key} must be a list")
    return tuple(TraceStep.model_validate(item) for item in value)


def _package_access(path: Path | None) -> tuple[PackageAccessEvent, ...]:
    if path is None:
        return ()
    raw = json.loads(path.read_text(encoding="utf-8"))
    value = raw.get("package_access", []) if isinstance(raw, dict) else raw
    if not isinstance(value, list):
        raise typer.BadParameter("package_access must be a list")
    return tuple(PackageAccessEvent.model_validate(item) for item in value)


def _token_count_kind(value: str) -> Literal["reported", "estimated", "unavailable"]:
    if value not in {"reported", "estimated", "unavailable"}:
        raise typer.BadParameter(
            "token_count_kind must be reported, estimated, or unavailable"
        )
    return cast(Literal["reported", "estimated", "unavailable"], value)


@eval_app.command("onboarding-start")
def onboarding_start(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    package: Annotated[Path, typer.Option("--package")],
    provenance: Annotated[Path, typer.Option("--provenance")],
    design_brief: Annotated[Path, typer.Option("--design-brief")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Parse a pinned Package and export one typed Eval Designer work item."""
    result = EvalPlanOnboarding(Path.cwd(), run_dir).start(
        package=package,
        provenance_path=provenance,
        design_brief_path=design_brief,
    )
    emit(result, output_format)


@eval_app.command("submit-design")
def submit_design(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    submission: Annotated[Path, typer.Option("--submission")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Ingest an isolated Eval Designer response and enter awaiting_review."""
    value = EvalDesignerSubmission.model_validate_json(submission.read_text(encoding="utf-8"))
    result = EvalPlanOnboarding(Path.cwd(), run_dir).ingest_design(value)
    submission_path = submission.resolve()
    run_root = run_dir.resolve()
    if submission_path.is_relative_to(run_root):
        relative = submission_path.relative_to(run_root).as_posix()
        ArtifactStore(run_root).index_existing(relative, "application/json")
        result["raw_submission_ref"] = relative
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@eval_app.command("review")
def review(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output: Annotated[Path | None, typer.Option("--output")] = None,
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Render the offline Chinese interactive EvalPlan review application."""
    result = EvalPlanOnboarding(Path.cwd(), run_dir).render_review(output)
    emit(result, output_format)


@eval_app.command("import-review")
def import_review(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    review_path: Annotated[Path, typer.Option("--review")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Validate review decisions and freeze an immutable EvalPlan revision."""
    value = EvalReviewSubmission.model_validate_json(review_path.read_text(encoding="utf-8"))
    result = EvalPlanOnboarding(Path.cwd(), run_dir).import_review(value)
    source_path = review_path.resolve()
    run_root = run_dir.resolve()
    if source_path.is_relative_to(run_root):
        relative = source_path.relative_to(run_root).as_posix()
        ArtifactStore(run_root).index_existing(relative, "application/json")
        result["raw_review_ref"] = relative
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@eval_app.command("onboarding-status")
def onboarding_status(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    emit(EvalPlanOnboarding(Path.cwd(), run_dir).status(), output_format)


@eval_app.command("plan")
def plan(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    manifest: Annotated[Path, typer.Option("--manifest")] = Path("benchmarks/manifest-draft.json"),
    splits: Annotated[str, typer.Option("--splits")] = "validation",
    tiers: Annotated[str, typer.Option("--tiers")] = "E2",
    variants: Annotated[str, typer.Option("--variants")] = "no-skill,original",
    case_ids: Annotated[str | None, typer.Option("--case-ids")] = None,
    host: Annotated[str, typer.Option("--host")] = "codex",
    model: Annotated[str, typer.Option("--model")] = "agent-model",
    seed: Annotated[int, typer.Option("--seed")] = 42,
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    tier_values = tuple(EvidenceTier(value) for value in _csv(tiers))
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.plan_cases(
            manifest,
            splits=_csv(splits),
            tiers=tier_values,
            variants=_csv(variants),
            host=host,
            model=model,
            case_ids=set(_csv(case_ids)) if case_ids else None,
            seed=seed,
        )
    emit(result, output_format)


@eval_app.command("plan-frozen")
def plan_frozen(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    frozen_plan: Annotated[Path, typer.Option("--frozen-plan")],
    scoring_policy: Annotated[Path, typer.Option("--scoring-policy")],
    skill_ref: Annotated[str, typer.Option("--skill-ref")],
    package_graph_ref: Annotated[str, typer.Option("--package-graph-ref")],
    splits: Annotated[str, typer.Option("--splits")] = "train,validation",
    variants: Annotated[str, typer.Option("--variants")] = "no-skill,original",
    host: Annotated[str, typer.Option("--host")] = "codex",
    model: Annotated[str, typer.Option("--model")] = "agent-model",
    seed: Annotated[int, typer.Option("--seed")] = 42,
    timeout_seconds: Annotated[int, typer.Option("--timeout-seconds")] = 600,
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Plan paired E2 work from an immutable reviewed EvalPlan."""
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.plan_frozen_functional(
            frozen_plan,
            scoring_policy,
            skill_ref=skill_ref,
            package_graph_ref=package_graph_ref,
            splits=_csv(splits),
            variants=_csv(variants),
            host=host,
            model=model,
            seed=seed,
            timeout_seconds=timeout_seconds,
        )
    emit(result, output_format)


@eval_app.command("plan-candidate")
def plan_candidate(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    frozen_plan: Annotated[Path, typer.Option("--frozen-plan")],
    scoring_policy: Annotated[Path, typer.Option("--scoring-policy")],
    reference_key: Annotated[Path, typer.Option("--reference-key")],
    candidate_id: Annotated[str, typer.Option("--candidate-id")],
    candidate_content_hash: Annotated[str, typer.Option("--candidate-content-hash")],
    candidate_ref: Annotated[str, typer.Option("--candidate-ref")],
    package_graph_ref: Annotated[str, typer.Option("--package-graph-ref")],
    split: Annotated[Literal["train", "validation"], typer.Option("--split")],
    host: Annotated[str, typer.Option("--host")] = "codex",
    model: Annotated[str, typer.Option("--model")] = "agent-model",
    seed: Annotated[int, typer.Option("--seed")] = 42,
    timeout_seconds: Annotated[int, typer.Option("--timeout-seconds")] = 600,
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Plan fresh candidate E2 work against a fully verified cached reference."""
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.plan_frozen_candidate(
            frozen_plan,
            scoring_policy,
            reference_key,
            candidate_id=candidate_id,
            candidate_content_hash=candidate_content_hash,
            candidate_ref=candidate_ref,
            package_graph_ref=package_graph_ref,
            split=split,
            host=host,
            model=model,
            seed=seed,
            timeout_seconds=timeout_seconds,
        )
    emit(result, output_format)


@eval_app.command("export-work")
def export_work(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output: Annotated[Path, typer.Option("--output")],
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.export_work(output, limit)
        engine.snapshot_ledger()
    emit(result, output_format)


@eval_app.command("submit-work")
def submit_work(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    work_id: Annotated[str, typer.Option("--work-id")],
    output: Annotated[Path, typer.Option("--output")],
    host: Annotated[str, typer.Option("--host")],
    model: Annotated[str, typer.Option("--model")],
    host_task_id: Annotated[str, typer.Option("--host-task-id")],
    duration_ms: Annotated[int, typer.Option("--duration-ms")],
    context_id: Annotated[str | None, typer.Option("--context-id")] = None,
    artifact_root: Annotated[Path | None, typer.Option("--artifact-root")] = None,
    transcript: Annotated[Path | None, typer.Option("--transcript")] = None,
    package_access: Annotated[Path | None, typer.Option("--package-access")] = None,
    planned_trace: Annotated[Path | None, typer.Option("--planned-trace")] = None,
    observed_trace: Annotated[Path | None, typer.Option("--observed-trace")] = None,
    input_tokens: Annotated[int | None, typer.Option("--input-tokens")] = None,
    output_tokens: Annotated[int | None, typer.Option("--output-tokens")] = None,
    tool_calls: Annotated[int | None, typer.Option("--tool-calls")] = None,
    token_count_kind: Annotated[str, typer.Option("--token-count-kind")] = "estimated",
    failure_kind: Annotated[str | None, typer.Option("--failure-kind")] = None,
    failure_detail: Annotated[str | None, typer.Option("--failure-detail")] = None,
) -> None:
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        item = engine.ledger.get_work(work_id)
        submission = build_submission(
            Path.cwd(),
            item,
            host=host,
            model=model,
            host_task_id=host_task_id,
            context_id=context_id,
            duration_ms=duration_ms,
            artifact_root=artifact_root,
            transcript_path=transcript,
            package_access=_package_access(package_access),
            planned_trace=_trace(planned_trace, "planned_trace"),
            observed_trace=_trace(observed_trace, "observed_trace"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
            token_count_kind=_token_count_kind(token_count_kind),
            failure_kind=ProviderFailureKind(failure_kind) if failure_kind else None,
            failure_detail=failure_detail,
        )
    atomic_write(output, canonical_json_bytes(submission.model_dump(mode="json")))
    emit({"submission_id": submission.submission_id, "output": output.as_posix()}, "json")


@eval_app.command("prepare-grading")
def prepare_grading(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Export one blind Independent Grader work item per E2 artifact."""
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.functional_coordinator().prepare_graders()
    emit(result, output_format)


@eval_app.command("submit-grade")
def submit_grade(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    submission: Annotated[Path, typer.Option("--submission")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    value = IndependentGraderSubmission.model_validate_json(
        submission.read_text(encoding="utf-8")
    )
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.functional_coordinator().ingest_grader(value)
    emit(result, output_format)


@eval_app.command("prepare-comparison")
def prepare_comparison(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Export anonymous AB/BA work for pre-registered comparator cases."""
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.functional_coordinator().prepare_comparators()
    emit(result, output_format)


@eval_app.command("submit-comparison")
def submit_comparison(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    submission: Annotated[Path, typer.Option("--submission")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    value = ComparatorSubmission.model_validate_json(submission.read_text(encoding="utf-8"))
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.functional_coordinator().ingest_comparator(value)
    emit(result, output_format)


@eval_app.command("prepare-analysis")
def prepare_analysis(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Reconcile AB/BA records and export graph-linked Analyzer work."""
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        coordinator = engine.functional_coordinator()
        comparison = coordinator.reconcile_comparators()
        analysis = coordinator.prepare_analyzers()
    emit({"comparison": comparison, "analysis": analysis}, output_format)


@eval_app.command("reconcile-comparison")
def reconcile_comparison(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Reconcile fresh candidate AB/BA comparisons without dispatching analysis."""
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.functional_coordinator().reconcile_comparators()
    emit(result, output_format)


@eval_app.command("submit-analysis")
def submit_analysis(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    submission: Annotated[Path, typer.Option("--submission")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    value = AnalyzerSubmission.model_validate_json(submission.read_text(encoding="utf-8"))
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.functional_coordinator().ingest_analyzer(value)
    emit(result, output_format)


@eval_app.command("finalize-functional")
def finalize_functional(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Recompute six-dimensional vectors and seal role/access audits."""
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.functional_coordinator().finalize()
    emit(result, output_format)


@eval_app.command("verify-functional")
def verify_functional(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Independently recompute stored TaskScoreVectors from raw role evidence."""
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.functional_coordinator().verify_scores()
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@eval_app.command("ingest")
def ingest(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    submission: Annotated[Path, typer.Option("--submission")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    value = ExecutionBundle.model_validate_json(submission.read_text(encoding="utf-8"))
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.ingest(value)
    emit(result, output_format)


@eval_app.command("export-submission")
def export_submission(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    work_id: Annotated[str, typer.Option("--work-id")],
    output: Annotated[Path, typer.Option("--output")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Restore the canonical ingested ExecutionBundle from the Core ledger."""
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        submission = engine.ledger.submission_for_work(work_id)
        if submission is None:
            raise typer.BadParameter(f"no ingested submission for work_id: {work_id}")
    atomic_write(output, canonical_json_bytes(submission.model_dump(mode="json")))
    emit({"submission_id": submission.submission_id, "output": output.as_posix()}, output_format)


@eval_app.command("status")
def status(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    if has_onboarding_checkpoint(run_dir) and not (run_dir / "ledger.sqlite3").is_file():
        emit(EvalPlanOnboarding(Path.cwd(), run_dir).status(), output_format)
        return
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.status()
    emit(result, output_format)


@eval_app.command("resume")
def resume(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    if has_onboarding_checkpoint(run_dir):
        emit(EvalPlanOnboarding(Path.cwd(), run_dir).resume(), output_format)
        return
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        resumed = engine.ledger.resume_interrupted()
        engine.snapshot_ledger()
        result = {"resumed": resumed, "status": engine.ledger.status()}
    emit(result, output_format)


@eval_app.command("aggregate")
def aggregate(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.aggregate()
    emit(result, output_format)


@eval_app.command("replay")
def replay(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.replay_assertions()
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@eval_app.command("seal-run")
def seal_run(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Checkpoint the ledger and content-index all durable run artifacts."""
    with MultiFidelityEvalEngine(Path.cwd(), run_dir) as engine:
        result = engine.seal_run()
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@eval_app.command("audit-fidelity")
def audit_fidelity_command(
    path: Path,
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    result = audit_fidelity(item for item in path.rglob("*.json") if item.is_file())
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@eval_app.command("paired")
def paired(
    provider: Annotated[str, typer.Option("--provider")] = "mock",
    case_set: Annotated[str, typer.Option("--case-set")] = "eval-fixtures",
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    if provider != "mock" or case_set != "eval-fixtures":
        raise typer.BadParameter("paired self-test expects mock/eval-fixtures")
    result = mock_pair_diagnostic()
    emit(result, output_format)
    if not result["pair_comparable"] or not result["incompatible_pair_rejected"]:
        raise typer.Exit(2)


@eval_app.command("self-test")
def self_test(
    suite: Annotated[str, typer.Option("--suite")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    operations = {
        "fault-injection": fault_injection_diagnostic,
        "cache-resume": cache_resume_diagnostic,
    }
    if suite not in operations:
        raise typer.BadParameter(f"unknown suite: {suite}")
    result = operations[suite](Path.cwd())
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)
