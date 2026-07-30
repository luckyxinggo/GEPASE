"""Candidate, GEPA-adapter, and ASI contract diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, cast

import typer

from gepase.cli.app_support import emit
from gepase.mutation.proposer import (
    PatchProposalStore,
    PatchProposalSubmission,
    build_failed_patch_submission,
    build_patch_submission,
)
from gepase.optimizer.candidate import build_seed_candidate
from gepase.optimizer.diagnostics import (
    adapter_contract_diagnostic,
    asi_audit_diagnostic,
    checkpoint_resume_diagnostic,
)
from gepase.optimizer.evolution_controller import (
    CandidateReflectionSubmission,
    R4EvolutionController,
)
from gepase.optimizer.materialize import materialize_candidate
from gepase.optimizer.session_runtime import (
    BudgetContinuationDecision,
    RuntimeBarrier,
)
from gepase.run_lifecycle import RunLifecycleMode
from gepase.store.artifacts import atomic_write, canonical_json_bytes

candidate_app = typer.Typer(no_args_is_help=True, help="Inspect package candidates.")
optimizer_app = typer.Typer(no_args_is_help=True, help="Run optimizer contract diagnostics.")
asi_app = typer.Typer(no_args_is_help=True, help="Audit reflective ASI datasets.")


def _r4(
    run_dir: Path,
    config: Path,
    mode: RunLifecycleMode = RunLifecycleMode.OPEN_EXISTING,
) -> R4EvolutionController:
    return R4EvolutionController(Path.cwd(), run_dir, config, lifecycle_mode=mode)


@candidate_app.command("roundtrip")
def candidate_roundtrip(
    packages: Annotated[list[Path], typer.Argument()],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    root = Path.cwd().resolve()
    rows = []
    local = root / "artifacts/local/candidate-roundtrip"
    for package in packages:
        reference = package.resolve().relative_to(root).as_posix()
        candidate = build_seed_candidate(root, reference, run_id="candidate-roundtrip")
        destination = local / candidate.candidate_id / "package"
        if destination.parent.exists():
            import shutil

            shutil.rmtree(destination.parent)
        manifest = materialize_candidate(root, candidate, destination)
        rows.append(
            {
                "package": reference,
                "candidate_id": candidate.candidate_id,
                "component_count": len(candidate.components),
                **manifest.model_dump(mode="json"),
            }
        )
    result = {
        "valid": len(rows) == len(packages)
        and all(
            row["file_set_equal"] and row["content_hash_equal"] and row["permission_policy_equal"]
            for row in rows
        ),
        "packages": rows,
    }
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@optimizer_app.command("contract-test")
def optimizer_contract_test(
    provider: Annotated[str, typer.Option("--provider")] = "mock",
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    if provider != "mock":
        raise typer.BadParameter("contract test uses the deterministic mock provider")
    result = adapter_contract_diagnostic(Path.cwd())
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@optimizer_app.command("checkpoint-resume-test")
def checkpoint_resume_test(
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    result = checkpoint_resume_diagnostic(Path.cwd())
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@asi_app.command("audit")
def asi_audit(
    fixture: Annotated[str, typer.Option("--fixture")] = "failure-corpus",
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    if fixture != "failure-corpus":
        raise typer.BadParameter("unknown ASI audit fixture")
    result = asi_audit_diagnostic(Path.cwd())
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@optimizer_app.command("r4-init")
def r4_init(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    emit(
        _r4(run_dir, config, RunLifecycleMode.CREATE_NEW).initialize(),
        output_format,
    )


@optimizer_app.command("r4-prepare-proposals")
def r4_prepare_proposals(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Build bounded proposal scope after package-compile approval."""

    emit(_r4(run_dir, config).prepare_initial_proposals(), output_format)


@optimizer_app.command("r4-next-proposal")
def r4_next_proposal(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    output: Annotated[Path | None, typer.Option("--output")] = None,
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    controller = _r4(run_dir, config)
    work = controller.export_next_proposal()
    payload = work.model_dump(mode="json") if work else {"work_type": "none"}
    if output is not None:
        atomic_write(output, canonical_json_bytes(payload))
    emit(payload, output_format)


@optimizer_app.command("r4-resume")
def r4_resume(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    emit(_r4(run_dir, config, RunLifecycleMode.RESUME).resume(), output_format)


@optimizer_app.command("r4-checkpoint")
def r4_checkpoint(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    barrier: Annotated[RuntimeBarrier, typer.Option("--barrier")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    emit(_r4(run_dir, config).pause_at_barrier(barrier), output_format)


@optimizer_app.command("r4-continue")
def r4_continue(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    decision: Annotated[Path, typer.Option("--decision")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    value = BudgetContinuationDecision.model_validate_json(decision.read_text(encoding="utf-8"))
    emit(_r4(run_dir, config).apply_continuation_decision(value), output_format)


@optimizer_app.command("r4-ingest-proposal")
def r4_ingest_proposal(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    submission: Annotated[Path, typer.Option("--submission")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    value = PatchProposalSubmission.model_validate_json(submission.read_text(encoding="utf-8"))
    emit(_r4(run_dir, config).ingest_proposal(value), output_format)


@optimizer_app.command("r4-build-proposal-submission")
def r4_build_proposal_submission(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    work_id: Annotated[str, typer.Option("--work-id")],
    proposal: Annotated[Path, typer.Option("--proposal")],
    output: Annotated[Path, typer.Option("--output")],
    host: Annotated[str, typer.Option("--host")],
    model: Annotated[str, typer.Option("--model")],
    host_task_id: Annotated[str, typer.Option("--host-task-id")],
    duration_ms: Annotated[int, typer.Option("--duration-ms")],
    token_estimate: Annotated[int, typer.Option("--token-estimate")] = 0,
    valid_on_first_attempt: Annotated[
        bool, typer.Option("--valid-on-first-attempt/--repaired")
    ] = True,
    repair_count: Annotated[int, typer.Option("--repair-count")] = 0,
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    controller = _r4(run_dir, config)
    with PatchProposalStore(controller.run_dir / "proposal-work.sqlite3") as store:
        work = store.get_work(work_id)
    raw = json.loads(proposal.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise typer.BadParameter("proposal must be a JSON object")
    submission = build_patch_submission(
        work,
        raw,
        host=host,
        model=model,
        host_task_id=host_task_id,
        duration_ms=duration_ms,
        token_estimate=token_estimate,
        valid_on_first_attempt=valid_on_first_attempt,
        repair_count=repair_count,
    )
    atomic_write(output, canonical_json_bytes(submission.model_dump(mode="json")))
    emit(
        {"submission_id": submission.submission_id, "output": output.as_posix()},
        output_format,
    )


@optimizer_app.command("r4-build-failed-proposal-submission")
def r4_build_failed_proposal_submission(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    work_id: Annotated[str, typer.Option("--work-id")],
    output: Annotated[Path, typer.Option("--output")],
    host: Annotated[str, typer.Option("--host")],
    model: Annotated[str, typer.Option("--model")],
    host_task_id: Annotated[str, typer.Option("--host-task-id")],
    duration_ms: Annotated[int, typer.Option("--duration-ms")],
    token_estimate: Annotated[int, typer.Option("--token-estimate")] = 0,
    failure_kind: Annotated[str, typer.Option("--failure-kind")] = "submission_validation_failure",
    failure_detail: Annotated[str, typer.Option("--failure-detail")] = "",
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Build a typed failed submission without fabricating a PackagePatch."""

    if not failure_detail:
        raise typer.BadParameter("failure-detail must describe the raw response failure")
    controller = _r4(run_dir, config)
    with PatchProposalStore(controller.run_dir / "proposal-work.sqlite3") as store:
        work = store.get_work(work_id)
    submission = build_failed_patch_submission(
        work,
        host=host,
        model=model,
        host_task_id=host_task_id,
        duration_ms=duration_ms,
        token_estimate=token_estimate,
        failure_kind=failure_kind,
        failure_detail=failure_detail,
    )
    atomic_write(output, canonical_json_bytes(submission.model_dump(mode="json")))
    emit(
        {"submission_id": submission.submission_id, "output": output.as_posix()},
        output_format,
    )


@optimizer_app.command("r4-prepare-proposal-repair")
def r4_prepare_proposal_repair(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    failed_work_id: Annotated[str, typer.Option("--failed-work-id")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Append a bounded repair work item after an ingested proposer failure."""

    emit(_r4(run_dir, config).prepare_proposal_repair(failed_work_id), output_format)


@optimizer_app.command("r4-prepare-proposal-repairs")
def r4_prepare_proposal_repairs(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    failed_work_id: Annotated[list[str], typer.Option("--failed-work-id")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Append one atomic bounded repair batch for failed proposer work."""

    emit(
        _r4(run_dir, config).prepare_proposal_repairs(tuple(failed_work_id)),
        output_format,
    )


@optimizer_app.command("r4-apply-proposals")
def r4_apply_proposals(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    emit(_r4(run_dir, config).apply_proposals(), output_format)


@optimizer_app.command("r4-prepare-recovery")
def r4_prepare_recovery(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    rejected_candidate_id: Annotated[str, typer.Option("--rejected-candidate-id")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    emit(
        _r4(run_dir, config).prepare_recovery_proposal(rejected_candidate_id),
        output_format,
    )


@optimizer_app.command("r4-apply-recovery")
def r4_apply_recovery(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    work_id: Annotated[str, typer.Option("--work-id")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    emit(_r4(run_dir, config).apply_recovery_proposal(work_id), output_format)


@optimizer_app.command("r4-plan-candidate")
def r4_plan_candidate(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    candidate_id: Annotated[str, typer.Option("--candidate-id")],
    split: Annotated[str, typer.Option("--split")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    if split not in {"train", "validation"}:
        raise typer.BadParameter("split must be train or validation")
    emit(
        _r4(run_dir, config).plan_candidate(
            candidate_id, cast(Literal["train", "validation"], split)
        ),
        output_format,
    )


@optimizer_app.command("r4-plan-generation2")
def r4_plan_generation2(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    parent_candidate_id: Annotated[str | None, typer.Option("--parent-candidate-id")] = None,
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    """Plan one bounded generation-2 refinement without running an Agent."""

    emit(
        _r4(run_dir, config).plan_generation2_refinement(parent_candidate_id),
        output_format,
    )


@optimizer_app.command("r4-pre-eval-gates")
def r4_pre_eval_gates(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    candidate_id: Annotated[str, typer.Option("--candidate-id")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    emit(_r4(run_dir, config).record_pre_eval_gates(candidate_id), output_format)


@optimizer_app.command("r4-admit-train")
def r4_admit_train(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    candidate_id: Annotated[str, typer.Option("--candidate-id")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    emit(_r4(run_dir, config).admit_train(candidate_id), output_format)


@optimizer_app.command("r4-finalize-validation")
def r4_finalize_validation(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    candidate_id: Annotated[str, typer.Option("--candidate-id")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    emit(_r4(run_dir, config).finalize_validation(candidate_id), output_format)


@optimizer_app.command("r4-build-merge")
def r4_build_merge(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    emit(_r4(run_dir, config).build_merge(), output_format)


@optimizer_app.command("r4-gepa-snapshot")
def r4_gepa_snapshot(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    emit(_r4(run_dir, config).write_gepa_snapshot(), output_format)


@optimizer_app.command("r4-prepare-reflection")
def r4_prepare_reflection(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    candidate_id: Annotated[str, typer.Option("--candidate-id")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    emit(_r4(run_dir, config).prepare_reflection(candidate_id), output_format)


@optimizer_app.command("r4-ingest-reflection")
def r4_ingest_reflection(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    submission: Annotated[Path, typer.Option("--submission")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    value = CandidateReflectionSubmission.model_validate_json(
        submission.read_text(encoding="utf-8")
    )
    emit(_r4(run_dir, config).ingest_reflection(value), output_format)


@optimizer_app.command("r4-status")
def r4_status(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    emit(_r4(run_dir, config).state(), output_format)


@optimizer_app.command("r4-audit")
def r4_audit(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    result = _r4(run_dir, config).audit()
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@optimizer_app.command("r4-complete")
def r4_complete(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    emit(_r4(run_dir, config).complete(), output_format)


@optimizer_app.command("r4-seal")
def r4_seal(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    config: Annotated[Path, typer.Option("--config")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    result = _r4(run_dir, config).seal()
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)
