"""S6 selector, PackagePatch, proposal-work, and application CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from gepase.cli.app_support import emit
from gepase.mutation.applier import apply_package_patch
from gepase.mutation.causal import audit_causality
from gepase.mutation.diagnostics import (
    patch_schema_fuzz,
    proposal_summary,
    rollback_diagnostic,
    selector_benchmark,
    selector_explanation_audit,
    selector_viability,
)
from gepase.mutation.proposer import (
    PatchProposalStore,
    PatchProposalSubmission,
    PatchProposalWorkItem,
    build_failed_patch_submission,
    build_patch_submission,
    draft_replacement_proposal,
    prepare_proposal_workspace,
)
from gepase.mutation.schema import PackagePatch
from gepase.optimizer.candidate import PackageCandidate
from gepase.store.artifacts import atomic_write, canonical_json_bytes

mutation_app = typer.Typer(no_args_is_help=True, help="Plan and apply bounded PackagePatch work.")


@mutation_app.command("next-work")
def next_work(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output: Annotated[Path | None, typer.Option("--output")] = None,
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    with PatchProposalStore(run_dir / "proposals.sqlite3") as store:
        work = store.next_work()
        store.write_snapshot(run_dir)
    payload = work.model_dump(mode="json") if work else {"work_type": "none"}
    if output is not None:
        atomic_write(output, canonical_json_bytes(payload))
    emit(payload, output_format)


@mutation_app.command("submit-proposal")
def submit_proposal(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    work_id: Annotated[str, typer.Option("--work-id")],
    proposal: Annotated[Path, typer.Option("--proposal")],
    output: Annotated[Path, typer.Option("--output")],
    host: Annotated[str, typer.Option("--host")],
    model: Annotated[str, typer.Option("--model")],
    host_task_id: Annotated[str, typer.Option("--host-task-id")],
    duration_ms: Annotated[int, typer.Option("--duration-ms")],
    token_estimate: Annotated[int, typer.Option("--token-estimate")] = 0,
    failure_kind: Annotated[str | None, typer.Option("--failure-kind")] = None,
    failure_detail: Annotated[str | None, typer.Option("--failure-detail")] = None,
) -> None:
    with PatchProposalStore(run_dir / "proposals.sqlite3") as store:
        work = store.get_work(work_id)
    if failure_kind:
        submission = build_failed_patch_submission(
            work,
            host=host,
            model=model,
            host_task_id=host_task_id,
            duration_ms=duration_ms,
            token_estimate=token_estimate,
            failure_kind=failure_kind,
            failure_detail=failure_detail or failure_kind,
        )
    else:
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
        )
    atomic_write(output, canonical_json_bytes(submission.model_dump(mode="json")))
    emit({"submission_id": submission.submission_id, "output": output.as_posix()}, "json")


@mutation_app.command("prepare-workspace")
def prepare_workspace(
    work_item: Annotated[Path, typer.Option("--work-item")],
    output_dir: Annotated[Path, typer.Option("--output-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    work = PatchProposalWorkItem.model_validate_json(work_item.read_text(encoding="utf-8"))
    emit(prepare_proposal_workspace(work, output_dir), output_format)


@mutation_app.command("draft-replacement")
def draft_replacement(
    work_item: Annotated[Path, typer.Option("--work-item")],
    replacement: Annotated[Path, typer.Option("--replacement")],
    output: Annotated[Path, typer.Option("--output")],
    summary: Annotated[str, typer.Option("--summary")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    work = PatchProposalWorkItem.model_validate_json(work_item.read_text(encoding="utf-8"))
    proposal = draft_replacement_proposal(work, replacement, summary=summary)
    atomic_write(output, canonical_json_bytes(proposal))
    emit({"work_id": work.work_id, "output": output.as_posix()}, output_format)


@mutation_app.command("ingest")
def ingest(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    submission: Annotated[Path, typer.Option("--submission")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    typed = PatchProposalSubmission.model_validate_json(submission.read_text(encoding="utf-8"))
    with PatchProposalStore(run_dir / "proposals.sqlite3") as store:
        inserted = store.ingest(typed)
        store.write_snapshot(run_dir)
        status = store.status()
    emit({"ingested": inserted, "status": status}, output_format)


@mutation_app.command("status")
def status(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    with PatchProposalStore(run_dir / "proposals.sqlite3") as store:
        emit(store.status(), output_format)


@mutation_app.command("resume")
def resume(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    with PatchProposalStore(run_dir / "proposals.sqlite3") as store:
        resumed = store.resume()
        store.write_snapshot(run_dir)
    emit({"resumed": resumed}, output_format)


@mutation_app.command("apply")
def apply(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    work_id: Annotated[str, typer.Option("--work-id")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    parent = PackageCandidate.model_validate_json(
        (run_dir / "parent-candidate.json").read_text(encoding="utf-8")
    )
    with PatchProposalStore(run_dir / "proposals.sqlite3") as store:
        submission = next((item for item in store.submissions() if item.work_id == work_id), None)
    if submission is None:
        raise typer.BadParameter(f"completed proposal submission is missing: {work_id}")
    if submission.patch is None:
        raise typer.BadParameter("proposal submission has no patch")
    application, candidate = apply_package_patch(
        Path.cwd(),
        parent,
        submission.patch,
        run_dir / "workspaces",
        run_id=submission.patch.proposal_work_id,
    )
    payload = {
        "application": application.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json") if candidate else None,
    }
    emit(payload, output_format)


patch_app = typer.Typer(
    no_args_is_help=True, help="Validate PackagePatch schemas and applications."
)
selector_app = typer.Typer(
    no_args_is_help=True, help="Benchmark explainable mutation target selectors."
)


@patch_app.command("validate")
def validate_patch(
    path: Path,
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    patch = PackagePatch.model_validate_json(path.read_text(encoding="utf-8"))
    emit(
        {"valid": True, "patch_id": patch.patch_id, "fingerprint": patch.fingerprint}, output_format
    )


@patch_app.command("schema-test")
def schema_test(
    fuzz_cases: Annotated[int, typer.Option("--fuzz-cases")] = 1_000,
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    result = patch_schema_fuzz(Path.cwd(), fuzz_cases)
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@patch_app.command("rollback-test")
def rollback_test(
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    result = rollback_diagnostic(Path.cwd())
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@selector_app.command("benchmark")
def benchmark_selector(
    corpus: Annotated[Path, typer.Argument()] = Path("benchmarks/fault_localization.jsonl"),
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    result = selector_benchmark(Path.cwd(), corpus)
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@selector_app.command("audit-explanations")
def audit_selector_explanations(
    corpus: Annotated[Path, typer.Argument()] = Path("benchmarks/fault_localization.jsonl"),
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    result = selector_explanation_audit(Path.cwd(), corpus)
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@mutation_app.command("audit-proposals")
def audit_proposals(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    result = proposal_summary(run_dir)
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@mutation_app.command("audit-causality")
def audit_causal_proposals(
    run_dir: Annotated[Path, typer.Argument()],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    with PatchProposalStore(run_dir / "proposals.sqlite3") as store:
        result = audit_causality(store.work_items(), store.submissions())
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)


@selector_app.command("viability")
def viability(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
    output_format: Annotated[str, typer.Option("--format")] = "json",
) -> None:
    result = selector_viability(run_dir)
    emit(result, output_format)
    if not result["valid"]:
        raise typer.Exit(2)
