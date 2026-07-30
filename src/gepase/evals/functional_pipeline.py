"""Core-owned R3 coordination for isolated functional evaluation roles.

The coordinator extends the authoritative Eval ledger with typed, durable role
artifacts.  It is deliberately not an Agent Runtime and does not own a second
evaluation, candidate, or scoring system.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

from gepase.evals.eval_plan import FrozenEvalPlan, FunctionalEvalCase
from gepase.evals.evidence import EvaluationRecord, UsageRecord
from gepase.evals.functional import (
    AnalysisNodeHint,
    AnalyzerEvidenceSummary,
    AnalyzerSubmission,
    AnalyzerWorkItem,
    BlindArtifact,
    ComparatorReconciliation,
    ComparatorSide,
    ComparatorSubmission,
    ComparatorWorkItem,
    DeterministicGradingBundle,
    FunctionalPairSummary,
    FunctionalRole,
    FunctionalRunSummary,
    FunctionalScoringPolicy,
    IndependentGraderSubmission,
    IndependentGraderWorkItem,
    IsolationAudit,
    PackageAccessAuditItem,
    ReliabilitySummary,
    RoleAttemptKind,
    RoleAttemptTerminalization,
    clamp,
    stable_role_id,
)
from gepase.evals.ledger import EvalLedger
from gepase.evals.scores import TaskScoreVector
from gepase.evals.work_items import (
    EvalWorkItem,
    PackageAccessKind,
    Variant,
    executor_view,
)
from gepase.optimizer.session_runtime import (
    ActiveSessionRuntime,
    HostAttemptAccounting,
    HostAttemptReason,
    ReservationSettlement,
    RuntimeBudgetBinding,
)
from gepase.package.ir import NodeKind, PackageGraph
from gepase.run_lifecycle import RunLifecycle, RunLifecycleRecord, RunLifecycleStatus
from gepase.store.artifacts import ArtifactStore, sha256_bytes

ModelT = TypeVar("ModelT", bound=BaseModel)
ComparatorOutcome = Literal["win", "loss", "tie"]


class RoleEvidenceIncompleteError(ValueError):
    """A required non-Executor role exhausted its bounded attempts."""

    def __init__(self, terminalization: RoleAttemptTerminalization) -> None:
        self.terminalization = terminalization
        super().__init__(
            f"{terminalization.role.value} evidence is incomplete for {terminalization.work_id}"
        )


def _weighted_score(case: FunctionalEvalCase, scores: dict[str, float]) -> float:
    return sum(scores[item.criterion_id] * item.weight for item in case.rubric)


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key) for key in value} | {
            nested for item in value.values() for nested in _keys(item)
        }
    if isinstance(value, list):
        return {nested for item in value for nested in _keys(item)}
    return set()


class FunctionalEvalCoordinator:
    """Prepare, validate, and aggregate the R3 role artifacts for one run."""

    def __init__(
        self,
        project_root: Path,
        run_dir: Path,
        ledger: EvalLedger,
        store: ArtifactStore,
    ) -> None:
        self.project_root = project_root.resolve()
        self.run_dir = run_dir.resolve()
        if not self.run_dir.is_relative_to(self.project_root):
            raise ValueError("functional run directory must remain inside the project")
        self.ledger = ledger
        self.store = store
        self.run_ref = self.run_dir.relative_to(self.project_root).as_posix()
        metadata = self._read_json(self.run_dir / "run-metadata.json")
        if metadata.get("mode") != "frozen-functional":
            raise ValueError("run is not a frozen Functional evaluation")
        self.metadata = metadata
        self.frozen = self._read_project_model(str(metadata["frozen_plan_ref"]), FrozenEvalPlan)
        self.policy = self._read_project_model(
            str(metadata["scoring_policy_ref"]), FunctionalScoringPolicy
        )
        self.cases = {case.case_id: case for case in self.frozen.functional_cases}
        self.graph = self._read_project_model(str(metadata["package_graph_ref"]), PackageGraph)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"expected a JSON object: {path}")
        return raw

    def _read_project_model(self, reference: str, model: type[ModelT]) -> ModelT:
        path = (self.project_root / reference).resolve(strict=True)
        if not path.is_relative_to(self.project_root):
            raise ValueError("project model reference escapes the repository")
        return model.model_validate_json(path.read_text(encoding="utf-8"))

    def _run_model(self, relative: str, model: type[ModelT]) -> ModelT:
        return model.model_validate_json((self.run_dir / relative).read_text(encoding="utf-8"))

    def _project_ref(self, relative: str) -> str:
        return f"{self.run_ref}/{relative}"

    def _items(self) -> list[EvalWorkItem]:
        return [item for item in self.ledger.work_items() if item.frozen_plan_hash]

    def _case_items(self, task_id: str) -> dict[Variant, EvalWorkItem]:
        values: dict[Variant, EvalWorkItem] = {
            item.variant: item for item in self._items() if item.task_id == task_id
        }
        if set(values) != {"no-skill", "original"}:
            raise ValueError(f"incomplete paired work for {task_id}")
        return values

    def _source_record(self, item: EvalWorkItem) -> EvaluationRecord:
        record = self.ledger.record_for_work(item.work_id)
        if record is None or record.failure_kind is not None:
            raise ValueError(f"successful E2 record is unavailable: {item.work_id}")
        return record

    def _deterministic(self, item: EvalWorkItem) -> DeterministicGradingBundle:
        return self._run_model(f"deterministic/{item.work_id}.json", DeterministicGradingBundle)

    def _context_ids(self) -> set[str]:
        values = {
            submission.context_id or submission.host_task_id
            for submission in self.ledger.submissions()
        }
        role_models: tuple[tuple[str, type[BaseModel]], ...] = (
            ("grader-submissions", IndependentGraderSubmission),
            ("comparator-submissions", ComparatorSubmission),
            ("analyzer-submissions", AnalyzerSubmission),
        )
        for directory, model in role_models:
            for path in (self.run_dir / directory).glob("*.json"):
                submission: Any = model.model_validate_json(path.read_text(encoding="utf-8"))
                role_run = submission.role_run
                values.add(str(role_run.context_id))
        return values

    def _assert_new_context(self, context_id: str) -> None:
        if context_id in self._context_ids():
            raise ValueError(f"role context was reused: {context_id}")

    def _budget_runtime(self) -> ActiveSessionRuntime | None:
        path = self.run_dir / "runtime-budget-binding.json"
        if not path.is_file():
            return None
        binding = RuntimeBudgetBinding.model_validate_json(path.read_text(encoding="utf-8"))
        owner = (self.project_root / binding.owner_run_ref).resolve(strict=True)
        if not owner.is_relative_to(self.project_root):
            raise ValueError("runtime budget binding escapes the project")
        return ActiveSessionRuntime(
            owner,
            run_id=binding.owner_run_id,
            config_hash=binding.config_hash,
            policy=binding.policy,
        )

    def _sync_runtime_lifecycle(self) -> None:
        record_path = self.run_dir / RunLifecycle.RECORD_NAME
        if not record_path.is_file():
            return
        record = RunLifecycleRecord.model_validate_json(record_path.read_text(encoding="utf-8"))
        if record.config_hash is None:
            raise ValueError("strict Functional run lifecycle lacks config hash")
        self.store.write_json("ledger-snapshot.json", self.ledger.status())
        self.ledger.checkpoint()
        runtime = self._budget_runtime()
        status = RunLifecycleStatus.ACTIVE
        if runtime is not None and runtime.state().status.value in {
            "paused",
            "awaiting_continuation",
            "stopped",
        }:
            status = RunLifecycleStatus.PAUSED
        critical = ["run-metadata.json", "ledger.sqlite3", "ledger-snapshot.json"]
        for relative in (
            "resolved-reference-config.json",
            "runtime-budget-binding.json",
            "runtime-session.json",
        ):
            if (self.run_dir / relative).is_file():
                critical.append(relative)
        RunLifecycle(
            self.run_dir,
            run_id=record.run_id,
            owner="eval",
            expected_config_hash=record.config_hash,
        ).checkpoint(
            config_hash=record.config_hash,
            status=status,
            critical_files=tuple(critical),
        )

    @staticmethod
    def _role_paths(role: FunctionalRole) -> tuple[str, str, type[BaseModel]]:
        if role is FunctionalRole.INDEPENDENT_GRADER:
            return "grader-work-items", "grader-submissions", IndependentGraderWorkItem
        if role is FunctionalRole.COMPARATOR:
            return "comparator-work-items", "comparator-submissions", ComparatorWorkItem
        return "analyzer-work-items", "analyzer-submissions", AnalyzerWorkItem

    def _role_terminalization(
        self,
        role: FunctionalRole,
        work_id: str,
    ) -> RoleAttemptTerminalization | None:
        path = self.run_dir / f"role-terminalizations/{role.value}/{work_id}.json"
        if not path.is_file():
            return None
        terminal = RoleAttemptTerminalization.model_validate_json(path.read_text(encoding="utf-8"))
        if terminal.role is not role or terminal.work_id != work_id:
            raise ValueError("role terminalization path and identity disagree")
        runtime = self._budget_runtime()
        if runtime is None:
            raise ValueError("role terminalization requires the authoritative runtime")
        settlement_path = runtime.run_dir / "reservation-settlements" / f"{work_id}.json"
        if not settlement_path.is_file():
            raise ValueError("role terminalization lacks its reservation settlement")
        settlement = ReservationSettlement.model_validate_json(
            settlement_path.read_text(encoding="utf-8")
        )
        attempt_ids = tuple(item.host_attempt_accounting_id for item in terminal.attempts)
        if (
            settlement.role != role.value
            or settlement.accounting_mode != "preaccounted_host_attempts"
            or settlement.host_attempt_accounting_ids != attempt_ids
        ):
            raise ValueError("role terminalization settlement binding is inconsistent")
        return terminal

    def _role_terminalization_for_task(
        self,
        role: FunctionalRole,
        task_id: str,
    ) -> RoleAttemptTerminalization | None:
        state_dir = {
            FunctionalRole.INDEPENDENT_GRADER: "grader",
            FunctionalRole.COMPARATOR: "comparator",
            FunctionalRole.ANALYZER: "analyzer",
        }[role]
        work_key = {
            FunctionalRole.INDEPENDENT_GRADER: "grader_work_id",
            FunctionalRole.COMPARATOR: "comparator_work_id",
            FunctionalRole.ANALYZER: "analyzer_work_id",
        }[role]
        for path in sorted((self.run_dir / f"role-state/{state_dir}").glob("*.json")):
            mapping = self._read_json(path)
            mapped_task_id = mapping.get("task_id")
            if mapped_task_id is None and role is FunctionalRole.INDEPENDENT_GRADER:
                source_work_id = mapping.get("source_work_id")
                source_tasks = {
                    item.task_id for item in self._items() if item.work_id == source_work_id
                }
                if len(source_tasks) > 1:
                    raise ValueError("grader source work maps to multiple tasks")
                mapped_task_id = next(iter(source_tasks), None)
            if mapped_task_id != task_id:
                continue
            terminal = self._role_terminalization(role, str(mapping[work_key]))
            if terminal is not None:
                return terminal
        return None

    def terminalize_role_attempts(
        self,
        terminalization: RoleAttemptTerminalization,
    ) -> dict[str, Any]:
        """Settle an exhausted Grader/Comparator/Analyzer without fake evidence."""

        role = terminalization.role
        work_dir, submission_dir, work_model = self._role_paths(role)
        work: Any = self._run_model(
            f"{work_dir}/{terminalization.work_id}.json",
            work_model,
        )
        if str(work.task_id) != terminalization.task_id:
            raise ValueError("role terminalization belongs to another task")
        if str(work.role) != role.value:
            raise ValueError("role terminalization belongs to another role")
        if (self.run_dir / f"{submission_dir}/{terminalization.work_id}.json").is_file():
            raise ValueError("a successful role submission cannot be terminalized")
        runtime = self._budget_runtime()
        if runtime is None:
            raise ValueError("role terminalization requires an ActiveSessionRuntime binding")
        if terminalization.run_id != runtime.run_id:
            raise ValueError("role terminalization belongs to another owner run")
        expected_id = stable_role_id(
            "role-terminal",
            terminalization.model_dump(mode="json", exclude={"terminalization_id"}),
        )
        if terminalization.terminalization_id != expected_id:
            raise ValueError("role terminalization ID is not content-addressed")

        successful_contexts = self._context_ids()
        terminal_contexts = {
            attempt.context_id
            for path in (self.run_dir / "role-terminalizations").glob("*/*.json")
            for record in [
                RoleAttemptTerminalization.model_validate_json(path.read_text(encoding="utf-8"))
            ]
            if record.work_id != terminalization.work_id
            for attempt in record.attempts
        }
        accounting_ids: list[str] = []
        for attempt in terminalization.attempts:
            host_path = (
                runtime.run_dir
                / "host-attempt-accounting"
                / f"{attempt.host_attempt_accounting_id}.json"
            )
            if not host_path.is_file():
                raise ValueError("role failure HostAttempt is missing")
            raw = host_path.read_bytes()
            if sha256_bytes(raw) != attempt.evidence_sha256:
                raise ValueError("role failure HostAttempt evidence hash differs")
            accounting = HostAttemptAccounting.model_validate_json(raw)
            expected_reason = (
                HostAttemptReason.EXECUTION_REPAIR
                if attempt.attempt_kind is RoleAttemptKind.REPAIR
                else None
            )
            if (
                accounting.accounting_id != attempt.host_attempt_accounting_id
                or accounting.work_id != terminalization.work_id
                or accounting.role != role.value
                or accounting.host_task_id != attempt.host_task_id
                or accounting.context_id != attempt.context_id
                or (
                    expected_reason is None
                    and accounting.reason is HostAttemptReason.EXECUTION_REPAIR
                )
                or (expected_reason is not None and accounting.reason is not expected_reason)
            ):
                raise ValueError("role failure HostAttempt binding is inconsistent")
            if not set(attempt.source_refs) <= set(accounting.evidence_refs):
                raise ValueError("role failure cites evidence outside its HostAttempt")
            if attempt.context_id in successful_contexts or attempt.context_id in terminal_contexts:
                raise ValueError("role terminalization reused another role context")
            accounting_ids.append(accounting.accounting_id)

        runtime.validate_preaccounted_failure(
            work_id=terminalization.work_id,
            host_attempt_accounting_ids=tuple(accounting_ids),
        )
        relative = f"role-terminalizations/{role.value}/{terminalization.work_id}.json"
        duplicate = (self.run_dir / relative).is_file()
        self.store.write_json_append_only(relative, terminalization.model_dump(mode="json"))
        runtime.settle_preaccounted_failure(
            work_id=terminalization.work_id,
            host_attempt_accounting_ids=tuple(accounting_ids),
            now=terminalization.terminalized_at,
        )
        self._sync_runtime_lifecycle()
        stored = self._role_terminalization(role, terminalization.work_id)
        if stored != terminalization:
            raise ValueError("role terminalization did not round-trip")
        return {
            "duplicate": duplicate,
            "terminalization_id": terminalization.terminalization_id,
            "work_id": terminalization.work_id,
            "role": role.value,
            "disposition": terminalization.disposition,
            "agent_usage_added": False,
        }

    def _reserve_role_batch(self, role: str, work_ids: list[str]) -> None:
        runtime = self._budget_runtime()
        if runtime is None or not work_ids:
            return
        batch_hash = stable_role_id("budget-batch", {"role": role, "work": sorted(work_ids)})
        runtime.reserve(
            batch_id=f"{role}:{batch_hash}",
            role=role,
            work_ids=tuple(sorted(work_ids)),
        )
        self._sync_runtime_lifecycle()

    def _settle_role_work(
        self,
        work_id: str,
        usage: UsageRecord,
        *,
        repair_attempt: bool = False,
    ) -> None:
        runtime = self._budget_runtime()
        if runtime is None:
            return
        runtime.settle(
            work_id=work_id,
            actual_tokens=(usage.input_tokens or 0) + (usage.output_tokens or 0),
            actual_duration_ms=usage.duration_ms,
            repairs=1 if repair_attempt else 0,
        )
        self._sync_runtime_lifecycle()

    def _blind_artifact(
        self,
        item: EvalWorkItem,
        source: EvaluationRecord,
        deterministic: DeterministicGradingBundle,
    ) -> BlindArtifact:
        case = self.cases[item.task_id]
        matches = [
            artifact
            for artifact in source.artifacts
            if artifact.path == case.requested_output.filename
            and artifact.media_type == case.requested_output.media_type
        ]
        if len(matches) != 1 or source.artifact_root is None:
            raise ValueError(f"task-native artifact is unavailable: {item.work_id}")
        blind_id = stable_role_id(
            "blind",
            {
                "task": item.task_id,
                "artifact_sha256": matches[0].sha256,
                "source_record_id": source.record_id,
                "plan": self.frozen.plan_hash,
            },
        )
        inspection = deterministic.inspection
        safe_inspection = {
            "schema_version": "1.0.0",
            "blind_id": blind_id,
            "width": inspection.width,
            "height": inspection.height,
            "frame_count": inspection.frame_count,
            "unique_frame_count": inspection.unique_frame_count,
            "total_duration_ms": inspection.total_duration_ms,
            "frame_durations_ms": list(inspection.frame_durations_ms),
            "effective_fps": inspection.effective_fps,
            "loop_count": inspection.loop_count,
            "file_size_bytes": inspection.file_size_bytes,
            "mean_adjacent_pixel_delta": inspection.mean_adjacent_pixel_delta,
            "first_last_pixel_delta": inspection.first_last_pixel_delta,
        }
        relative = f"blind-inspections/{blind_id}.json"
        self.store.write_json(relative, safe_inspection)
        return BlindArtifact(
            blind_id=blind_id,
            artifact_root=source.artifact_root,
            artifact=matches[0],
            contact_sheet_ref=inspection.contact_sheet_ref,
            inspection_ref=self._project_ref(relative),
        )

    def prepare_graders(self) -> dict[str, Any]:
        rows: list[tuple[IndependentGraderWorkItem, dict[str, Any]]] = []
        for item in self._items():
            source = self._source_record(item)
            deterministic = self._deterministic(item)
            blind = self._blind_artifact(item, source, deterministic)
            case = self.cases[item.task_id]
            grader_id = stable_role_id(
                "grader-work",
                {"task": item.task_id, "blind": blind.blind_id, "plan": self.frozen.plan_hash},
            )
            work = IndependentGraderWorkItem(
                grader_work_id=grader_id,
                task_id=item.task_id,
                task_prompt=case.prompt,
                expected_output_zh=case.expected_output_zh,
                rubric=case.rubric,
                blind_artifact=blind,
                submission_schema_ref="schemas/independent_grader_submission.schema.json",
            )
            mapping = {
                "schema_version": "1.0.0",
                "grader_work_id": grader_id,
                "task_id": item.task_id,
                "source_work_id": item.work_id,
                "variant": item.variant,
                "blind_id": blind.blind_id,
            }
            rows.append((work, mapping))
        work_ids = [work.grader_work_id for work, _mapping in rows]
        self._reserve_role_batch("independent_grader", work_ids)
        for work, mapping in rows:
            self.store.write_json(
                f"grader-work-items/{work.grader_work_id}.json",
                work.model_dump(mode="json"),
            )
            self.store.write_json(f"role-state/grader/{work.grader_work_id}.json", mapping)
        return {"prepared": len(work_ids), "grader_work_ids": sorted(work_ids)}

    def ingest_grader(self, submission: IndependentGraderSubmission) -> dict[str, Any]:
        relative = f"grader-submissions/{submission.grader_work_id}.json"
        existing = self.run_dir / relative
        if existing.is_file():
            stored = IndependentGraderSubmission.model_validate_json(
                existing.read_text(encoding="utf-8")
            )
            if stored != submission:
                raise ValueError("grader work already has a different submission")
            return {"duplicate": True, "submission_id": submission.submission_id}
        work = self._run_model(
            f"grader-work-items/{submission.grader_work_id}.json",
            IndependentGraderWorkItem,
        )
        self._assert_new_context(submission.role_run.context_id)
        case = self.cases[work.task_id]
        expected_ids = {item.criterion_id for item in case.rubric}
        grades = {item.criterion_id: item for item in submission.criterion_grades}
        if len(grades) != len(submission.criterion_grades) or set(grades) != expected_ids:
            raise ValueError("grader criterion IDs differ from the frozen rubric")
        allowed_refs = {
            f"{work.blind_artifact.artifact_root}/{work.blind_artifact.artifact.path}",
            work.blind_artifact.contact_sheet_ref,
            work.blind_artifact.inspection_ref,
        }
        for grade in grades.values():
            if not set(grade.evidence_refs).issubset(allowed_refs):
                raise ValueError("grader cited evidence outside its blind input")
        recomputed = _weighted_score(case, {key: value.score for key, value in grades.items()})
        if abs(recomputed - submission.overall_score) > 1e-6:
            raise ValueError("grader overall_score is not reproducible from rubric grades")
        self.store.write_json(relative, submission.model_dump(mode="json"))
        self._settle_role_work(
            submission.grader_work_id,
            submission.role_run.usage,
            repair_attempt=submission.role_run.repair_attempt,
        )
        return {"duplicate": False, "submission_id": submission.submission_id}

    def _grader_for_source(
        self, source_work_id: str
    ) -> tuple[IndependentGraderSubmission, str, BlindArtifact]:
        for path in (self.run_dir / "role-state/grader").glob("*.json"):
            mapping = self._read_json(path)
            if mapping.get("source_work_id") != source_work_id:
                continue
            grader_id = str(mapping["grader_work_id"])
            work = self._run_model(f"grader-work-items/{grader_id}.json", IndependentGraderWorkItem)
            terminal = self._role_terminalization(
                FunctionalRole.INDEPENDENT_GRADER,
                grader_id,
            )
            if terminal is not None:
                raise RoleEvidenceIncompleteError(terminal)
            submission = self._run_model(
                f"grader-submissions/{grader_id}.json", IndependentGraderSubmission
            )
            return submission, grader_id, work.blind_artifact
        raise ValueError(f"grader mapping is unavailable: {source_work_id}")

    def prepare_comparators(self) -> dict[str, Any]:
        rows: list[tuple[ComparatorWorkItem, dict[str, Any]]] = []
        incomplete_task_ids: list[str] = []
        for task_id in self.policy.comparator_case_ids:
            case = self.cases.get(task_id)
            if case is None:
                raise ValueError(f"unknown comparator case: {task_id}")
            items = self._case_items(task_id)
            try:
                blind = {
                    variant: self._grader_for_source(item.work_id)[2]
                    for variant, item in items.items()
                }
            except RoleEvidenceIncompleteError:
                incomplete_task_ids.append(task_id)
                continue
            for order in ("AB", "BA"):
                left_variant: Literal["no-skill", "original"] = (
                    "no-skill" if order == "AB" else "original"
                )
                right_variant: Literal["no-skill", "original"] = (
                    "original" if order == "AB" else "no-skill"
                )
                comparator_id = stable_role_id(
                    "comparator-work",
                    {"task": task_id, "order": order, "plan": self.frozen.plan_hash},
                )
                work = ComparatorWorkItem(
                    comparator_work_id=comparator_id,
                    task_id=task_id,
                    task_prompt=case.prompt,
                    expected_output_zh=case.expected_output_zh,
                    rubric=case.rubric,
                    left=ComparatorSide(side_id="left", blind_artifact=blind[left_variant]),
                    right=ComparatorSide(side_id="right", blind_artifact=blind[right_variant]),
                    order_label=order,
                    submission_schema_ref="schemas/comparator_submission.schema.json",
                )
                mapping = {
                    "schema_version": "1.0.0",
                    "comparator_work_id": comparator_id,
                    "task_id": task_id,
                    "order": order,
                    "left_variant": left_variant,
                    "right_variant": right_variant,
                }
                rows.append((work, mapping))
        work_ids = [work.comparator_work_id for work, _mapping in rows]
        self._reserve_role_batch("comparator", work_ids)
        for work, mapping in rows:
            self.store.write_json(
                f"comparator-work-items/{work.comparator_work_id}.json",
                work.model_dump(mode="json"),
            )
            self.store.write_json(f"role-state/comparator/{work.comparator_work_id}.json", mapping)
        return {
            "prepared": len(work_ids),
            "comparator_work_ids": sorted(work_ids),
            "evidence_incomplete_task_ids": sorted(incomplete_task_ids),
        }

    def ingest_comparator(self, submission: ComparatorSubmission) -> dict[str, Any]:
        relative = f"comparator-submissions/{submission.comparator_work_id}.json"
        existing = self.run_dir / relative
        if existing.is_file():
            stored = ComparatorSubmission.model_validate_json(existing.read_text(encoding="utf-8"))
            if stored != submission:
                raise ValueError("comparator work already has a different submission")
            return {"duplicate": True, "submission_id": submission.submission_id}
        work = self._run_model(
            f"comparator-work-items/{submission.comparator_work_id}.json",
            ComparatorWorkItem,
        )
        self._assert_new_context(submission.role_run.context_id)
        expected_ids = {item.criterion_id for item in work.rubric}
        criteria = {item.criterion_id: item for item in submission.criteria}
        if len(criteria) != len(submission.criteria) or set(criteria) != expected_ids:
            raise ValueError("comparator criterion IDs differ from the frozen rubric")
        self.store.write_json(relative, submission.model_dump(mode="json"))
        self._settle_role_work(
            submission.comparator_work_id,
            submission.role_run.usage,
            repair_attempt=submission.role_run.repair_attempt,
        )
        return {"duplicate": False, "submission_id": submission.submission_id}

    def reconcile_comparators(self) -> dict[str, Any]:
        task_ids: list[str] = []
        incomplete_task_ids: list[str] = []
        for task_id in self.policy.comparator_case_ids:
            if self._role_terminalization_for_task(
                FunctionalRole.INDEPENDENT_GRADER,
                task_id,
            ):
                incomplete_task_ids.append(task_id)
                continue
            outcomes: dict[str, tuple[ComparatorOutcome, str]] = {}
            refs: dict[str, str] = {}
            pair_id = self._case_items(task_id)["original"].pair_id
            role_incomplete = False
            for mapping_path in (self.run_dir / "role-state/comparator").glob("*.json"):
                mapping = self._read_json(mapping_path)
                if mapping.get("task_id") != task_id:
                    continue
                comparator_id = str(mapping["comparator_work_id"])
                if self._role_terminalization(FunctionalRole.COMPARATOR, comparator_id):
                    role_incomplete = True
                    continue
                submission = self._run_model(
                    f"comparator-submissions/{comparator_id}.json", ComparatorSubmission
                )
                winner = submission.winner
                if winner == "tie":
                    original_outcome: ComparatorOutcome = "tie"
                else:
                    winner_variant = mapping[f"{winner}_variant"]
                    original_outcome = "win" if winner_variant == "original" else "loss"
                order = str(mapping["order"])
                outcomes[order] = (original_outcome, comparator_id)
                refs[order] = self._project_ref(f"comparator-submissions/{comparator_id}.json")
            if role_incomplete:
                incomplete_task_ids.append(task_id)
                continue
            if set(outcomes) != {"AB", "BA"}:
                raise ValueError(f"AB/BA comparator submissions are incomplete: {task_id}")
            numeric = {"win": 1.0, "loss": -1.0, "tie": 0.0}
            ab_outcome = outcomes["AB"][0]
            ba_outcome = outcomes["BA"][0]
            reconciliation = ComparatorReconciliation(
                task_id=task_id,
                pair_id=pair_id,
                ab_submission_ref=refs["AB"],
                ba_submission_ref=refs["BA"],
                ab_original_outcome=ab_outcome,
                ba_original_outcome=ba_outcome,
                consistent=ab_outcome == ba_outcome,
                original_margin=(numeric[ab_outcome] + numeric[ba_outcome]) / 2,
            )
            self.store.write_json(
                f"comparator-reconciliation/{task_id}.json",
                reconciliation.model_dump(mode="json"),
            )
            task_ids.append(task_id)
        result = {
            "reconciled": len(task_ids),
            "task_ids": sorted(task_ids),
            "evidence_incomplete_task_ids": sorted(incomplete_task_ids),
        }
        self.store.write_json("role-evidence/comparator-reconciliation.json", result)
        return result

    def audit_package_access(self) -> dict[str, Any]:
        audit_items: list[PackageAccessAuditItem] = []
        for item in self._items():
            submission = self.ledger.submission_for_work(item.work_id)
            if submission is None:
                raise ValueError(f"execution submission is unavailable: {item.work_id}")
            available = tuple(sorted(set(item.package_node_map.values())))
            if submission.failure_kind is not None:
                audit_items.append(
                    PackageAccessAuditItem(
                        work_id=item.work_id,
                        variant=item.variant,
                        valid=True,
                        available_node_ids=available,
                        read_node_ids=(),
                        executed_node_ids=(),
                        bytes_loaded=0,
                        tokens_loaded=0,
                        unresolved_paths=(),
                        problems=(),
                    )
                )
                continue
            read_ids: list[str] = []
            executed_ids: list[str] = []
            unresolved: list[str] = []
            problems: list[str] = []
            sequences = [event.sequence for event in submission.package_access]
            if sequences != sorted(set(sequences)):
                problems.append("package access sequences are not unique and ordered")
            bytes_loaded = 0
            tokens_loaded = 0
            for event in submission.package_access:
                expected = item.package_node_map.get(event.path)
                if expected is None or event.node_id != expected:
                    unresolved.append(event.path)
                    continue
                bytes_loaded += event.bytes_loaded
                tokens_loaded += event.tokens_loaded
                if event.kind is PackageAccessKind.READ:
                    read_ids.append(expected)
                    if event.bytes_loaded == 0 or event.tokens_loaded == 0:
                        problems.append(f"read event lacks byte/token accounting: {event.path}")
                elif event.kind is PackageAccessKind.EXECUTED:
                    executed_ids.append(expected)
            if item.variant == "no-skill":
                if available or submission.package_access:
                    problems.append("no-skill execution exposed Package state")
            else:
                skill_node = item.package_node_map.get("SKILL.md")
                if skill_node not in read_ids:
                    problems.append("SKILL.md was not read")
                if not executed_ids:
                    problems.append("Package code was not executed")
            audit = PackageAccessAuditItem(
                work_id=item.work_id,
                variant=item.variant,
                valid=not unresolved and not problems,
                available_node_ids=available,
                read_node_ids=tuple(sorted(set(read_ids))),
                executed_node_ids=tuple(sorted(set(executed_ids))),
                bytes_loaded=bytes_loaded,
                tokens_loaded=tokens_loaded,
                unresolved_paths=tuple(sorted(set(unresolved))),
                problems=tuple(problems),
            )
            self.store.write_json(
                f"package-access/{item.work_id}.json", audit.model_dump(mode="json")
            )
            audit_items.append(audit)
        summary = {
            "schema_version": "1.0.0",
            "valid": all(item.valid for item in audit_items),
            "items": [item.model_dump(mode="json") for item in audit_items],
        }
        self.store.write_json("package-access-audit.json", summary)
        return summary

    def _node_hints(self, original: EvalWorkItem) -> tuple[AnalysisNodeHint, ...]:
        submission = self.ledger.submission_for_work(original.work_id)
        accessed = {
            event.node_id
            for event in (submission.package_access if submission else ())
            if event.node_id
        }
        adjacent = set(accessed)
        for edge in self.graph.edges:
            if edge.source in accessed:
                adjacent.add(edge.target)
            if edge.target in accessed:
                adjacent.add(edge.source)
        allowed_kinds = {
            NodeKind.FILE,
            NodeKind.SECTION,
            NodeKind.INSTRUCTION,
            NodeKind.PYTHON_MODULE,
            NodeKind.FUNCTION,
            NodeKind.CLASS,
            NodeKind.DEPENDENCY,
        }
        nodes = [node for node in self.graph.nodes if node.kind in allowed_kinds]
        nodes.sort(
            key=lambda node: (
                node.node_id not in accessed,
                node.node_id not in adjacent,
                node.path != "SKILL.md",
                node.path,
                node.locator,
            )
        )
        return tuple(
            AnalysisNodeHint(
                node_id=node.node_id,
                path=node.path,
                kind=node.kind.value,
                label=node.label,
            )
            for node in nodes[:120]
        )

    def prepare_analyzers(self) -> dict[str, Any]:
        access = self.audit_package_access()
        if not access["valid"]:
            raise ValueError("package access audit must pass before Analyzer dispatch")
        works: list[AnalyzerWorkItem] = []
        incomplete_task_ids: list[str] = []
        for task_id in sorted({item.task_id for item in self._items()}):
            case = self.cases[task_id]
            items = self._case_items(task_id)
            summaries: dict[str, AnalyzerEvidenceSummary] = {}
            try:
                for variant in ("no-skill", "original"):
                    item = items[variant]
                    source = self._source_record(item)
                    deterministic = self._deterministic(item)
                    grader, grader_id, _blind = self._grader_for_source(item.work_id)
                    summaries[variant] = AnalyzerEvidenceSummary(
                        variant=variant,
                        execution_record_ref=self._project_ref(f"records/{source.record_id}.json"),
                        deterministic_bundle_ref=self._project_ref(
                            f"deterministic/{item.work_id}.json"
                        ),
                        independent_grade_ref=self._project_ref(
                            f"grader-submissions/{grader_id}.json"
                        ),
                        task_correctness=deterministic.weighted_score,
                        output_quality=grader.overall_score,
                        failed_expectation_ids=tuple(
                            result.assertion_id
                            for result in deterministic.assertion_results
                            if not result.passed
                        ),
                        grader_feedback_zh=grader.feedback_zh,
                        failure_kind=source.failure_kind,
                        package_access_ref=(
                            self._project_ref(f"package-access/{item.work_id}.json")
                            if variant == "original"
                            else None
                        ),
                    )
            except RoleEvidenceIncompleteError:
                incomplete_task_ids.append(task_id)
                continue
            comparator_ref = None
            if task_id in self.policy.comparator_case_ids:
                if self._role_terminalization_for_task(FunctionalRole.COMPARATOR, task_id):
                    incomplete_task_ids.append(task_id)
                    continue
                comparator_ref = self._project_ref(f"comparator-reconciliation/{task_id}.json")
                self._run_model(
                    f"comparator-reconciliation/{task_id}.json",
                    ComparatorReconciliation,
                )
            analyzer_id = stable_role_id(
                "analyzer-work",
                {"task": task_id, "pair": items["original"].pair_id, "plan": self.frozen.plan_hash},
            )
            work = AnalyzerWorkItem(
                analyzer_work_id=analyzer_id,
                task_id=task_id,
                pair_id=items["original"].pair_id,
                task_prompt=case.prompt,
                baseline=summaries["no-skill"],
                original=summaries["original"],
                comparator_ref=comparator_ref,
                package_graph_ref=str(self.metadata["package_graph_ref"]),
                node_hints=self._node_hints(items["original"]),
                submission_schema_ref="schemas/analyzer_submission.schema.json",
            )
            works.append(work)
        work_ids = [work.analyzer_work_id for work in works]
        self._reserve_role_batch("analyzer", work_ids)
        for work in works:
            self.store.write_json(
                f"analyzer-work-items/{work.analyzer_work_id}.json",
                work.model_dump(mode="json"),
            )
            self.store.write_json(
                f"role-state/analyzer/{work.analyzer_work_id}.json",
                {
                    "schema_version": "1.0.0",
                    "analyzer_work_id": work.analyzer_work_id,
                    "task_id": work.task_id,
                    "pair_id": work.pair_id,
                },
            )
        return {
            "prepared": len(work_ids),
            "analyzer_work_ids": work_ids,
            "evidence_incomplete_task_ids": sorted(incomplete_task_ids),
        }

    def ingest_analyzer(self, submission: AnalyzerSubmission) -> dict[str, Any]:
        relative = f"analyzer-submissions/{submission.analyzer_work_id}.json"
        existing = self.run_dir / relative
        if existing.is_file():
            stored = AnalyzerSubmission.model_validate_json(existing.read_text(encoding="utf-8"))
            if stored != submission:
                raise ValueError("analyzer work already has a different submission")
            return {"duplicate": True, "submission_id": submission.submission_id}
        work = self._run_model(
            f"analyzer-work-items/{submission.analyzer_work_id}.json", AnalyzerWorkItem
        )
        if work.semantic_enrichment is not None or submission.semantic_relation_proposals:
            raise ValueError(
                "semantic hypothesis generation is retired; sealed GH-P1 evidence is read-only"
            )
        self._assert_new_context(submission.role_run.context_id)
        known_nodes = {node.node_id for node in self.graph.nodes}
        allowed_refs = {
            work.baseline.execution_record_ref,
            work.baseline.deterministic_bundle_ref,
            work.baseline.independent_grade_ref,
            work.original.execution_record_ref,
            work.original.deterministic_bundle_ref,
            work.original.independent_grade_ref,
            work.package_graph_ref,
        }
        if work.original.package_access_ref:
            allowed_refs.add(work.original.package_access_ref)
        if work.comparator_ref:
            allowed_refs.add(work.comparator_ref)
        for analysis in submission.analyses:
            if not set(analysis.evidence_refs).issubset(allowed_refs):
                raise ValueError("Analyzer cited evidence outside its typed work item")
            if not set(analysis.target_node_ids).issubset(known_nodes):
                raise ValueError("Analyzer target cannot be resolved in the Package Graph")
        imperfect = min(work.original.task_correctness, work.original.output_quality) < 1.0
        if imperfect and not submission.analyses:
            raise ValueError("imperfect original output requires at least one failure analysis")
        self.store.write_json(relative, submission.model_dump(mode="json"))
        self._settle_role_work(
            submission.analyzer_work_id,
            submission.role_run.usage,
            repair_attempt=submission.role_run.repair_attempt,
        )
        return {
            "duplicate": False,
            "submission_id": submission.submission_id,
        }

    def audit_isolation(self) -> IsolationAudit:
        executor_contexts = tuple(
            (submission.context_id or submission.host_task_id)
            for submission in self.ledger.submissions()
        )
        grader_submissions = [
            IndependentGraderSubmission.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted((self.run_dir / "grader-submissions").glob("*.json"))
        ]
        comparator_submissions = [
            ComparatorSubmission.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted((self.run_dir / "comparator-submissions").glob("*.json"))
        ]
        analyzer_submissions = [
            AnalyzerSubmission.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted((self.run_dir / "analyzer-submissions").glob("*.json"))
        ]
        terminalizations = [
            RoleAttemptTerminalization.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted((self.run_dir / "role-terminalizations").glob("*/*.json"))
        ]
        failed_contexts = {
            role: tuple(
                attempt.context_id
                for item in terminalizations
                if item.role is role
                for attempt in item.attempts
            )
            for role in FunctionalRole
        }
        grader_contexts = (
            tuple(item.role_run.context_id for item in grader_submissions)
            + failed_contexts[FunctionalRole.INDEPENDENT_GRADER]
        )
        comparator_contexts = (
            tuple(item.role_run.context_id for item in comparator_submissions)
            + failed_contexts[FunctionalRole.COMPARATOR]
        )
        analyzer_contexts = (
            tuple(item.role_run.context_id for item in analyzer_submissions)
            + failed_contexts[FunctionalRole.ANALYZER]
        )
        all_contexts = executor_contexts + grader_contexts + comparator_contexts + analyzer_contexts
        duplicate_contexts = tuple(
            sorted(key for key, count in Counter(all_contexts).items() if count > 1)
        )
        oracle_findings: list[str] = []
        sibling_findings: list[str] = []
        candidate_findings: list[str] = []
        executor_forbidden = {
            "variant",
            "candidate_snapshot_hash",
            "pairing",
            "expectations",
            "rubric",
            "expected_output_zh",
            "oracle_ref",
        }
        for item in self._items():
            view = executor_view(item)
            exposed = _keys(view.model_dump(mode="json")) & executor_forbidden
            if exposed:
                oracle_findings.append(f"{item.work_id}:{','.join(sorted(exposed))}")
        grader_forbidden = {
            "variant",
            "candidate_snapshot_hash",
            "assertion_results",
            "oracle_ref",
            "sibling_output",
        }
        for path in (self.run_dir / "grader-work-items").glob("*.json"):
            exposed = _keys(self._read_json(path)) & grader_forbidden
            if exposed:
                sibling_findings.append(f"{path.name}:{','.join(sorted(exposed))}")
        comparator_forbidden = {
            "variant",
            "candidate_snapshot_hash",
            "assertion_results",
            "overall_score",
            "expected_winner",
        }
        for path in (self.run_dir / "comparator-work-items").glob("*.json"):
            exposed = _keys(self._read_json(path)) & comparator_forbidden
            if exposed:
                candidate_findings.append(f"{path.name}:{','.join(sorted(exposed))}")
        audit = IsolationAudit(
            valid=not (
                duplicate_contexts or oracle_findings or sibling_findings or candidate_findings
            ),
            executor_context_ids=executor_contexts,
            grader_context_ids=grader_contexts,
            comparator_context_ids=comparator_contexts,
            analyzer_context_ids=analyzer_contexts,
            duplicate_context_ids=duplicate_contexts,
            oracle_leakage_findings=tuple(oracle_findings),
            sibling_leakage_findings=tuple(sibling_findings),
            candidate_identity_findings=tuple(candidate_findings),
        )
        self.store.write_json("isolation-audit.json", audit.model_dump(mode="json"))
        return audit

    def _comparison(self, task_id: str) -> ComparatorReconciliation | None:
        path = self.run_dir / f"comparator-reconciliation/{task_id}.json"
        if not path.is_file():
            return None
        return ComparatorReconciliation.model_validate_json(path.read_text(encoding="utf-8"))

    def _efficiency(self, source: EvaluationRecord, item: EvalWorkItem) -> float:
        case = self.cases[item.task_id]
        output_sizes = [
            artifact.size_bytes
            for artifact in source.artifacts
            if artifact.path == case.requested_output.filename
        ]
        artifact_size = output_sizes[0] if len(output_sizes) == 1 else sum(output_sizes)
        ratios = (
            source.usage.duration_ms / self.policy.duration_budget_ms,
            (source.usage.input_tokens + source.usage.output_tokens) / self.policy.token_budget,
            source.usage.tool_calls / self.policy.tool_call_budget,
            artifact_size / self.policy.artifact_size_budget_bytes,
        )
        return mean(max(0.0, 1.0 - ratio) for ratio in ratios)

    def _pair_scores(
        self, task_id: str
    ) -> tuple[dict[Variant, TaskScoreVector], FunctionalPairSummary, dict[str, Any]]:
        items = self._case_items(task_id)
        raw: dict[str, dict[str, Any]] = {}
        for variant in ("no-skill", "original"):
            item = items[variant]
            source = self._source_record(item)
            deterministic = self._deterministic(item)
            e3 = self.ledger.record_for_work(f"{item.work_id}-assertions")
            if e3 is None:
                raise ValueError(f"E3 record is unavailable: {item.work_id}")
            grader, grader_id, _blind = self._grader_for_source(item.work_id)
            case = self.cases[task_id]
            grade_map = {grade.criterion_id: grade.score for grade in grader.criterion_grades}
            grader_score = _weighted_score(case, grade_map)
            assertion_total = sum(result.weight for result in e3.assertion_results)
            correctness = (
                sum(result.weight for result in e3.assertion_results if result.passed)
                / assertion_total
            )
            if abs(correctness - deterministic.weighted_score) > 1e-9:
                raise ValueError("E3 record and DeterministicGradingBundle disagree")
            if abs(grader_score - grader.overall_score) > 1e-6:
                raise ValueError("independent grade is not reproducible")
            raw[variant] = {
                "item": item,
                "source": source,
                "e3": e3,
                "deterministic": deterministic,
                "grader": grader,
                "grader_id": grader_id,
                "task_correctness": correctness,
                "grader_score": grader_score,
            }
        comparison = self._comparison(task_id)
        if comparison is None and task_id in self.policy.comparator_case_ids:
            terminal = self._role_terminalization_for_task(
                FunctionalRole.COMPARATOR,
                task_id,
            )
            if terminal is not None:
                raise RoleEvidenceIncompleteError(terminal)
            raise ValueError(f"fresh comparator evidence is required: {task_id}")
        quality = {
            variant: float(raw[variant]["grader_score"]) for variant in ("no-skill", "original")
        }
        if comparison is not None:
            original_preference = (comparison.original_margin + 1.0) / 2.0
            comparator_quality = {
                "original": original_preference,
                "no-skill": 1.0 - original_preference,
            }
            for variant in ("no-skill", "original"):
                quality[variant] = (1.0 - self.policy.comparator_weight) * quality[
                    variant
                ] + self.policy.comparator_weight * comparator_quality[variant]
        correctness_delta = float(raw["original"]["task_correctness"]) - float(
            raw["no-skill"]["task_correctness"]
        )
        quality_delta = quality["original"] - quality["no-skill"]
        paired_basis = clamp(
            self.policy.task_correctness_weight * correctness_delta
            + self.policy.output_quality_weight * quality_delta
        )
        e0 = self._read_json(self.run_dir / "e0-package-record.json")
        package_quality = float(e0["package_quality"])
        vectors: dict[Variant, TaskScoreVector] = {}
        for variant in ("no-skill", "original"):
            value = raw[variant]
            item = value["item"]
            source = value["source"]
            e3 = value["e3"]
            grader_id = str(value["grader_id"])
            evidence_refs = [
                self._project_ref(f"records/{source.record_id}.json"),
                self._project_ref(f"records/{e3.record_id}.json"),
                self._project_ref(f"deterministic/{item.work_id}.json"),
                self._project_ref(f"grader-submissions/{grader_id}.json"),
                self._project_ref("e0-package-record.json"),
            ]
            if comparison is not None:
                evidence_refs.append(self._project_ref(f"comparator-reconciliation/{task_id}.json"))
            vectors[variant] = TaskScoreVector(
                task_id=task_id,
                pair_id=item.pair_id,
                variant=variant,
                candidate_snapshot_hash=item.candidate_snapshot_hash,
                task_correctness=float(value["task_correctness"]),
                output_quality=quality[variant],
                skill_gain=paired_basis if variant == "original" else 0.0,
                reliability=max(0.0, 1.0 - source.uncertainty),
                efficiency=self._efficiency(source, item),
                # Package quality is pair-level static context. Copying the same
                # value keeps it neutral in no-skill/original deltas.
                package_quality=package_quality,
                evidence_refs=tuple(evidence_refs),
                scoring_policy_ref=str(self.metadata["scoring_policy_ref"]),
            )
        summary = FunctionalPairSummary(
            task_id=task_id,
            pair_id=items["original"].pair_id,
            split=self.cases[task_id].split,
            no_skill_vector_ref=self._project_ref(
                f"task-score-vectors/{items['no-skill'].work_id}.json"
            ),
            original_vector_ref=self._project_ref(
                f"task-score-vectors/{items['original'].work_id}.json"
            ),
            correctness_delta=correctness_delta,
            quality_delta=quality_delta,
            paired_basis_delta=paired_basis,
            comparator_margin=(comparison.original_margin if comparison else None),
            skill_gain=paired_basis,
            comparator_consistent=(comparison.consistent if comparison else None),
        )
        audit = {
            "task_id": task_id,
            "assertion_recomputed": {
                variant: raw[variant]["task_correctness"] for variant in ("no-skill", "original")
            },
            "grader_recomputed": {
                variant: raw[variant]["grader_score"] for variant in ("no-skill", "original")
            },
            "quality_after_comparator": quality,
            "paired_basis_delta": paired_basis,
        }
        return vectors, summary, audit

    @staticmethod
    def _reliability_summary(values: list[float]) -> ReliabilitySummary:
        average = mean(values)
        deviation = pstdev(values)
        outliers = (
            sum(abs(value - average) > 2 * deviation for value in values) if deviation > 0 else 0
        )
        return ReliabilitySummary(
            sample_count=len(values),
            mean=average,
            std=deviation,
            minimum=min(values),
            maximum=max(values),
            failure_rate=0.0,
            outlier_count=outliers,
        )

    @staticmethod
    def _sum_usage(records: list[UsageRecord]) -> UsageRecord:
        kinds = {record.token_count_kind for record in records}
        kind: Literal["reported", "estimated", "unavailable"] = (
            "reported"
            if kinds == {"reported"}
            else "unavailable"
            if kinds == {"unavailable"}
            else "estimated"
        )
        return UsageRecord(
            input_tokens=sum(item.input_tokens for item in records),
            output_tokens=sum(item.output_tokens for item in records),
            tool_calls=sum(item.tool_calls for item in records),
            duration_ms=sum(item.duration_ms for item in records),
            token_count_kind=kind,
        )

    def _usage_report(self) -> dict[str, Any]:
        executor_submissions = self.ledger.submissions()
        grader_submissions = [
            IndependentGraderSubmission.model_validate_json(path.read_text(encoding="utf-8"))
            for path in (self.run_dir / "grader-submissions").glob("*.json")
        ]
        comparator_submissions = [
            ComparatorSubmission.model_validate_json(path.read_text(encoding="utf-8"))
            for path in (self.run_dir / "comparator-submissions").glob("*.json")
        ]
        analyzer_submissions = [
            AnalyzerSubmission.model_validate_json(path.read_text(encoding="utf-8"))
            for path in (self.run_dir / "analyzer-submissions").glob("*.json")
        ]
        usages = {
            "executor": [item.usage for item in executor_submissions],
            "independent_grader": [item.role_run.usage for item in grader_submissions],
            "comparator": [item.role_run.usage for item in comparator_submissions],
            "analyzer": [item.role_run.usage for item in analyzer_submissions],
        }
        return {
            "schema_version": "1.0.0",
            "roles": {
                role: {
                    "calls": len(records),
                    "usage": self._sum_usage(records).model_dump(mode="json"),
                    "failures": (
                        sum(item.failure_kind is not None for item in executor_submissions)
                        if role == "executor"
                        else 0
                    ),
                }
                for role, records in usages.items()
            },
        }

    def finalize(self) -> dict[str, Any]:
        access = self.audit_package_access()
        isolation = self.audit_isolation()
        if not access["valid"] or not isolation.valid:
            raise ValueError("access and isolation audits must pass before scoring")
        task_ids = sorted({item.task_id for item in self._items()})
        analysis_unavailable: list[dict[str, str]] = []
        for path in (self.run_dir / "analyzer-work-items").glob("*.json"):
            work = AnalyzerWorkItem.model_validate_json(path.read_text(encoding="utf-8"))
            terminal = self._role_terminalization(
                FunctionalRole.ANALYZER,
                work.analyzer_work_id,
            )
            if terminal is not None:
                analysis_unavailable.append(
                    {
                        "task_id": work.task_id,
                        "work_id": work.analyzer_work_id,
                        "terminalization_id": terminal.terminalization_id,
                    }
                )
                continue
            self._run_model(
                f"analyzer-submissions/{work.analyzer_work_id}.json", AnalyzerSubmission
            )
        pair_summaries: list[FunctionalPairSummary] = []
        vectors_by_variant: dict[str, list[TaskScoreVector]] = {
            "no-skill": [],
            "original": [],
        }
        recomputation_rows: list[dict[str, Any]] = []
        for task_id in task_ids:
            vectors, pair_summary, audit = self._pair_scores(task_id)
            pair_summaries.append(pair_summary)
            recomputation_rows.append(audit)
            for variant, vector in vectors.items():
                item = self._case_items(task_id)[variant]
                vectors_by_variant[variant].append(vector)
                self.store.write_json(
                    f"task-score-vectors/{item.work_id}.json", vector.model_dump(mode="json")
                )
                e3 = self.ledger.record_for_work(f"{item.work_id}-assertions")
                if e3 is None:
                    raise ValueError(f"E3 record disappeared: {item.work_id}")
                scored = e3.model_copy(update={"task_score_vector": vector})
                self.ledger.store_derived_record(scored)
                self.store.write_json(
                    f"records/{scored.record_id}.json", scored.model_dump(mode="json")
                )
        usage = {
            variant: self._sum_usage(
                [
                    self._source_record(item).usage
                    for item in self._items()
                    if item.variant == variant
                ]
            )
            for variant in ("no-skill", "original")
        }
        reliability = {
            variant: self._reliability_summary(
                [vector.reliability for vector in vectors_by_variant[variant]]
            )
            for variant in ("no-skill", "original")
        }
        trigger_separation = {
            "schema_version": "1.0.0",
            "frozen_trigger_cases": len(self.frozen.trigger_cases),
            "functional_run_includes_trigger_scores": False,
            "note_zh": "R3 功能评分不执行或混入 Trigger Eval. Trigger Eval 保持独立口径。",
        }
        self.store.write_json("trigger-eval-separation.json", trigger_separation)
        summary = FunctionalRunSummary(
            run_id=self.run_dir.name,
            frozen_plan_hash=self.frozen.plan_hash,
            scoring_policy_ref=str(self.metadata["scoring_policy_ref"]),
            pair_summaries=tuple(pair_summaries),
            reliability=reliability,
            usage=usage,
            trigger_metrics_ref=self._project_ref("trigger-eval-separation.json"),
        )
        self.store.write_json("functional-run-summary.json", summary.model_dump(mode="json"))
        self.store.write_json(
            "score-recomputation-audit.json",
            {
                "schema_version": "1.0.0",
                "valid": True,
                "source": "raw E2/E3, rubric grades, comparator reconciliation, and E0",
                "rows": recomputation_rows,
                "vector_count": sum(len(values) for values in vectors_by_variant.values()),
                "trigger_mixed": False,
            },
        )
        analyzer_submissions = [
            AnalyzerSubmission.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted((self.run_dir / "analyzer-submissions").glob("*.json"))
        ]
        self.store.write_json(
            "asi-dataset.json",
            {
                "schema_version": "1.0.0",
                "source": "typed R3 Analyzer submissions",
                "analysis_complete": not analysis_unavailable,
                "analysis_unavailable": analysis_unavailable,
                "rows": [item.model_dump(mode="json") for item in analyzer_submissions],
                "target_node_ids": sorted(
                    {
                        node_id
                        for item in analyzer_submissions
                        for analysis in item.analyses
                        for node_id in analysis.target_node_ids
                    }
                ),
            },
        )
        usage_report = self._usage_report()
        self.store.write_json("usage-report.json", usage_report)
        return {
            "pairs": len(pair_summaries),
            "vectors": sum(len(values) for values in vectors_by_variant.values()),
            "skill_gain_mean": mean(item.skill_gain for item in pair_summaries),
            "isolation_valid": isolation.valid,
            "package_access_valid": access["valid"],
            "analysis_complete": not analysis_unavailable,
            "analysis_unavailable": analysis_unavailable,
            "usage": usage_report,
        }

    def _independent_pair_vectors(self, task_id: str) -> dict[Variant, TaskScoreVector]:
        """Rebuild one pair without calling the production aggregation path.

        This deliberately duplicates the small, frozen scoring formula so the
        verification command can catch regressions in ``_pair_scores`` instead
        of merely exercising the same implementation twice.
        """
        items = self._case_items(task_id)
        case = self.cases[task_id]
        raw: dict[Variant, dict[str, Any]] = {}
        for variant in ("no-skill", "original"):
            item = items[variant]
            source = self._source_record(item)
            e3 = self.ledger.record_for_work(f"{item.work_id}-assertions")
            if e3 is None:
                raise ValueError(f"E3 record is unavailable: {item.work_id}")
            total_weight = sum(assertion.weight for assertion in e3.assertion_results)
            if total_weight <= 0:
                raise ValueError("E3 assertions must have positive total weight")
            correctness = (
                sum(assertion.weight for assertion in e3.assertion_results if assertion.passed)
                / total_weight
            )
            grader, grader_id, _blind = self._grader_for_source(item.work_id)
            grade_scores = {grade.criterion_id: grade.score for grade in grader.criterion_grades}
            grader_score = sum(
                grade_scores[criterion.criterion_id] * criterion.weight for criterion in case.rubric
            )
            raw[variant] = {
                "item": item,
                "source": source,
                "e3": e3,
                "grader_id": grader_id,
                "task_correctness": correctness,
                "grader_score": grader_score,
            }

        quality = {
            variant: float(raw[variant]["grader_score"]) for variant in ("no-skill", "original")
        }
        comparison = self._comparison(task_id)
        if comparison is not None:
            original_preference = (comparison.original_margin + 1.0) / 2.0
            for variant, comparator_score in (
                ("original", original_preference),
                ("no-skill", 1.0 - original_preference),
            ):
                quality[variant] = (1.0 - self.policy.comparator_weight) * quality[
                    variant
                ] + self.policy.comparator_weight * comparator_score

        correctness_delta = float(raw["original"]["task_correctness"]) - float(
            raw["no-skill"]["task_correctness"]
        )
        quality_delta = quality["original"] - quality["no-skill"]
        paired_basis = clamp(
            self.policy.task_correctness_weight * correctness_delta
            + self.policy.output_quality_weight * quality_delta
        )
        package_quality = float(
            self._read_json(self.run_dir / "e0-package-record.json")["package_quality"]
        )

        vectors: dict[Variant, TaskScoreVector] = {}
        for variant in ("no-skill", "original"):
            value = raw[variant]
            item = value["item"]
            source = value["source"]
            e3 = value["e3"]
            evidence_refs = [
                self._project_ref(f"records/{source.record_id}.json"),
                self._project_ref(f"records/{e3.record_id}.json"),
                self._project_ref(f"deterministic/{item.work_id}.json"),
                self._project_ref(f"grader-submissions/{value['grader_id']}.json"),
                self._project_ref("e0-package-record.json"),
            ]
            if comparison is not None:
                evidence_refs.append(self._project_ref(f"comparator-reconciliation/{task_id}.json"))
            vectors[variant] = TaskScoreVector(
                task_id=task_id,
                pair_id=item.pair_id,
                variant=variant,
                candidate_snapshot_hash=item.candidate_snapshot_hash,
                task_correctness=float(value["task_correctness"]),
                output_quality=quality[variant],
                skill_gain=paired_basis if variant == "original" else 0.0,
                reliability=max(0.0, 1.0 - source.uncertainty),
                efficiency=self._efficiency(source, item),
                package_quality=package_quality,
                evidence_refs=tuple(evidence_refs),
                scoring_policy_ref=str(self.metadata["scoring_policy_ref"]),
            )
        return vectors

    def score_verification_result(self) -> dict[str, Any]:
        """Independently rebuild every vector without mutating run evidence."""
        mismatches: list[dict[str, Any]] = []
        task_ids = sorted({item.task_id for item in self._items()})
        recomputed = 0
        for task_id in task_ids:
            vectors = self._independent_pair_vectors(task_id)
            items = self._case_items(task_id)
            for variant, expected in vectors.items():
                stored = self._run_model(
                    f"task-score-vectors/{items[variant].work_id}.json",
                    TaskScoreVector,
                )
                recomputed += 1
                if stored != expected:
                    mismatches.append(
                        {
                            "task_id": task_id,
                            "variant": variant,
                            "stored": stored.objectives,
                            "recomputed": expected.objectives,
                        }
                    )
        result = {
            "schema_version": "1.0.0",
            "valid": not mismatches,
            "recomputed_vectors": recomputed,
            "mismatches": mismatches,
            "trigger_mixed": False,
        }
        return result

    def verify_scores(self) -> dict[str, Any]:
        """Persist the independent score verification for an open run."""

        result = self.score_verification_result()
        self.store.write_json("score-independent-verification.json", result)
        return result


def independently_verify_functional_scores(
    project_root: Path,
    run_dir: Path,
) -> dict[str, Any]:
    """Read-only score recomputation suitable for a terminal-sealed run.

    It reuses the same isolated Functional coordinator's deliberately separate
    scoring formula, but opens the SQLite ledger in ``mode=ro`` and never
    writes an artifact.  This makes a missing pre-seal verification artifact a
    fail-closed cache concern rather than a reason to mutate sealed evidence.
    """

    root = project_root.resolve()
    run = run_dir.resolve(strict=True)
    if not run.is_relative_to(root):
        raise ValueError("functional score verification run escapes the project")
    ledger = EvalLedger(run / "ledger.sqlite3", create=False, read_only=True)
    try:
        coordinator = FunctionalEvalCoordinator(root, run, ledger, ArtifactStore(run))
        return coordinator.score_verification_result()
    finally:
        ledger.close()
