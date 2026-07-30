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
    RoleAttemptTerminalization,
)
from gepase.evals.onboarding import EvalPlanOnboarding, has_onboarding_checkpoint
from gepase.evals.recovery import (
    RepairExhaustionTerminalization,
    WorkRecoveryAudit,
    build_recovered_submission,
    stage_recovery_evidence,
)
from gepase.evals.reference_runtime import load_reference_execution_config
from gepase.evals.schema import EvidenceTier
from gepase.evals.work_items import ExecutionBundle, PackageAccessEvent
from gepase.optimizer.session_runtime import (
    BudgetContinuationDecision,
    HostAttemptReason,
    MeasurementKind,
    RuntimeBarrier,
    UsageAllowance,
    build_host_attempt_accounting,
)
from gepase.package.analyzer import PackageAnalyzer
from gepase.run_lifecycle import RunLifecycleMode
from gepase.store.artifacts import ArtifactStore, atomic_write, canonical_json_bytes

eval_app = typer.Typer(no_args_is_help=True, help="Plan and ingest multi-fidelity evidence.")


def _eval_engine(
    run_dir: Path,
    mode: RunLifecycleMode = RunLifecycleMode.OPEN_EXISTING,
    *,
    expected_config_hash: str | None = None,
    run_id: str | None = None,
) -> MultiFidelityEvalEngine:
    return MultiFidelityEvalEngine(
        Path.cwd(),
        run_dir,
        lifecycle_mode=mode,
        expected_config_hash=expected_config_hash,
        run_id=run_id,
    )


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
        raise typer.BadParameter("token_count_kind must be reported, estimated, or unavailable")
    return cast(Literal["reported", "estimated", "unavailable"], value)


def _compile_reference_package(
    engine: MultiFidelityEvalEngine,
    *,
    skill_ref: str,
) -> dict[str, object]:
    """Compile the fresh static Package evidence required by a reference run.

    The frozen EvalPlan binds a snapshot hash, while its reference config points at
    the graph that belongs to the new run.  This small adapter deliberately owns
    neither graph semantics nor evaluation planning: it persists the existing
    PackageAnalyzer result before the existing Eval Engine validates and consumes
    it.
    """

    package_root = (engine.project_root / skill_ref).resolve(strict=True)
    if not package_root.is_relative_to(engine.project_root):
        raise ValueError("reference skill_ref must remain inside the project")
    result = PackageAnalyzer().analyze(package_root)
    engine.store.write_json("package/snapshot.json", result.snapshot.model_dump(mode="json"))
    engine.store.write_json("package/package-ir.json", result.package_ir.model_dump(mode="json"))
    engine.store.write_json("package/graph.json", result.graph.model_dump(mode="json"))
    engine.store.write_json(
        "package/diagnostics.json",
        {
            "schema_version": result.graph.schema_version,
            "package_id": result.graph.package_id,
            "diagnostics": [item.model_dump(mode="json") for item in result.graph.diagnostics],
        },
    )
    return {
        "package_id": result.snapshot.package_id,
        "package_snapshot_hash": result.snapshot.snapshot_hash,
        "file_count": len(result.snapshot.files),
        "graph_nodes": len(result.graph.nodes),
        "graph_edges": len(result.graph.edges),
        "static_graph_only": True,
        "diagnostic_count": len(result.graph.diagnostics),
        "valid": not any(item.severity == "error" for item in result.graph.diagnostics),
    }


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
    with _eval_engine(run_dir, RunLifecycleMode.CREATE_NEW) as engine:
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
    with _eval_engine(run_dir, RunLifecycleMode.CREATE_NEW) as engine:
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


@eval_app.command("plan-reference")
def plan_reference(
    config: Annotated[Path, typer.Option("--config")],
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Create one fresh paired reference using the existing Eval Engine."""

    frozen = load_reference_execution_config(config)
    if run_dir.name != frozen.run_id:
        raise typer.BadParameter("run-dir name must match reference config run_id")
    with _eval_engine(
        run_dir,
        RunLifecycleMode.CREATE_NEW,
        expected_config_hash=frozen.config_hash,
        run_id=frozen.run_id,
    ) as engine:
        engine.store.write_json("resolved-reference-config.json", frozen.model_dump(mode="json"))
        fresh_package = _compile_reference_package(engine, skill_ref=frozen.skill_ref)
        result = engine.plan_frozen_functional(
            Path(frozen.frozen_eval_plan_ref),
            Path(frozen.scoring_policy_ref),
            skill_ref=frozen.skill_ref,
            package_graph_ref=frozen.package_graph_ref,
            splits=frozen.splits,
            variants=frozen.variants,
            host=frozen.isolation.host,
            model=frozen.isolation.model,
            seed=frozen.seed,
            timeout_seconds=frozen.timeout_seconds,
        )
        engine.configure_runtime_budget(
            policy=frozen.active_session_budget_policy,
            config_hash=frozen.config_hash,
        )
        checkpoint = engine.pause_at_barrier(
            barrier=RuntimeBarrier.PACKAGE_COMPILED,
            next_role="executor",
            next_work_count=int(result["planned_work_items"]),
            continuation_risk_zh=("fresh Package 已编译; 继续后将导出完整 paired Executor 批次。"),
        )
    emit(
        {
            **result,
            "fresh_package": fresh_package,
            "budget_checkpoint": checkpoint,
        },
        output_format,
    )


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
    with _eval_engine(run_dir, RunLifecycleMode.CREATE_NEW) as engine:
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
    with _eval_engine(run_dir) as engine:
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
    evidence_file: Annotated[list[str] | None, typer.Option("--evidence-file")] = None,
    transcript: Annotated[Path | None, typer.Option("--transcript")] = None,
    package_access: Annotated[Path | None, typer.Option("--package-access")] = None,
    planned_trace: Annotated[Path | None, typer.Option("--planned-trace")] = None,
    observed_trace: Annotated[Path | None, typer.Option("--observed-trace")] = None,
    input_tokens: Annotated[int | None, typer.Option("--input-tokens")] = None,
    output_tokens: Annotated[int | None, typer.Option("--output-tokens")] = None,
    tool_calls: Annotated[int | None, typer.Option("--tool-calls")] = None,
    token_count_kind: Annotated[str, typer.Option("--token-count-kind")] = "estimated",
    repair_attempt: Annotated[bool, typer.Option("--repair-attempt")] = False,
    failure_kind: Annotated[str | None, typer.Option("--failure-kind")] = None,
    failure_detail: Annotated[str | None, typer.Option("--failure-detail")] = None,
) -> None:
    with _eval_engine(run_dir) as engine:
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
            artifact_relative_paths=(tuple(evidence_file) if evidence_file is not None else None),
            transcript_path=transcript,
            package_access=_package_access(package_access),
            planned_trace=_trace(planned_trace, "planned_trace"),
            observed_trace=_trace(observed_trace, "observed_trace"),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_calls=tool_calls,
            token_count_kind=_token_count_kind(token_count_kind),
            repair_attempt=repair_attempt,
            failure_kind=ProviderFailureKind(failure_kind) if failure_kind else None,
            failure_detail=failure_detail,
        )
    atomic_write(output, canonical_json_bytes(submission.model_dump(mode="json")))
    emit({"submission_id": submission.submission_id, "output": output.as_posix()}, "json")


@eval_app.command("terminalize-repair-exhaustion")
def terminalize_repair_exhaustion(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    terminalization: Annotated[Path, typer.Option("--terminalization")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Close one exhausted work as a typed failure without another Agent call."""

    value = RepairExhaustionTerminalization.model_validate_json(
        terminalization.read_text(encoding="utf-8")
    )
    with _eval_engine(run_dir) as engine:
        result = engine.terminalize_repair_exhaustion(value)
    emit(result, output_format)


@eval_app.command("terminalize-role-attempts")
def terminalize_role_attempts(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    terminalization: Annotated[Path, typer.Option("--terminalization")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Close exhausted functional-role attempts through the existing coordinator."""

    value = RoleAttemptTerminalization.model_validate_json(
        terminalization.read_text(encoding="utf-8")
    )
    with _eval_engine(run_dir) as engine:
        result = engine.functional_coordinator().terminalize_role_attempts(value)
        engine.snapshot_ledger()
    emit(result, output_format)


@eval_app.command("prepare-recovered-submission")
def prepare_recovered_submission(
    audit: Annotated[Path, typer.Option("--audit")],
    staging_root: Annotated[Path, typer.Option("--staging-root")],
    output: Annotated[Path, typer.Option("--output")],
) -> None:
    """Stage a required-only bundle from immutable evidence; never run an Agent."""

    value = WorkRecoveryAudit.model_validate_json(audit.read_text(encoding="utf-8"))
    staged = stage_recovery_evidence(Path.cwd(), value, staging_root)
    submission = build_recovered_submission(Path.cwd(), value, staged)
    payload = canonical_json_bytes(submission.model_dump(mode="json"))
    if output.exists():
        if not output.is_file() or output.read_bytes() != payload:
            raise FileExistsError("append-only recovered submission output already differs")
    else:
        atomic_write(output, payload)
    emit(
        {
            "submission_id": submission.submission_id,
            "manifest_id": value.manifest.manifest_id,
            "agent_calls": 0,
            "output": output.as_posix(),
        },
        "json",
    )


@eval_app.command("ingest-recovered")
def ingest_recovered(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    submission: Annotated[Path, typer.Option("--submission")],
    audit: Annotated[Path, typer.Option("--audit")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Ingest deterministic repackaging against already-accounted Host attempts."""

    submission_value = ExecutionBundle.model_validate_json(submission.read_text(encoding="utf-8"))
    audit_value = WorkRecoveryAudit.model_validate_json(audit.read_text(encoding="utf-8"))
    with _eval_engine(run_dir) as engine:
        result = engine.ingest_recovered_submission(submission_value, audit_value)
    emit(result, output_format)


@eval_app.command("post-recovery-checkpoint")
def post_recovery_checkpoint(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    work_id: Annotated[list[str], typer.Option("--work-id")],
    next_role: Annotated[str, typer.Option("--next-role")],
    next_work_count: Annotated[int, typer.Option("--next-work-count", min=1)],
    continuation_risk_zh: Annotated[str, typer.Option("--continuation-risk-zh")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Force a fresh runtime checkpoint after deterministic recovery ingest."""

    with _eval_engine(run_dir) as engine:
        result = engine.pause_after_recovery(
            recovered_work_ids=tuple(work_id),
            next_role=next_role,
            next_work_count=next_work_count,
            continuation_risk_zh=continuation_risk_zh,
        )
    emit(result, output_format)


@eval_app.command("prepare-grading")
def prepare_grading(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Export one blind Independent Grader work item per E2 artifact."""
    with _eval_engine(run_dir) as engine:
        result = engine.functional_coordinator().prepare_graders()
        engine.snapshot_ledger()
    emit(result, output_format)


@eval_app.command("submit-grade")
def submit_grade(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    submission: Annotated[Path, typer.Option("--submission")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    value = IndependentGraderSubmission.model_validate_json(submission.read_text(encoding="utf-8"))
    with _eval_engine(run_dir) as engine:
        result = engine.functional_coordinator().ingest_grader(value)
        engine.snapshot_ledger()
    emit(result, output_format)


@eval_app.command("prepare-comparison")
def prepare_comparison(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Export anonymous AB/BA work for pre-registered comparator cases."""
    with _eval_engine(run_dir) as engine:
        result = engine.functional_coordinator().prepare_comparators()
        engine.snapshot_ledger()
    emit(result, output_format)


@eval_app.command("submit-comparison")
def submit_comparison(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    submission: Annotated[Path, typer.Option("--submission")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    value = ComparatorSubmission.model_validate_json(submission.read_text(encoding="utf-8"))
    with _eval_engine(run_dir) as engine:
        result = engine.functional_coordinator().ingest_comparator(value)
        engine.snapshot_ledger()
    emit(result, output_format)


@eval_app.command("prepare-analysis")
def prepare_analysis(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Reconcile AB/BA records and export graph-linked Analyzer work."""
    with _eval_engine(run_dir) as engine:
        coordinator = engine.functional_coordinator()
        comparison = coordinator.reconcile_comparators()
        analysis = coordinator.prepare_analyzers()
        engine.snapshot_ledger()
    emit({"comparison": comparison, "analysis": analysis}, output_format)


@eval_app.command("reconcile-comparison")
def reconcile_comparison(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Reconcile fresh candidate AB/BA comparisons without dispatching analysis."""
    with _eval_engine(run_dir) as engine:
        result = engine.functional_coordinator().reconcile_comparators()
        engine.snapshot_ledger()
    emit(result, output_format)


@eval_app.command("submit-analysis")
def submit_analysis(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    submission: Annotated[Path, typer.Option("--submission")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    value = AnalyzerSubmission.model_validate_json(submission.read_text(encoding="utf-8"))
    with _eval_engine(run_dir) as engine:
        result = engine.functional_coordinator().ingest_analyzer(value)
        engine.snapshot_ledger()
    emit(result, output_format)


@eval_app.command("finalize-functional")
def finalize_functional(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Recompute six-dimensional vectors and seal role/access audits."""
    with _eval_engine(run_dir) as engine:
        result = engine.functional_coordinator().finalize()
        engine.snapshot_ledger()
    emit(result, output_format)


@eval_app.command("verify-functional")
def verify_functional(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Independently recompute stored TaskScoreVectors from raw role evidence."""
    with _eval_engine(run_dir) as engine:
        result = engine.functional_coordinator().verify_scores()
        engine.snapshot_ledger()
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
    with _eval_engine(run_dir) as engine:
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
    with _eval_engine(run_dir) as engine:
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
    with _eval_engine(run_dir) as engine:
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
    with _eval_engine(run_dir, RunLifecycleMode.RESUME) as engine:
        result = engine.resume()
    emit(result, output_format)


@eval_app.command("runtime-checkpoint")
def runtime_checkpoint(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    barrier: Annotated[RuntimeBarrier, typer.Option("--barrier")],
    next_role: Annotated[str | None, typer.Option("--next-role")] = None,
    next_work_count: Annotated[int, typer.Option("--next-work-count")] = 0,
    continuation_risk_zh: Annotated[
        str, typer.Option("--continuation-risk-zh")
    ] = "继续会导出下一个预注册原子批次。",
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Pause an Eval run at a frozen barrier and render its review checkpoint."""

    with _eval_engine(run_dir) as engine:
        result = engine.pause_at_barrier(
            barrier=barrier,
            next_role=next_role,
            next_work_count=next_work_count,
            continuation_risk_zh=continuation_risk_zh,
        )
    emit(result, output_format)


@eval_app.command("runtime-continue")
def runtime_continue(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    decision: Annotated[Path, typer.Option("--decision")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Apply one append-only, hash-bound user continuation decision."""

    value = BudgetContinuationDecision.model_validate_json(decision.read_text(encoding="utf-8"))
    with _eval_engine(run_dir) as engine:
        result = engine.apply_continuation_decision(value)
    emit(result, output_format)


@eval_app.command("record-host-attempt")
def record_host_attempt(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    role: Annotated[str, typer.Option("--role")],
    host_task_id: Annotated[str, typer.Option("--host-task-id")],
    context_id: Annotated[str, typer.Option("--context-id")],
    reason: Annotated[HostAttemptReason, typer.Option("--reason")],
    reason_zh: Annotated[str, typer.Option("--reason-zh")],
    evidence_ref: Annotated[list[str], typer.Option("--evidence-ref")],
    estimated_tokens: Annotated[int, typer.Option("--estimated-tokens")],
    duration_ms: Annotated[int, typer.Option("--duration-ms")],
    repairs: Annotated[int, typer.Option("--repairs")] = 0,
    work_id: Annotated[str | None, typer.Option("--work-id")] = None,
    token_count_kind: Annotated[MeasurementKind, typer.Option("--token-count-kind")] = (
        MeasurementKind.ESTIMATED
    ),
    duration_kind: Annotated[MeasurementKind, typer.Option("--duration-kind")] = (
        MeasurementKind.ESTIMATED
    ),
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Append one failed/repair Agent-host context to the owner runtime ledger."""

    with _eval_engine(run_dir) as engine:
        runtime = engine._budget_runtime()
        if runtime is None:
            raise typer.BadParameter("run has no active-session runtime budget")
        accounting = build_host_attempt_accounting(
            run_id=runtime.run_id,
            config_hash=runtime.config_hash,
            role=role,
            host_task_id=host_task_id,
            context_id=context_id,
            work_id=work_id,
            reason=reason,
            usage=UsageAllowance(
                agent_calls=1,
                estimated_tokens=estimated_tokens,
                active_wall_clock_ms=0,
                repairs=repairs,
            ),
            token_count_kind=token_count_kind,
            agent_duration_ms=duration_ms,
            duration_kind=duration_kind,
            reason_zh=reason_zh,
            evidence_refs=tuple(evidence_ref),
        )
        result = engine.record_host_attempt_accounting(accounting)
    emit(result, output_format)


@eval_app.command("aggregate")
def aggregate(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    with _eval_engine(run_dir) as engine:
        result = engine.aggregate()
    emit(result, output_format)


@eval_app.command("replay")
def replay(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    with _eval_engine(run_dir) as engine:
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
    with _eval_engine(run_dir) as engine:
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
