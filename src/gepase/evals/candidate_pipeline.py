"""Candidate-only Functional coordination against a verified reference anchor.

This module is an R4 coordinator over the authoritative Eval ledger.  It does
not implement another evaluator: E2 ingestion, E3 assertions, artifacts, and
TaskScoreVector remain owned by :mod:`gepase.evals.engine` and the existing
Functional contracts.
"""

from __future__ import annotations

from pathlib import Path
from statistics import mean
from typing import Any, Literal

from pydantic import Field

from gepase.evals.eval_plan import FrozenEvalPlan
from gepase.evals.functional import (
    BlindArtifact,
    ComparatorSide,
    ComparatorSubmission,
    ComparatorWorkItem,
    FunctionalRole,
    FunctionalScoringPolicy,
    IndependentGraderSubmission,
    IndependentGraderWorkItem,
    clamp,
    stable_role_id,
)
from gepase.evals.functional_pipeline import (
    FunctionalEvalCoordinator,
    RoleEvidenceIncompleteError,
    _weighted_score,
)
from gepase.evals.scores import TaskScoreVector
from gepase.evals.statistics import PairedScore
from gepase.evals.work_items import EvalWorkItem, Variant
from gepase.optimizer.runtime import ReferenceEvidenceKey
from gepase.package.ir import PackageGraph
from gepase.schemas.common import FrozenModel


class CandidateComparatorReconciliation(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    task_id: str
    pair_id: str
    ab_submission_ref: str
    ba_submission_ref: str
    ab_candidate_outcome: Literal["win", "loss", "tie"]
    ba_candidate_outcome: Literal["win", "loss", "tie"]
    consistent: bool
    candidate_margin: float = Field(ge=-1, le=1)


class CandidatePairSummary(FrozenModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    task_id: str
    pair_id: str
    split: Literal["train", "validation"]
    reference_vector_ref: str
    candidate_vector_ref: str
    reference_score: float = Field(ge=0, le=1)
    candidate_score: float = Field(ge=0, le=1)
    paired_delta: float = Field(ge=-1, le=1)
    correctness_delta: float = Field(ge=-1, le=1)
    quality_delta: float = Field(ge=-1, le=1)
    comparator_margin: float | None = Field(default=None, ge=-1, le=1)
    comparator_consistent: bool | None = None


class CandidateFunctionalCoordinator(FunctionalEvalCoordinator):
    """Coordinate one candidate split while keeping the R3 anchor immutable."""

    def __init__(self, project_root: Path, run_dir: Path, ledger: Any, store: Any) -> None:
        self.project_root = project_root.resolve()
        self.run_dir = run_dir.resolve()
        if not self.run_dir.is_relative_to(self.project_root):
            raise ValueError("candidate Functional run must remain inside the project")
        self.ledger = ledger
        self.store = store
        self.run_ref = self.run_dir.relative_to(self.project_root).as_posix()
        metadata = self._read_json(self.run_dir / "run-metadata.json")
        if metadata.get("mode") != "frozen-candidate":
            raise ValueError("run is not a frozen candidate Functional evaluation")
        self.metadata = metadata
        self.frozen = self._read_project_model(str(metadata["frozen_plan_ref"]), FrozenEvalPlan)
        self.policy = self._read_project_model(
            str(metadata["scoring_policy_ref"]), FunctionalScoringPolicy
        )
        self.cases = {case.case_id: case for case in self.frozen.functional_cases}
        self.graph = self._read_project_model(str(metadata["package_graph_ref"]), PackageGraph)
        self.reference_key = self._read_project_model(
            str(metadata["reference_key_ref"]), ReferenceEvidenceKey
        )
        self.reference_run = (self.project_root / self.reference_key.reference_run_ref).resolve(
            strict=True
        )
        if not self.reference_run.is_relative_to(self.project_root):
            raise ValueError("reference run escapes the project")

    def _case_items(self, task_id: str) -> dict[Variant, EvalWorkItem]:
        values: dict[Variant, EvalWorkItem] = {
            item.variant: item for item in self._items() if item.task_id == task_id
        }
        if set(values) != {"candidate"}:
            raise ValueError(f"candidate run must contain one fresh side: {task_id}")
        return values

    def _reference_models(
        self, task_id: str, variant: str | None = None
    ) -> tuple[TaskScoreVector, IndependentGraderSubmission, BlindArtifact, str]:
        expected_variant = variant or self.reference_key.reference_variant
        matches: list[tuple[TaskScoreVector, str]] = []
        for path in sorted((self.reference_run / "task-score-vectors").glob("*.json")):
            vector = TaskScoreVector.model_validate_json(path.read_text(encoding="utf-8"))
            if vector.task_id == task_id and vector.variant == expected_variant:
                matches.append((vector, path.stem))
        if len(matches) != 1:
            raise ValueError(f"reference vector is not unique: {task_id}:{expected_variant}")
        vector, source_work_id = matches[0]
        grader_ids: list[str] = []
        for path in sorted((self.reference_run / "role-state/grader").glob("*.json")):
            mapping = self._read_json(path)
            if mapping.get("source_work_id") == source_work_id:
                grader_ids.append(str(mapping["grader_work_id"]))
        if len(grader_ids) != 1:
            raise ValueError(f"reference grader mapping is not unique: {source_work_id}")
        grader_id = grader_ids[0]
        grader = IndependentGraderSubmission.model_validate_json(
            (self.reference_run / f"grader-submissions/{grader_id}.json").read_text(
                encoding="utf-8"
            )
        )
        grader_work = IndependentGraderWorkItem.model_validate_json(
            (self.reference_run / f"grader-work-items/{grader_id}.json").read_text(encoding="utf-8")
        )
        return vector, grader, grader_work.blind_artifact, source_work_id

    def prepare_graders(self) -> dict[str, Any]:
        work_ids: list[str] = []
        failed_work_ids: list[str] = []
        for item in self._items():
            source = self.ledger.record_for_work(item.work_id)
            if source is None:
                raise ValueError(f"candidate E2 record is unavailable: {item.work_id}")
            if source.failure_kind is not None:
                failed_work_ids.append(item.work_id)
                continue
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
            self.store.write_json(
                f"grader-work-items/{grader_id}.json", work.model_dump(mode="json")
            )
            self.store.write_json(
                f"role-state/grader/{grader_id}.json",
                {
                    "schema_version": "1.0.0",
                    "grader_work_id": grader_id,
                    "task_id": item.task_id,
                    "source_work_id": item.work_id,
                    "variant": item.variant,
                    "blind_id": blind.blind_id,
                },
            )
            work_ids.append(grader_id)
        self._reserve_role_batch("independent_grader", work_ids)
        return {
            "prepared": len(work_ids),
            "grader_work_ids": sorted(work_ids),
            "typed_failures_without_grader": sorted(failed_work_ids),
        }

    def prepare_comparators(self) -> dict[str, Any]:
        work_ids: list[str] = []
        incomplete_task_ids: list[str] = []
        selected = {item.task_id for item in self._items()}
        for task_id in self.policy.comparator_case_ids:
            if task_id not in selected:
                continue
            case = self.cases[task_id]
            candidate_item = self._case_items(task_id)["candidate"]
            source = self.ledger.record_for_work(candidate_item.work_id)
            if source is None:
                raise ValueError(f"candidate E2 record is unavailable: {candidate_item.work_id}")
            if source.failure_kind is not None:
                failure_ref = self._project_ref(f"records/{source.record_id}.json")
                reconciliation = CandidateComparatorReconciliation(
                    task_id=task_id,
                    pair_id=candidate_item.pair_id,
                    ab_submission_ref=failure_ref,
                    ba_submission_ref=failure_ref,
                    ab_candidate_outcome="loss",
                    ba_candidate_outcome="loss",
                    consistent=True,
                    candidate_margin=-1.0,
                )
                self.store.write_json(
                    f"comparator-reconciliation/{task_id}.json",
                    reconciliation.model_dump(mode="json"),
                )
                self.store.write_json(
                    f"failure-comparator-decisions/{task_id}.json",
                    {
                        "schema_version": "1.0.0",
                        "task_id": task_id,
                        "candidate_failure_kind": source.failure_kind.value,
                        "candidate_record_ref": failure_ref,
                        "candidate_margin": -1.0,
                        "agent_comparator_calls": 0,
                    },
                )
                continue
            try:
                candidate_blind = self._grader_for_source(candidate_item.work_id)[2]
            except RoleEvidenceIncompleteError:
                incomplete_task_ids.append(task_id)
                continue
            _vector, _grader, reference_blind, _source = self._reference_models(task_id)
            for order in ("AB", "BA"):
                left_kind = "reference" if order == "AB" else "candidate"
                right_kind = "candidate" if order == "AB" else "reference"
                blind = {"reference": reference_blind, "candidate": candidate_blind}
                comparator_id = stable_role_id(
                    "candidate-comparator-work",
                    {
                        "task": task_id,
                        "order": order,
                        "candidate": self.metadata["candidate_content_hash"],
                        "reference_key": self.reference_key.key_hash,
                    },
                )
                work = ComparatorWorkItem(
                    comparator_work_id=comparator_id,
                    task_id=task_id,
                    task_prompt=case.prompt,
                    expected_output_zh=case.expected_output_zh,
                    rubric=case.rubric,
                    left=ComparatorSide(side_id="left", blind_artifact=blind[left_kind]),
                    right=ComparatorSide(side_id="right", blind_artifact=blind[right_kind]),
                    order_label=order,
                    submission_schema_ref="schemas/comparator_submission.schema.json",
                )
                self.store.write_json(
                    f"comparator-work-items/{comparator_id}.json",
                    work.model_dump(mode="json"),
                )
                self.store.write_json(
                    f"role-state/comparator/{comparator_id}.json",
                    {
                        "schema_version": "1.0.0",
                        "comparator_work_id": comparator_id,
                        "task_id": task_id,
                        "order": order,
                        "left_kind": left_kind,
                        "right_kind": right_kind,
                    },
                )
                work_ids.append(comparator_id)
        self._reserve_role_batch("comparator", work_ids)
        return {
            "prepared": len(work_ids),
            "comparator_work_ids": sorted(work_ids),
            "evidence_incomplete_task_ids": sorted(incomplete_task_ids),
        }

    def reconcile_comparators(self) -> dict[str, Any]:
        task_ids: list[str] = []
        incomplete_task_ids: list[str] = []
        selected = {item.task_id for item in self._items()}
        for task_id in self.policy.comparator_case_ids:
            if task_id not in selected:
                continue
            if self._role_terminalization_for_task(
                FunctionalRole.INDEPENDENT_GRADER,
                task_id,
            ):
                incomplete_task_ids.append(task_id)
                continue
            existing = self.run_dir / f"comparator-reconciliation/{task_id}.json"
            if existing.is_file():
                task_ids.append(task_id)
                continue
            outcomes: dict[str, Literal["win", "loss", "tie"]] = {}
            refs: dict[str, str] = {}
            role_incomplete = False
            for path in sorted((self.run_dir / "role-state/comparator").glob("*.json")):
                mapping = self._read_json(path)
                if mapping.get("task_id") != task_id:
                    continue
                comparator_id = str(mapping["comparator_work_id"])
                if self._role_terminalization(FunctionalRole.COMPARATOR, comparator_id):
                    role_incomplete = True
                    continue
                submission = self._run_model(
                    f"comparator-submissions/{comparator_id}.json", ComparatorSubmission
                )
                if submission.winner == "tie":
                    outcome: Literal["win", "loss", "tie"] = "tie"
                else:
                    winner_kind = str(mapping[f"{submission.winner}_kind"])
                    outcome = "win" if winner_kind == "candidate" else "loss"
                outcomes[str(mapping["order"])] = outcome
                refs[str(mapping["order"])] = self._project_ref(
                    f"comparator-submissions/{comparator_id}.json"
                )
            if role_incomplete:
                incomplete_task_ids.append(task_id)
                continue
            if set(outcomes) != {"AB", "BA"}:
                raise ValueError(f"candidate AB/BA comparators are incomplete: {task_id}")
            numeric = {"win": 1.0, "loss": -1.0, "tie": 0.0}
            reconciliation = CandidateComparatorReconciliation(
                task_id=task_id,
                pair_id=self._case_items(task_id)["candidate"].pair_id,
                ab_submission_ref=refs["AB"],
                ba_submission_ref=refs["BA"],
                ab_candidate_outcome=outcomes["AB"],
                ba_candidate_outcome=outcomes["BA"],
                consistent=outcomes["AB"] == outcomes["BA"],
                candidate_margin=(numeric[outcomes["AB"]] + numeric[outcomes["BA"]]) / 2,
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

    def _candidate_comparison(self, task_id: str) -> CandidateComparatorReconciliation | None:
        path = self.run_dir / f"comparator-reconciliation/{task_id}.json"
        if not path.is_file():
            return None
        return CandidateComparatorReconciliation.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def _score_task(
        self, task_id: str
    ) -> tuple[TaskScoreVector, CandidatePairSummary, PairedScore, dict[str, Any]]:
        item = self._case_items(task_id)["candidate"]
        source_record = self.ledger.record_for_work(item.work_id)
        if source_record is None:
            raise ValueError(f"candidate E2 record is unavailable: {item.work_id}")
        if source_record.failure_kind is not None:
            return self._score_failed_task(task_id, item, source_record)
        source = self._source_record(item)
        deterministic = self._deterministic(item)
        e3 = self.ledger.record_for_work(f"{item.work_id}-assertions")
        if e3 is None:
            raise ValueError(f"candidate E3 record is unavailable: {item.work_id}")
        grader, grader_id, _blind = self._grader_for_source(item.work_id)
        case = self.cases[task_id]
        candidate_grader_score = _weighted_score(
            case,
            {grade.criterion_id: grade.score for grade in grader.criterion_grades},
        )
        reference_vector, reference_grader, _ref_blind, source_work_id = self._reference_models(
            task_id
        )
        reference_grader_score = _weighted_score(
            case,
            {grade.criterion_id: grade.score for grade in reference_grader.criterion_grades},
        )
        candidate_correctness = deterministic.weighted_score
        reference_correctness = reference_vector.task_correctness
        quality = {
            "candidate": candidate_grader_score,
            "reference": reference_grader_score,
        }
        comparison = self._candidate_comparison(task_id)
        if task_id in self.policy.comparator_case_ids and comparison is None:
            terminal = self._role_terminalization_for_task(
                FunctionalRole.COMPARATOR,
                task_id,
            )
            if terminal is not None:
                raise RoleEvidenceIncompleteError(terminal)
            raise ValueError(f"fresh candidate comparator is required: {task_id}")
        if comparison is not None:
            candidate_preference = (comparison.candidate_margin + 1.0) / 2.0
            quality["candidate"] = (1.0 - self.policy.comparator_weight) * quality[
                "candidate"
            ] + self.policy.comparator_weight * candidate_preference
            quality["reference"] = (1.0 - self.policy.comparator_weight) * quality[
                "reference"
            ] + self.policy.comparator_weight * (1.0 - candidate_preference)
        correctness_delta = candidate_correctness - reference_correctness
        quality_delta = quality["candidate"] - quality["reference"]
        reference_score = clamp(
            self.policy.task_correctness_weight * reference_correctness
            + self.policy.output_quality_weight * quality["reference"],
            0.0,
            1.0,
        )
        candidate_score = clamp(
            self.policy.task_correctness_weight * candidate_correctness
            + self.policy.output_quality_weight * quality["candidate"],
            0.0,
            1.0,
        )
        delta = candidate_score - reference_score
        e0 = self._read_json(self.run_dir / "e0-package-record.json")
        evidence_refs = [
            self._project_ref(f"records/{source.record_id}.json"),
            self._project_ref(f"records/{e3.record_id}.json"),
            self._project_ref(f"deterministic/{item.work_id}.json"),
            self._project_ref(f"grader-submissions/{grader_id}.json"),
            self._project_ref("e0-package-record.json"),
            f"{self.reference_key.reference_run_ref}/task-score-vectors/{source_work_id}.json",
        ]
        if comparison is not None:
            evidence_refs.append(self._project_ref(f"comparator-reconciliation/{task_id}.json"))
        vector = TaskScoreVector(
            task_id=task_id,
            pair_id=item.pair_id,
            variant="candidate",
            candidate_snapshot_hash=item.candidate_snapshot_hash,
            task_correctness=candidate_correctness,
            output_quality=quality["candidate"],
            skill_gain=clamp(reference_vector.skill_gain + delta),
            reliability=max(0.0, 1.0 - source.uncertainty),
            efficiency=self._efficiency(source, item),
            package_quality=float(e0["package_quality"]),
            evidence_refs=tuple(evidence_refs),
            scoring_policy_ref=str(self.metadata["scoring_policy_ref"]),
        )
        vector_ref = self._project_ref(f"task-score-vectors/{item.work_id}.json")
        reference_ref = (
            f"{self.reference_key.reference_run_ref}/task-score-vectors/{source_work_id}.json"
        )
        summary = CandidatePairSummary(
            task_id=task_id,
            pair_id=item.pair_id,
            split=case.split,
            reference_vector_ref=reference_ref,
            candidate_vector_ref=vector_ref,
            reference_score=reference_score,
            candidate_score=candidate_score,
            paired_delta=delta,
            correctness_delta=correctness_delta,
            quality_delta=quality_delta,
            comparator_margin=(comparison.candidate_margin if comparison else None),
            comparator_consistent=(comparison.consistent if comparison else None),
        )
        paired = PairedScore(
            task_id=task_id,
            category=case.case_family,
            risk_level=case.risk,
            parent_score=reference_score,
            candidate_score=candidate_score,
            evidence_tier="E3",
            minimum_acceptance_tier=case.evidence_policy.minimum_tier.value,
            parent_record_id=reference_ref,
            candidate_record_id=vector_ref,
            uncertainty=max(source.uncertainty, 0.0),
        )
        audit = {
            "task_id": task_id,
            "reference_correctness": reference_correctness,
            "candidate_correctness": candidate_correctness,
            "reference_grader_score": reference_grader_score,
            "candidate_grader_score": candidate_grader_score,
            "quality_after_comparator": quality,
            "reference_score": reference_score,
            "candidate_score": candidate_score,
            "paired_delta": delta,
        }
        return vector, summary, paired, audit

    def _score_failed_task(
        self, task_id: str, item: EvalWorkItem, source: Any
    ) -> tuple[TaskScoreVector, CandidatePairSummary, PairedScore, dict[str, Any]]:
        case = self.cases[task_id]
        reference_vector, reference_grader, _ref_blind, source_work_id = self._reference_models(
            task_id
        )
        reference_grader_score = _weighted_score(
            case,
            {grade.criterion_id: grade.score for grade in reference_grader.criterion_grades},
        )
        comparison = self._candidate_comparison(task_id)
        if task_id in self.policy.comparator_case_ids and comparison is None:
            raise ValueError(f"typed failure comparator decision is unavailable: {task_id}")
        reference_quality = reference_grader_score
        if comparison is not None:
            candidate_preference = (comparison.candidate_margin + 1.0) / 2.0
            reference_quality = (
                1.0 - self.policy.comparator_weight
            ) * reference_quality + self.policy.comparator_weight * (1.0 - candidate_preference)
        reference_score = clamp(
            self.policy.task_correctness_weight * reference_vector.task_correctness
            + self.policy.output_quality_weight * reference_quality,
            0.0,
            1.0,
        )
        candidate_score = 0.0
        delta = -reference_score
        e0 = self._read_json(self.run_dir / "e0-package-record.json")
        failure_ref = self._project_ref(f"records/{source.record_id}.json")
        reference_ref = (
            f"{self.reference_key.reference_run_ref}/task-score-vectors/{source_work_id}.json"
        )
        evidence_refs = [failure_ref, self._project_ref("e0-package-record.json"), reference_ref]
        if comparison is not None:
            evidence_refs.append(self._project_ref(f"comparator-reconciliation/{task_id}.json"))
        vector = TaskScoreVector(
            task_id=task_id,
            pair_id=item.pair_id,
            variant="candidate",
            candidate_snapshot_hash=item.candidate_snapshot_hash,
            task_correctness=0.0,
            output_quality=0.0,
            skill_gain=clamp(reference_vector.skill_gain + delta),
            reliability=0.0,
            efficiency=0.0,
            package_quality=float(e0["package_quality"]),
            evidence_refs=tuple(evidence_refs),
            scoring_policy_ref=str(self.metadata["scoring_policy_ref"]),
        )
        vector_ref = self._project_ref(f"task-score-vectors/{item.work_id}.json")
        summary = CandidatePairSummary(
            task_id=task_id,
            pair_id=item.pair_id,
            split=case.split,
            reference_vector_ref=reference_ref,
            candidate_vector_ref=vector_ref,
            reference_score=reference_score,
            candidate_score=candidate_score,
            paired_delta=delta,
            correctness_delta=-reference_vector.task_correctness,
            quality_delta=-reference_quality,
            comparator_margin=(comparison.candidate_margin if comparison else None),
            comparator_consistent=(comparison.consistent if comparison else None),
        )
        paired = PairedScore(
            task_id=task_id,
            category=case.case_family,
            risk_level=case.risk,
            parent_score=reference_score,
            candidate_score=0.0,
            evidence_tier="E2",
            minimum_acceptance_tier=case.evidence_policy.minimum_tier.value,
            parent_record_id=reference_ref,
            candidate_record_id=vector_ref,
            uncertainty=1.0,
        )
        audit = {
            "task_id": task_id,
            "typed_failure": source.failure_kind.value,
            "reference_correctness": reference_vector.task_correctness,
            "candidate_correctness": 0.0,
            "reference_grader_score": reference_grader_score,
            "candidate_grader_score": 0.0,
            "reference_score": reference_score,
            "candidate_score": 0.0,
            "paired_delta": delta,
            "agent_grader_calls": 0,
            "agent_comparator_calls": 0,
        }
        return vector, summary, paired, audit

    def finalize(self) -> dict[str, Any]:
        access = self.audit_package_access()
        isolation = self.audit_isolation()
        if not access["valid"] or not isolation.valid:
            raise ValueError("candidate access and isolation audits must pass before scoring")
        vectors: list[TaskScoreVector] = []
        summaries: list[CandidatePairSummary] = []
        pairs: list[PairedScore] = []
        audits: list[dict[str, Any]] = []
        incomplete: list[dict[str, Any]] = []
        for task_id in sorted({item.task_id for item in self._items()}):
            try:
                vector, summary, paired, audit = self._score_task(task_id)
            except RoleEvidenceIncompleteError as error:
                incomplete.append(
                    {
                        "task_id": task_id,
                        "work_id": error.terminalization.work_id,
                        "role": error.terminalization.role.value,
                        "terminalization_id": error.terminalization.terminalization_id,
                        "disposition": error.terminalization.disposition,
                    }
                )
                continue
            item = self._case_items(task_id)["candidate"]
            self.store.write_json(
                f"task-score-vectors/{item.work_id}.json", vector.model_dump(mode="json")
            )
            source = self.ledger.record_for_work(item.work_id)
            if source is None:
                raise ValueError(f"candidate E2 record disappeared: {item.work_id}")
            scored = self.ledger.record_for_work(f"{item.work_id}-assertions")
            if scored is None:
                if source.failure_kind is None:
                    raise ValueError(f"candidate E3 record disappeared: {item.work_id}")
                scored = source
            scored = scored.model_copy(update={"task_score_vector": vector})
            self.ledger.store_derived_record(scored)
            self.store.write_json(
                f"records/{scored.record_id}.json", scored.model_dump(mode="json")
            )
            vectors.append(vector)
            summaries.append(summary)
            pairs.append(paired)
            audits.append(audit)
        complete = not incomplete
        mean_delta = mean(item.paired_delta for item in summaries) if complete else None
        self.store.write_json(
            "candidate-run-summary.json",
            {
                "schema_version": "1.0.0",
                "candidate_id": self.metadata["candidate_id"],
                "candidate_content_hash": self.metadata["candidate_content_hash"],
                "reference_key_hash": self.reference_key.key_hash,
                "split": self.metadata["split"],
                "status": "scored" if complete else "evidence_incomplete",
                "evidence_complete": complete,
                "gate_eligible": complete,
                "incomplete_cases": incomplete,
                "pair_summaries": [item.model_dump(mode="json") for item in summaries],
                "mean_paired_delta": mean_delta,
                "strict_wins": sum(item.paired_delta > 0 for item in summaries),
                "ties": sum(item.paired_delta == 0 for item in summaries),
                "losses": sum(item.paired_delta < 0 for item in summaries),
            },
        )
        self.store.write_json(
            "paired-scores.json",
            {"schema_version": "1.0.0", "rows": [item.model_dump() for item in pairs]},
        )
        self.store.write_json(
            "score-recomputation-audit.json",
            {
                "schema_version": "1.0.0",
                "valid": complete,
                "evidence_complete": complete,
                "incomplete_cases": incomplete,
                "rows": audits,
                "vector_count": len(vectors),
                "trigger_mixed": False,
                "reference_cache_hit": True,
            },
        )
        usage = self._usage_report()
        self.store.write_json("usage-report.json", usage)
        return {
            "candidate_id": self.metadata["candidate_id"],
            "split": self.metadata["split"],
            "vectors": len(vectors),
            "status": "scored" if complete else "evidence_incomplete",
            "gate_eligible": complete,
            "incomplete_cases": incomplete,
            "mean_paired_delta": mean_delta,
            "isolation_valid": isolation.valid,
            "package_access_valid": access["valid"],
            "reference_cache_hit": True,
            "usage": usage,
        }

    def verify_scores(self) -> dict[str, Any]:
        mismatches: list[dict[str, Any]] = []
        for task_id in sorted({item.task_id for item in self._items()}):
            expected, summary, _paired, _audit = self._score_task(task_id)
            item = self._case_items(task_id)["candidate"]
            stored = self._run_model(f"task-score-vectors/{item.work_id}.json", TaskScoreVector)
            stored_summary = self._read_json(self.run_dir / "candidate-run-summary.json")
            summary_rows = {
                str(row["task_id"]): CandidatePairSummary.model_validate(row)
                for row in stored_summary.get("pair_summaries", [])
            }
            if stored != expected or summary_rows.get(task_id) != summary:
                mismatches.append(
                    {
                        "task_id": task_id,
                        "stored": stored.objectives,
                        "recomputed": expected.objectives,
                    }
                )
        result = {
            "schema_version": "1.0.0",
            "valid": not mismatches,
            "recomputed_vectors": len(self._items()),
            "mismatches": mismatches,
            "trigger_mixed": False,
            "reference_cache_hit": True,
        }
        self.store.write_json("score-independent-verification.json", result)
        return result
