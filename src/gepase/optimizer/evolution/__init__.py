"""S7.6 same-lineage evolution primitives."""

from gepase.optimizer.evolution.lineage import CandidateAncestryIndex
from gepase.optimizer.evolution.models import (
    BreedingSnapshot,
    EvolutionCandidateIdentity,
    ExclusiveContribution,
    FailureCluster,
    MergeCompatibilityReason,
    MergeCompatibilityReport,
    MergeEligibility,
    MergeParentCandidate,
    MergeParentSetSnapshot,
    MutationBranch,
)

__all__ = [
    "BreedingSnapshot",
    "CandidateAncestryIndex",
    "EvolutionCandidateIdentity",
    "ExclusiveContribution",
    "FailureCluster",
    "MergeCompatibilityReason",
    "MergeCompatibilityReport",
    "MergeEligibility",
    "MergeParentCandidate",
    "MergeParentSetSnapshot",
    "MutationBranch",
]
