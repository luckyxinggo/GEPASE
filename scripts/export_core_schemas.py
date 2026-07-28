"""Export public JSON schemas from the authoritative Core Pydantic models."""

from __future__ import annotations

import json
from pathlib import Path

from gepase.config.models import ProjectConfig
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
)
from gepase.evals.schema import TaskCase
from gepase.evals.scores import TaskScoreVector
from gepase.evals.work_items import EvalWorkItem, ExecutionBundle, ExecutorWorkItem
from gepase.mutation.schema import PackagePatch
from gepase.mutation.target_set import TargetSet
from gepase.optimizer.acceptance.models import GateDecision
from gepase.optimizer.candidate import PackageCandidate
from gepase.optimizer.evolution.models import MergeParentSetSnapshot
from gepase.optimizer.evolution_controller import (
    CandidateReflectionSubmission,
    CandidateReflectionWorkItem,
)
from gepase.optimizer.runtime import R4EvolutionConfig, ReferenceEvidenceKey
from gepase.optimizer.selectors import SelectionResult, SelectorRankingAudit
from gepase.package.coverage import GraphCoverageAudit
from gepase.package.dynamic_graph import PackageAccessOverlayAudit, SelectorGraphBinding
from gepase.package.ir import PackageGraph
from gepase.package.semantic_models import (
    SemanticCacheState,
    SemanticEnrichmentScope,
    SemanticHypothesisConfig,
    SemanticOverlayResult,
    SemanticRelationProposal,
)
from gepase.store.evolution_pool import EvolutionPoolEntry

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    models = {
        "analyzer_submission.schema.json": AnalyzerSubmission,
        "analyzer_work_item.schema.json": AnalyzerWorkItem,
        "candidate_reflection_submission.schema.json": CandidateReflectionSubmission,
        "candidate_reflection_work_item.schema.json": CandidateReflectionWorkItem,
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
        "merge_parent_set.schema.json": MergeParentSetSnapshot,
        "package_candidate.schema.json": PackageCandidate,
        "package_access_overlay_audit.schema.json": PackageAccessOverlayAudit,
        "package_graph.schema.json": PackageGraph,
        "package_patch.schema.json": PackagePatch,
        "project_config.schema.json": ProjectConfig,
        "r4_evolution_config.schema.json": R4EvolutionConfig,
        "reference_evidence_key.schema.json": ReferenceEvidenceKey,
        "selection_result.schema.json": SelectionResult,
        "selector_graph_binding.schema.json": SelectorGraphBinding,
        "selector_ranking_audit.schema.json": SelectorRankingAudit,
        "semantic_cache_state.schema.json": SemanticCacheState,
        "semantic_enrichment_scope.schema.json": SemanticEnrichmentScope,
        "semantic_hypothesis_config.schema.json": SemanticHypothesisConfig,
        "semantic_overlay_result.schema.json": SemanticOverlayResult,
        "semantic_relation_proposal.schema.json": SemanticRelationProposal,
        "task_case.schema.json": TaskCase,
        "task_score_vector.schema.json": TaskScoreVector,
        "target_set.schema.json": TargetSet,
    }
    for name, model in models.items():
        output = ROOT / "schemas" / name
        output.write_text(
            json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
