"""Gate funnel and immutable acceptance-decision schemas."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from gepase.evals.statistics import PairedScore, PairedStatistics
from gepase.evals.variance import VarianceDecision
from gepase.optimizer.status import CandidateStatus
from gepase.schemas.common import FrozenModel


class GateLevel(StrEnum):
    GATE_0_SCHEMA = "gate_0_schema"
    GATE_1_STATIC = "gate_1_static"
    GATE_2_MINIBATCH = "gate_2_minibatch"
    GATE_3_VALIDATION = "gate_3_validation"
    GATE_4_FRONTIER = "gate_4_frontier"


class GateOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"
    NOT_RUN = "not_run"


class AcceptancePolicyKind(StrEnum):
    CONSERVATIVE = "conservative"
    EXPLORATORY = "exploratory"


class GateUsage(FrozenModel):
    metric_calls: int = Field(default=0, ge=0)
    e2_calls: int = Field(default=0, ge=0)
    e3_calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    duration_ms: int = Field(default=0, ge=0)


class GateResult(FrozenModel):
    schema_version: str = "1.0.0"
    level: GateLevel
    outcome: GateOutcome
    reason_codes: tuple[str, ...] = Field(min_length=1)
    human_summary: str = Field(min_length=1)
    evidence_refs: tuple[str, ...] = ()
    checks: dict[str, object] = Field(default_factory=dict)
    usage: GateUsage = Field(default_factory=GateUsage)
    target_calls: int = Field(default=0, ge=0)
    started_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


def gate_decision_id_for(payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"gate-decision-{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


class GateDecision(FrozenModel):
    schema_version: str = "1.0.0"
    decision_id: str
    run_id: str
    patch_id: str
    parent_candidate_id: str
    candidate_id: str | None
    policy: AcceptancePolicyKind
    verdict: CandidateStatus
    gates: tuple[GateResult, ...] = Field(min_length=1)
    train_pairs: tuple[PairedScore, ...] = ()
    validation_pairs: tuple[PairedScore, ...] = ()
    train_statistics: PairedStatistics | None = None
    validation_statistics: PairedStatistics | None = None
    variance_decision: VarianceDecision | None = None
    reason_codes: tuple[str, ...] = Field(min_length=1)
    human_summary: str = Field(min_length=1)
    frontier_eligible: bool
    exploration_pool_eligible: bool = False
    rejected_record_id: str | None = None
    total_usage: GateUsage = Field(default_factory=GateUsage)
    test_access_count: int = Field(default=0, ge=0)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @model_validator(mode="after")
    def decision_invariants(self) -> GateDecision:
        levels = [item.level for item in self.gates]
        if len(levels) != len(set(levels)):
            raise ValueError("gate decision contains duplicate levels")
        if self.frontier_eligible != (self.verdict is CandidateStatus.ACCEPTED):
            raise ValueError("only accepted candidates may enter the frontier")
        if self.verdict is CandidateStatus.ACCEPTED:
            required = {
                GateLevel.GATE_0_SCHEMA,
                GateLevel.GATE_1_STATIC,
                GateLevel.GATE_2_MINIBATCH,
                GateLevel.GATE_3_VALIDATION,
            }
            passed = {item.level for item in self.gates if item.outcome is GateOutcome.PASSED}
            if not required <= passed:
                raise ValueError("accepted candidate is missing a passed gate")
        expected = gate_decision_id_for(self.identity_payload())
        if self.decision_id != expected:
            raise ValueError("gate decision id does not match immutable payload")
        return self

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "patch_id": self.patch_id,
            "parent_candidate_id": self.parent_candidate_id,
            "candidate_id": self.candidate_id,
            "policy": self.policy.value,
            "verdict": self.verdict.value,
            "gates": [item.model_dump(mode="json") for item in self.gates],
            "train_pairs": [item.model_dump(mode="json") for item in self.train_pairs],
            "validation_pairs": [item.model_dump(mode="json") for item in self.validation_pairs],
            "train_statistics": (
                self.train_statistics.model_dump(mode="json") if self.train_statistics else None
            ),
            "validation_statistics": (
                self.validation_statistics.model_dump(mode="json")
                if self.validation_statistics
                else None
            ),
            "variance_decision": (
                self.variance_decision.model_dump(mode="json") if self.variance_decision else None
            ),
            "reason_codes": list(self.reason_codes),
            "human_summary": self.human_summary,
            "frontier_eligible": self.frontier_eligible,
            "exploration_pool_eligible": self.exploration_pool_eligible,
            "rejected_record_id": self.rejected_record_id,
            "total_usage": self.total_usage.model_dump(mode="json"),
            "test_access_count": self.test_access_count,
            "created_at": self.created_at,
        }


def build_gate_decision(**values: Any) -> GateDecision:
    temporary = GateDecision.model_construct(decision_id="pending", **values)
    identity = temporary.identity_payload()
    return GateDecision.model_validate(
        {**temporary.model_dump(mode="json"), "decision_id": gate_decision_id_for(identity)}
    )
