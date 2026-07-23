"""Core-owned EvalPlan onboarding, review, freeze, and resume workflow."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gepase.evals.eval_plan import (
    EvalDesignBrief,
    EvalDesignerSubmission,
    EvalDesignerWorkItem,
    EvalPlanCheckpoint,
    EvalPlanCheckReport,
    EvalPlanDraft,
    EvalPlanState,
    EvalReviewSubmission,
    FrozenEvalPlan,
    FunctionalEvalCase,
    PackageDesignHint,
    ReviewDecisionKind,
    SourceProvenance,
    TriggerEvalCase,
)
from gepase.evals.eval_plan_checks import run_eval_plan_checks, verify_upstream_tree_manifest
from gepase.evals.work_items import PackageAccessKind, canonical_hash, work_id_for
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.ir import NodeKind, PackageGraph
from gepase.reporting.eval_review import render_eval_review
from gepase.store.artifacts import ArtifactStore


class EvalPlanOnboarding:
    """Persist one Package-specific EvalPlan revision in a resumable run directory."""

    def __init__(self, repo_root: Path, run_dir: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.run_dir = run_dir.resolve()
        self.store = ArtifactStore(self.run_dir)

    def _repo_path(self, value: Path, *, kind: str) -> tuple[Path, str]:
        resolved = value.resolve()
        if not resolved.is_relative_to(self.repo_root):
            raise ValueError(f"{kind} must be inside the repository")
        relative = resolved.relative_to(self.repo_root).as_posix()
        if relative == "skills_test" or relative.startswith("skills_test/"):
            raise ValueError("private Skill packages are outside the R2 scope")
        return resolved, relative

    def _write_checkpoint(self, checkpoint: EvalPlanCheckpoint, event: str) -> None:
        self.store.write_json("checkpoint.json", checkpoint.model_dump(mode="json"))
        self.store.append_event(
            "onboarding-events.jsonl",
            {
                "event": event,
                "state": checkpoint.state.value,
                "run_id": checkpoint.run_id,
                "package_id": checkpoint.package_id,
                "draft_hash": checkpoint.draft_hash,
                "review_id": checkpoint.review_id,
                "frozen_plan_hash": checkpoint.frozen_plan_hash,
                "occurred_at": checkpoint.updated_at.isoformat(),
            },
        )
        self.store.index_existing("onboarding-events.jsonl", "application/x-ndjson")

    def checkpoint(self) -> EvalPlanCheckpoint:
        path = self.run_dir / "checkpoint.json"
        if not path.is_file():
            raise FileNotFoundError("EvalPlan onboarding checkpoint does not exist")
        return EvalPlanCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))

    def _model(self, relative: str, model: type[Any]) -> Any:
        return model.model_validate_json((self.run_dir / relative).read_text(encoding="utf-8"))

    def start(
        self,
        *,
        package: Path,
        provenance_path: Path,
        design_brief_path: Path,
    ) -> dict[str, object]:
        if (self.run_dir / "checkpoint.json").exists():
            raise ValueError("onboarding run already exists; inspect or resume it")
        package_root, package_ref = self._repo_path(package, kind="package")
        provenance_file, provenance_ref = self._repo_path(provenance_path, kind="source provenance")
        design_file, _ = self._repo_path(design_brief_path, kind="design brief")
        provenance = SourceProvenance.model_validate_json(
            provenance_file.read_text(encoding="utf-8")
        )
        brief = EvalDesignBrief.model_validate_json(design_file.read_text(encoding="utf-8"))
        tree_valid, tree_problems = verify_upstream_tree_manifest(self.repo_root, provenance)
        if not tree_valid:
            raise ValueError(
                "vendored Package does not match pinned upstream tree manifest: "
                + ", ".join(tree_problems)
            )
        result = PackageAnalyzer().analyze(package_root)
        if result.snapshot.snapshot_hash != provenance.package_snapshot_hash:
            raise ValueError("vendored PackageSnapshot does not match pinned provenance")
        if provenance.vendored_ref != package_ref:
            raise ValueError("provenance vendored_ref does not match package argument")

        self.store.write_json("source-provenance.json", provenance.model_dump(mode="json"))
        self.store.write_json("design-brief.json", brief.model_dump(mode="json"))
        self.store.write_json("package/snapshot.json", result.snapshot.model_dump(mode="json"))
        self.store.write_json("package/package-ir.json", result.package_ir.model_dump(mode="json"))
        self.store.write_json("package/graph.json", result.graph.model_dump(mode="json"))
        self.store.write_json(
            "package/diagnostics.json",
            {
                "schema_version": result.graph.schema_version,
                "package_id": result.graph.package_id,
                "diagnostics": [item.model_dump(mode="json") for item in result.graph.diagnostics],
            },
        )

        hint_kinds = {
            NodeKind.FRONTMATTER,
            NodeKind.SECTION,
            NodeKind.PYTHON_MODULE,
            NodeKind.FUNCTION,
            NodeKind.DEPENDENCY,
        }
        hints = tuple(
            PackageDesignHint(
                kind=node.kind.value,
                path=node.path,
                label=node.label,
                node_id=node.node_id,
            )
            for node in result.graph.nodes
            if node.kind in hint_kinds
        )
        work_payload: dict[str, object] = {
            "role": "eval_designer",
            "skill_id": result.snapshot.package_id,
            "snapshot_hash": result.snapshot.snapshot_hash,
            "brief_id": brief.brief_id,
            "run_id": self.run_dir.name,
        }
        work_item = EvalDesignerWorkItem(
            work_id=work_id_for(work_payload).replace("work-", "eval-design-"),
            skill_id=result.snapshot.package_id,
            skill_ref=package_ref,
            package_snapshot_hash=result.snapshot.snapshot_hash,
            package_graph_ref=(self.run_dir / "package/graph.json")
            .relative_to(self.repo_root)
            .as_posix(),
            package_diagnostics_ref=(self.run_dir / "package/diagnostics.json")
            .relative_to(self.repo_root)
            .as_posix(),
            source_provenance_ref=provenance_ref,
            design_brief=brief,
            package_hints=hints,
            submission_schema_ref="schemas/eval_designer_submission.schema.json",
            required_package_reads=tuple(item.path for item in result.snapshot.files),
        )
        self.store.write_json("designer-work-item.json", work_item.model_dump(mode="json"))
        checkpoint = EvalPlanCheckpoint(
            run_id=self.run_dir.name,
            state=EvalPlanState.PACKAGE_PARSED,
            package_id=result.snapshot.package_id,
            package_snapshot_hash=result.snapshot.snapshot_hash,
            designer_work_id=work_item.work_id,
        )
        self._write_checkpoint(checkpoint, "package_parsed_and_designer_work_exported")
        return {
            "valid": True,
            "state": checkpoint.state.value,
            "run_id": checkpoint.run_id,
            "work_id": work_item.work_id,
            "package_id": checkpoint.package_id,
            "package_snapshot_hash": checkpoint.package_snapshot_hash,
            "files": len(result.snapshot.files),
            "graph_nodes": len(result.graph.nodes),
            "graph_edges": len(result.graph.edges),
            "diagnostics": len(result.graph.diagnostics),
            "designer_work_item": "designer-work-item.json",
        }

    def ingest_design(self, submission: EvalDesignerSubmission) -> dict[str, object]:
        checkpoint = self.checkpoint()
        if checkpoint.state is not EvalPlanState.PACKAGE_PARSED:
            raise ValueError(f"design submission is invalid in state {checkpoint.state.value}")
        work_item = self._model("designer-work-item.json", EvalDesignerWorkItem)
        if submission.work_id != work_item.work_id:
            raise ValueError("designer submission work_id mismatch")
        if submission.skill_id != checkpoint.package_id:
            raise ValueError("designer submission skill_id mismatch")
        if submission.package_snapshot_hash != checkpoint.package_snapshot_hash:
            raise ValueError("designer submission snapshot mismatch")
        if not submission.role_run.usage.nonempty:
            raise ValueError("Agent-native Eval Designer requires non-empty usage")
        accessed = {
            event.path
            for event in submission.package_access
            if event.kind in {PackageAccessKind.READ, PackageAccessKind.EXECUTED}
        }
        missing_reads = set(work_item.required_package_reads) - accessed
        if missing_reads:
            raise ValueError(
                "Eval Designer did not record required Package reads: "
                + ", ".join(sorted(missing_reads))
            )

        draft = EvalPlanDraft(
            plan_id=f"evalplan-{checkpoint.package_id}-r1",
            package_id=checkpoint.package_id,
            package_snapshot_hash=checkpoint.package_snapshot_hash,
            designer_submission_id=submission.submission_id,
            designer_work_id=submission.work_id,
            trigger_cases=submission.trigger_cases,
            functional_cases=submission.functional_cases,
            design_notes_zh=submission.design_notes_zh,
        )
        draft_hash = canonical_hash(draft)
        self.store.write_json("designer-submission.json", submission.model_dump(mode="json"))
        self.store.write_json("eval-plan-draft.json", draft.model_dump(mode="json"))
        draft_checkpoint = checkpoint.model_copy(
            update={
                "state": EvalPlanState.EVAL_DRAFT_GENERATED,
                "draft_hash": draft_hash,
                "updated_at": datetime.now(UTC),
            }
        )
        self._write_checkpoint(draft_checkpoint, "eval_draft_generated")

        provenance = self._model("source-provenance.json", SourceProvenance)
        brief = self._model("design-brief.json", EvalDesignBrief)
        checks = run_eval_plan_checks(self.repo_root, draft, provenance, brief)
        self.store.write_json("automatic-check-report.json", checks.model_dump(mode="json"))
        if not checks.valid:
            return {
                "valid": False,
                "state": draft_checkpoint.state.value,
                "draft_hash": draft_hash,
                "hard_failures": checks.metrics["hard_failures"],
            }

        checked = draft_checkpoint.model_copy(
            update={
                "state": EvalPlanState.AUTOMATIC_CHECKS_PASSED,
                "updated_at": datetime.now(UTC),
            }
        )
        self._write_checkpoint(checked, "automatic_checks_passed")
        graph = self._model("package/graph.json", PackageGraph)
        self.store.write_text(
            "review.html",
            render_eval_review(
                draft, draft_hash, checks, graph, EvalPlanState.AWAITING_REVIEW.value
            ),
            "text/html",
        )
        awaiting = checked.model_copy(
            update={
                "state": EvalPlanState.AWAITING_REVIEW,
                "updated_at": datetime.now(UTC),
            }
        )
        self._write_checkpoint(awaiting, "awaiting_review")
        return {
            "valid": True,
            "state": awaiting.state.value,
            "draft_hash": draft_hash,
            "trigger_cases": len(draft.trigger_cases),
            "functional_cases": len(draft.functional_cases),
            "automatic_checks": len(checks.checks),
            "review_html": "review.html",
        }

    def render_review(self, output: Path | None = None) -> dict[str, object]:
        checkpoint = self.checkpoint()
        if checkpoint.state not in {
            EvalPlanState.AUTOMATIC_CHECKS_PASSED,
            EvalPlanState.AWAITING_REVIEW,
            EvalPlanState.REVIEW_IMPORTED,
            EvalPlanState.EVAL_PLAN_FROZEN,
            EvalPlanState.EXECUTION_READY,
        }:
            raise ValueError(f"review cannot render in state {checkpoint.state.value}")
        draft = self._model("eval-plan-draft.json", EvalPlanDraft)
        checks = self._model("automatic-check-report.json", EvalPlanCheckReport)
        graph = self._model("package/graph.json", PackageGraph)
        existing_review = (
            self._model("review.json", EvalReviewSubmission)
            if (self.run_dir / "review.json").is_file()
            else None
        )
        content = render_eval_review(
            draft,
            checkpoint.draft_hash or canonical_hash(draft),
            checks,
            graph,
            checkpoint.state.value,
            existing_review,
        )
        target = output.resolve() if output is not None else self.run_dir / "review.html"
        if target == self.run_dir / "review.html":
            ref = self.store.write_text("review.html", content, "text/html")
            output_value = ref.path
        else:
            if not target.is_relative_to(self.repo_root):
                raise ValueError("review output must remain inside the repository")
            from gepase.store.artifacts import atomic_write

            atomic_write(target, content.encode())
            output_value = target.relative_to(self.repo_root).as_posix()
        return {"valid": True, "state": checkpoint.state.value, "output": output_value}

    def import_review(self, review: EvalReviewSubmission) -> dict[str, object]:
        checkpoint = self.checkpoint()
        if checkpoint.state is not EvalPlanState.AWAITING_REVIEW:
            raise ValueError(f"review import is invalid in state {checkpoint.state.value}")
        draft = self._model("eval-plan-draft.json", EvalPlanDraft)
        if review.plan_id != draft.plan_id or review.draft_hash != checkpoint.draft_hash:
            raise ValueError("review does not target the current immutable draft")
        source_by_id: dict[str, TriggerEvalCase | FunctionalEvalCase] = {
            case.case_id: case for case in (*draft.trigger_cases, *draft.functional_cases)
        }
        decision_ids = [item.case_id for item in review.decisions]
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("review contains duplicate case decisions")
        if set(decision_ids) != set(source_by_id):
            missing = sorted(set(source_by_id) - set(decision_ids))
            extra = sorted(set(decision_ids) - set(source_by_id))
            raise ValueError(f"review decision set mismatch: missing={missing}, extra={extra}")

        accepted_trigger: list[TriggerEvalCase] = []
        accepted_functional: list[FunctionalEvalCase] = []
        regeneration = []
        for decision in review.decisions:
            source = source_by_id[decision.case_id]
            expected_type = source.case_type
            if decision.case_type != expected_type:
                raise ValueError(f"review case_type mismatch for {decision.case_id}")
            if decision.decision is ReviewDecisionKind.REQUEST_REGENERATION:
                regeneration.append(decision.case_id)
                continue
            if decision.decision is ReviewDecisionKind.REJECT:
                continue
            selected: TriggerEvalCase | FunctionalEvalCase = source
            if decision.decision is ReviewDecisionKind.EDIT:
                assert decision.edited_case is not None
                selected = (
                    TriggerEvalCase.model_validate(decision.edited_case)
                    if expected_type == "trigger"
                    else FunctionalEvalCase.model_validate(decision.edited_case)
                )
                if selected.case_id != decision.case_id:
                    raise ValueError("review edits cannot rename case_id")
            if isinstance(selected, TriggerEvalCase):
                accepted_trigger.append(selected)
            else:
                accepted_functional.append(selected)

        self.store.write_json("review.json", review.model_dump(mode="json"))
        self.store.append_event(
            "review-ledger.jsonl",
            {
                "review_id": review.review_id,
                "draft_hash": review.draft_hash,
                "reviewer_id": review.reviewer_id,
                "reviewer_kind": review.reviewer_kind,
                "decisions": [item.model_dump(mode="json") for item in review.decisions],
                "imported_at": datetime.now(UTC).isoformat(),
            },
        )
        self.store.index_existing("review-ledger.jsonl", "application/x-ndjson")
        if regeneration:
            return {
                "valid": False,
                "state": checkpoint.state.value,
                "regeneration_requested": regeneration,
                "detail": "a new isolated Eval Designer attempt is required",
            }

        reviewed_draft = draft.model_copy(
            update={
                "trigger_cases": tuple(accepted_trigger),
                "functional_cases": tuple(accepted_functional),
            }
        )
        provenance = self._model("source-provenance.json", SourceProvenance)
        brief = self._model("design-brief.json", EvalDesignBrief)
        checks = run_eval_plan_checks(self.repo_root, reviewed_draft, provenance, brief)
        self.store.write_json("post-review-check-report.json", checks.model_dump(mode="json"))
        if not checks.valid:
            return {
                "valid": False,
                "state": checkpoint.state.value,
                "hard_failures": checks.metrics["hard_failures"],
                "detail": "reviewed plan failed deterministic checks",
            }

        imported = checkpoint.model_copy(
            update={
                "state": EvalPlanState.REVIEW_IMPORTED,
                "review_id": review.review_id,
                "updated_at": datetime.now(UTC),
            }
        )
        self._write_checkpoint(imported, "review_imported")
        review_hash = canonical_hash(review)
        frozen_at = datetime.now(UTC)
        plan_payload = {
            "plan_id": draft.plan_id,
            "revision": draft.revision,
            "package_id": draft.package_id,
            "package_snapshot_hash": draft.package_snapshot_hash,
            "source_commit": provenance.source_commit,
            "draft_hash": checkpoint.draft_hash,
            "review_id": review.review_id,
            "review_hash": review_hash,
            "trigger_cases": [case.model_dump(mode="json") for case in accepted_trigger],
            "functional_cases": [case.model_dump(mode="json") for case in accepted_functional],
            "frozen_at": frozen_at.isoformat(),
        }
        frozen = FrozenEvalPlan(
            **plan_payload,
            plan_hash=canonical_hash(plan_payload),
        )
        self.store.write_json("frozen-eval-plan.json", frozen.model_dump(mode="json"))
        frozen_checkpoint = imported.model_copy(
            update={
                "state": EvalPlanState.EVAL_PLAN_FROZEN,
                "frozen_plan_hash": frozen.plan_hash,
                "updated_at": datetime.now(UTC),
            }
        )
        self._write_checkpoint(frozen_checkpoint, "eval_plan_frozen")
        return {
            "valid": True,
            "state": frozen_checkpoint.state.value,
            "plan_hash": frozen.plan_hash,
            "review_id": review.review_id,
            "accepted_trigger_cases": len(accepted_trigger),
            "accepted_functional_cases": len(accepted_functional),
            "unresolved_review_decisions": 0,
        }

    def resume(self) -> dict[str, object]:
        checkpoint = self.checkpoint()
        if checkpoint.state is EvalPlanState.EXECUTION_READY:
            return {"resumed": False, "state": checkpoint.state.value, "already_ready": True}
        if checkpoint.state is not EvalPlanState.EVAL_PLAN_FROZEN:
            return {
                "resumed": False,
                "state": checkpoint.state.value,
                "detail": "run is not resumable until the EvalPlan is frozen",
            }
        ready = checkpoint.model_copy(
            update={
                "state": EvalPlanState.EXECUTION_READY,
                "updated_at": datetime.now(UTC),
            }
        )
        self._write_checkpoint(ready, "execution_ready")
        return {
            "resumed": True,
            "state": ready.state.value,
            "run_id": ready.run_id,
            "plan_hash": ready.frozen_plan_hash,
        }

    def status(self) -> dict[str, object]:
        checkpoint = self.checkpoint()
        review_decisions = 0
        if (self.run_dir / "review.json").is_file():
            review = self._model("review.json", EvalReviewSubmission)
            review_decisions = len(review.decisions)
        verification = self.store.verify()
        return {
            "run_id": checkpoint.run_id,
            "state": checkpoint.state.value,
            "package_id": checkpoint.package_id,
            "package_snapshot_hash": checkpoint.package_snapshot_hash,
            "draft_hash": checkpoint.draft_hash,
            "review_id": checkpoint.review_id,
            "review_decisions": review_decisions,
            "frozen_plan_hash": checkpoint.frozen_plan_hash,
            "artifact_verification": verification.as_dict(),
        }


def has_onboarding_checkpoint(run_dir: Path) -> bool:
    return (run_dir.resolve() / "checkpoint.json").is_file()
