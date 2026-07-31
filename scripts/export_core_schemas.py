"""Export public JSON schemas from the authoritative Core Pydantic models."""

from __future__ import annotations

import json
from pathlib import Path

from gepase.config.models import ProjectConfig
from gepase.evals.candidate_pipeline import CandidateValidationIncompleteResolution
from gepase.evals.eval_plan import (
    EvalDesignerSubmission,
    EvalDesignerWorkItem,
    EvalPlanDraft,
    EvalReviewSubmission,
    FrozenEvalPlan,
)
from gepase.evals.evidence import EvaluationRecord
from gepase.evals.functional import (
    AnalyzerSubmission,
    AnalyzerWorkItem,
    ComparatorReconciliation,
    ComparatorSubmission,
    ComparatorWorkItem,
    DeterministicGradingBundle,
    FunctionalRunSummary,
    IndependentGraderSubmission,
    IndependentGraderWorkItem,
    IsolationAudit,
    RoleAttemptTerminalization,
)
from gepase.evals.recovery import (
    EvidenceStagingManifest,
    RecoveryAttemptBinding,
    ReexecutionAuthorization,
    RepairExhaustionTerminalization,
    WorkRecoveryAudit,
)
from gepase.evals.reference_runtime import ReferenceExecutionConfig
from gepase.evals.schema import TaskCase
from gepase.evals.scores import TaskScoreVector
from gepase.evals.work_items import EvalWorkItem, ExecutionBundle, ExecutorWorkItem
from gepase.mutation.schema import PackagePatch
from gepase.mutation.target_set import TargetSet
from gepase.optimizer.acceptance.models import GateDecision
from gepase.optimizer.acceptance.validation import (
    RelativeEfficiencyEvidence,
    RelativeEfficiencyFrontierRanking,
    RelativeEfficiencyPolicy,
    TaskScoreSecondaryEvidence,
)
from gepase.optimizer.candidate import PackageCandidate
from gepase.optimizer.evolution.models import MergeParentSetSnapshot
from gepase.optimizer.evolution.parent_sets import ParentSetEnumerationReport
from gepase.optimizer.evolution_controller import (
    CandidateReflectionSubmission,
    CandidateReflectionWorkItem,
    Generation2PlanningOutcome,
)
from gepase.optimizer.merge.models import MergeOutcome
from gepase.optimizer.runtime import R4EvolutionConfig, ReferenceEvidenceKey
from gepase.optimizer.selectors import SelectionResult, SelectorRankingAudit
from gepase.optimizer.session_runtime import (
    ActiveSessionState,
    BudgetCheckpoint,
    BudgetContinuationDecision,
    CheckpointFreshnessAudit,
    HostAttemptAccounting,
    ReservationSettlement,
    RuntimeBudgetBinding,
    WorkBatchReservation,
)
from gepase.package.coverage import GraphCoverageAudit
from gepase.package.dynamic_graph import PackageAccessOverlayAudit, SelectorGraphBinding
from gepase.package.ir import PackageGraph
from gepase.reporting.outcome import (
    EvolutionOutcomeReportConfig,
    EvolutionOutcomeReportInput,
)
from gepase.run_lifecycle import RunIntegrityCheckpoint, RunLifecycleRecord
from gepase.store.evolution_pool import EvolutionPoolEntry

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    models = {
        "analyzer_submission.schema.json": AnalyzerSubmission,
        "analyzer_work_item.schema.json": AnalyzerWorkItem,
        "active_session_state.schema.json": ActiveSessionState,
        "budget_checkpoint.schema.json": BudgetCheckpoint,
        "budget_continuation_decision.schema.json": BudgetContinuationDecision,
        "checkpoint_freshness_audit.schema.json": CheckpointFreshnessAudit,
        "host_attempt_accounting.schema.json": HostAttemptAccounting,
        "candidate_reflection_submission.schema.json": CandidateReflectionSubmission,
        "candidate_reflection_work_item.schema.json": CandidateReflectionWorkItem,
        "candidate_validation_incomplete_resolution.schema.json": (
            CandidateValidationIncompleteResolution
        ),
        "generation2_planning_outcome.schema.json": Generation2PlanningOutcome,
        "comparator_reconciliation.schema.json": ComparatorReconciliation,
        "comparator_submission.schema.json": ComparatorSubmission,
        "comparator_work_item.schema.json": ComparatorWorkItem,
        "deterministic_grading_bundle.schema.json": DeterministicGradingBundle,
        "eval_designer_submission.schema.json": EvalDesignerSubmission,
        "eval_designer_work_item.schema.json": EvalDesignerWorkItem,
        "eval_plan_draft.schema.json": EvalPlanDraft,
        "eval_review_submission.schema.json": EvalReviewSubmission,
        "eval_work_item.schema.json": EvalWorkItem,
        "evaluation_record.schema.json": EvaluationRecord,
        "evidence_staging_manifest.schema.json": EvidenceStagingManifest,
        "evolution_pool_entry.schema.json": EvolutionPoolEntry,
        "execution_bundle.schema.json": ExecutionBundle,
        "executor_work_item.schema.json": ExecutorWorkItem,
        "frozen_eval_plan.schema.json": FrozenEvalPlan,
        "functional_run_summary.schema.json": FunctionalRunSummary,
        "graph_coverage_audit.schema.json": GraphCoverageAudit,
        "gate_decision.schema.json": GateDecision,
        "independent_grader_submission.schema.json": IndependentGraderSubmission,
        "independent_grader_work_item.schema.json": IndependentGraderWorkItem,
        "isolation_audit.schema.json": IsolationAudit,
        "role_attempt_terminalization.schema.json": RoleAttemptTerminalization,
        "merge_parent_set.schema.json": MergeParentSetSnapshot,
        "merge_outcome.schema.json": MergeOutcome,
        "parent_set_enumeration_report.schema.json": ParentSetEnumerationReport,
        "package_candidate.schema.json": PackageCandidate,
        "package_access_overlay_audit.schema.json": PackageAccessOverlayAudit,
        "package_graph.schema.json": PackageGraph,
        "package_patch.schema.json": PackagePatch,
        "project_config.schema.json": ProjectConfig,
        "r4_evolution_config.schema.json": R4EvolutionConfig,
        "reference_execution_config.schema.json": ReferenceExecutionConfig,
        "reference_evidence_key.schema.json": ReferenceEvidenceKey,
        "relative_efficiency_evidence.schema.json": RelativeEfficiencyEvidence,
        "relative_efficiency_frontier_ranking.schema.json": (
            RelativeEfficiencyFrontierRanking
        ),
        "relative_efficiency_policy.schema.json": RelativeEfficiencyPolicy,
        "recovery_attempt_binding.schema.json": RecoveryAttemptBinding,
        "reexecution_authorization.schema.json": ReexecutionAuthorization,
        "repair_exhaustion_terminalization.schema.json": RepairExhaustionTerminalization,
        "reservation_settlement.schema.json": ReservationSettlement,
        "run_integrity_checkpoint.schema.json": RunIntegrityCheckpoint,
        "run_lifecycle_record.schema.json": RunLifecycleRecord,
        "runtime_budget_binding.schema.json": RuntimeBudgetBinding,
        "selection_result.schema.json": SelectionResult,
        "selector_graph_binding.schema.json": SelectorGraphBinding,
        "selector_ranking_audit.schema.json": SelectorRankingAudit,
        "task_case.schema.json": TaskCase,
        "task_score_vector.schema.json": TaskScoreVector,
        "task_score_secondary_evidence.schema.json": TaskScoreSecondaryEvidence,
        "target_set.schema.json": TargetSet,
        "evolution_outcome_report_config.schema.json": EvolutionOutcomeReportConfig,
        "evolution_outcome_report_input.schema.json": EvolutionOutcomeReportInput,
        "work_batch_reservation.schema.json": WorkBatchReservation,
        "work_recovery_audit.schema.json": WorkRecoveryAudit,
    }
    for name, model in models.items():
        output = ROOT / "schemas" / name
        output.write_text(
            json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
