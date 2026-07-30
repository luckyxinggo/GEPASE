"""GEPASE public Core contracts."""

from gepase.evals.eval_plan import (
    EvalDesignerWorkItem,
    EvalPlanDraft,
    EvalReviewSubmission,
    FrozenEvalPlan,
)
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
from gepase.evals.recovery import (
    EvidenceStagingManifest,
    RecoveryAttemptBinding,
    ReexecutionAuthorization,
    RepairExhaustionTerminalization,
    WorkRecoveryAudit,
)
from gepase.evals.reference_runtime import ReferenceExecutionConfig
from gepase.evals.scores import TaskScoreVector
from gepase.evals.work_items import EvalWorkItem, ExecutionBundle, ExecutorWorkItem
from gepase.mutation.schema import PackagePatch
from gepase.optimizer.acceptance.models import GateDecision
from gepase.optimizer.candidate import PackageCandidate
from gepase.optimizer.evolution.models import MergeParentSetSnapshot
from gepase.optimizer.merge.models import MergeOutcome
from gepase.optimizer.session_runtime import (
    ActiveSessionState,
    BudgetCheckpoint,
    BudgetContinuationDecision,
    CheckpointFreshnessAudit,
    HostAttemptAccounting,
    RuntimeBudgetBinding,
)
from gepase.package.ir import PackageGraph
from gepase.reporting.outcome import (
    EvolutionOutcomeReportConfig,
    EvolutionOutcomeReportInput,
)
from gepase.run_lifecycle import RunIntegrityCheckpoint, RunLifecycleRecord
from gepase.schemas.common import SCHEMA_VERSION
from gepase.store.evolution_pool import EvolutionPoolEntry

__all__ = [
    "SCHEMA_VERSION",
    "ActiveSessionState",
    "AnalyzerSubmission",
    "AnalyzerWorkItem",
    "BudgetCheckpoint",
    "BudgetContinuationDecision",
    "CheckpointFreshnessAudit",
    "ComparatorReconciliation",
    "ComparatorSubmission",
    "ComparatorWorkItem",
    "DeterministicGradingBundle",
    "EvalDesignerWorkItem",
    "EvalPlanDraft",
    "EvalReviewSubmission",
    "EvalWorkItem",
    "EvidenceStagingManifest",
    "EvolutionOutcomeReportConfig",
    "EvolutionOutcomeReportInput",
    "EvolutionPoolEntry",
    "ExecutionBundle",
    "ExecutorWorkItem",
    "FrozenEvalPlan",
    "FunctionalRunSummary",
    "GateDecision",
    "HostAttemptAccounting",
    "IndependentGraderSubmission",
    "IndependentGraderWorkItem",
    "IsolationAudit",
    "MergeOutcome",
    "MergeParentSetSnapshot",
    "PackageCandidate",
    "PackageGraph",
    "PackagePatch",
    "RecoveryAttemptBinding",
    "ReexecutionAuthorization",
    "ReferenceExecutionConfig",
    "RepairExhaustionTerminalization",
    "RunIntegrityCheckpoint",
    "RunLifecycleRecord",
    "RuntimeBudgetBinding",
    "TaskScoreVector",
    "WorkRecoveryAudit",
    "__version__",
]
__version__ = "0.1.0"
