"""End-to-end Gate 0-4 acceptance engine with durable decisions and rejection memory."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from gepase.evals.statistics import PairedScore
from gepase.evals.variance import VariancePolicy, variance_decision
from gepase.mutation.schema import PackagePatch, PatchApplication
from gepase.mutation.validators.schema_gate import run_schema_gate
from gepase.mutation.validators.static_gate import run_static_gate
from gepase.optimizer.acceptance.minibatch import MinibatchPolicy, run_minibatch_gate
from gepase.optimizer.acceptance.models import (
    AcceptancePolicyKind,
    GateDecision,
    GateLevel,
    GateOutcome,
    GateResult,
    GateUsage,
    build_gate_decision,
)
from gepase.optimizer.acceptance.policy import AcceptancePolicy, decide_acceptance
from gepase.optimizer.acceptance.validation import ValidationPolicy, run_validation_gate
from gepase.optimizer.candidate import PackageCandidate
from gepase.optimizer.evolution.models import (
    EvolutionCandidateIdentity,
    MergeEligibility,
)
from gepase.optimizer.status import CandidateStatus, transition_candidate
from gepase.package.analyzer import PackageAnalyzer
from gepase.store.artifacts import atomic_write, canonical_json_bytes
from gepase.store.candidates import CandidateStore
from gepase.store.evolution_pool import (
    DeployableFrontierEntry,
    DeployableFrontierStore,
    EvolutionPoolEntry,
    EvolutionPoolStore,
)
from gepase.store.rejected import RejectedEditStore, rejected_record


class GateDecisionStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                decision_id TEXT PRIMARY KEY,
                patch_id TEXT NOT NULL,
                candidate_id TEXT,
                verdict TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.commit()
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        self.connection.close()

    def __enter__(self) -> GateDecisionStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def add(self, decision: GateDecision) -> bool:
        row = self.connection.execute(
            "SELECT payload FROM decisions WHERE decision_id = ?", (decision.decision_id,)
        ).fetchone()
        if row:
            if GateDecision.model_validate_json(row["payload"]) != decision:
                raise ValueError("gate decision id reused with different payload")
            return False
        with self.connection:
            self.connection.execute(
                "INSERT INTO decisions(decision_id, patch_id, candidate_id, verdict, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    decision.decision_id,
                    decision.patch_id,
                    decision.candidate_id,
                    decision.verdict.value,
                    decision.model_dump_json(),
                ),
            )
        return True

    def all(self) -> list[GateDecision]:
        rows = self.connection.execute("SELECT payload FROM decisions ORDER BY rowid").fetchall()
        return [GateDecision.model_validate_json(row["payload"]) for row in rows]


def _not_run(level: GateLevel, reason: str) -> GateResult:
    return GateResult(
        level=level,
        outcome=GateOutcome.NOT_RUN,
        reason_codes=(reason,),
        human_summary=f"{level.value} was skipped because {reason}.",
        checks={"short_circuit": True},
        target_calls=0,
    )


def _sum_usage(gates: tuple[GateResult, ...]) -> GateUsage:
    return GateUsage(
        metric_calls=sum(item.usage.metric_calls for item in gates),
        e2_calls=sum(item.usage.e2_calls for item in gates),
        e3_calls=sum(item.usage.e3_calls for item in gates),
        tokens=sum(item.usage.tokens for item in gates),
        duration_ms=sum(item.usage.duration_ms for item in gates),
    )


class ValidationGatedAcceptance:
    def __init__(self, project_root: Path, run_dir: Path, *, run_id: str) -> None:
        self.project_root = project_root.resolve()
        self.run_dir = run_dir.resolve()
        if not self.run_dir.is_relative_to(self.project_root):
            raise ValueError("acceptance run must be inside project root")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id

    def evaluate(
        self,
        parent: PackageCandidate,
        candidate: PackageCandidate | None,
        patch: PackagePatch,
        application: PatchApplication,
        *,
        train_pairs: tuple[PairedScore, ...] = (),
        validation_pairs: tuple[PairedScore, ...] = (),
        policy_kind: AcceptancePolicyKind = AcceptancePolicyKind.CONSERVATIVE,
        reevaluations_used: int = 0,
        efficiency_regression: float = 0.0,
        complexity_regression: float = 0.0,
        secondary_objective_improvements: dict[str, float] | None = None,
        secondary_evidence_refs: tuple[str, ...] = (),
        validation_policy: ValidationPolicy | None = None,
        minibatch_policy: MinibatchPolicy | None = None,
        static_regression_aware: bool = False,
        evolution_identity: EvolutionCandidateIdentity | None = None,
        ancestor_candidate_ids: tuple[str, ...] = (),
        exclusive_closure_ids: tuple[str, ...] = (),
        record_evolution_candidate: bool = True,
    ) -> GateDecision:
        parent_root = (
            self.run_dir
            / "gate-parent"
            / parent.candidate_id
            / Path(parent.source_package_ref).name
        )
        if not parent_root.exists():
            from gepase.optimizer.materialize import materialize_candidate

            parent_root.parent.mkdir(parents=True, exist_ok=True)
            materialize_candidate(self.project_root, parent, parent_root)
        graph = PackageAnalyzer().analyze(parent_root).graph
        rejected_path = self.run_dir / "rejected.sqlite3"
        with RejectedEditStore(rejected_path) as rejected_store:
            gate0 = run_schema_gate(parent, patch, graph, rejected_store=rejected_store)
        gate1 = (
            run_static_gate(
                self.project_root,
                application,
                baseline_package_root=parent_root if static_regression_aware else None,
            )
            if gate0.outcome is GateOutcome.PASSED
            else _not_run(GateLevel.GATE_1_STATIC, "gate_0_failed")
        )
        train_stats = None
        validation_stats = None
        variance = None
        if gate1.outcome is GateOutcome.PASSED and train_pairs:
            minibatch = run_minibatch_gate(
                train_pairs,
                policy=minibatch_policy or MinibatchPolicy(),
            )
            gate2 = minibatch.gate
            train_stats = minibatch.statistics
        elif gate1.outcome is GateOutcome.PASSED:
            gate2 = _not_run(GateLevel.GATE_2_MINIBATCH, "paired_train_evidence_missing")
        else:
            gate2 = _not_run(GateLevel.GATE_2_MINIBATCH, "early_gate_failed")
        if gate2.outcome is GateOutcome.PASSED and validation_pairs:
            validation = run_validation_gate(
                validation_pairs,
                policy=validation_policy or ValidationPolicy(),
                secondary_objective_improvements=secondary_objective_improvements,
                secondary_evidence_refs=secondary_evidence_refs,
            )
            gate3 = validation.gate
            validation_stats = validation.statistics
            mean_uncertainty = sum(item.uncertainty for item in validation_pairs) / len(
                validation_pairs
            )
            variance = variance_decision(
                validation.statistics,
                mean_uncertainty=mean_uncertainty,
                reevaluations_used=reevaluations_used,
                policy=VariancePolicy(),
            )
        elif gate2.outcome is GateOutcome.PASSED:
            gate3 = _not_run(GateLevel.GATE_3_VALIDATION, "held_out_evidence_missing")
        else:
            gate3 = _not_run(GateLevel.GATE_3_VALIDATION, "early_gate_failed")
        first_four = (gate0, gate1, gate2, gate3)
        policy = decide_acceptance(
            first_four,
            policy=AcceptancePolicy(kind=policy_kind),
            variance=variance,
            efficiency_regression=efficiency_regression,
            complexity_regression=complexity_regression,
        )
        gates = (*first_four, policy.gate_4)
        rejection = None
        if policy.verdict in {
            CandidateStatus.INVALID,
            CandidateStatus.REJECTED,
        }:
            failed = next(
                (
                    item
                    for item in gates
                    if item.outcome is GateOutcome.FAILED
                    and item.level is not GateLevel.GATE_4_FRONTIER
                ),
                policy.gate_4,
            )
            rejection = rejected_record(
                patch,
                parent_candidate_id=parent.candidate_id,
                candidate_id=candidate.candidate_id if candidate else None,
                evidence_refs=tuple(sorted({ref for item in gates for ref in item.evidence_refs})),
                failed_gate=failed.level.value,
                score_delta=(validation_stats.mean_delta if validation_stats else None),
                error_type=policy.verdict.value,
                reason_codes=policy.reason_codes,
            )
        decision = build_gate_decision(
            run_id=self.run_id,
            patch_id=patch.patch_id,
            parent_candidate_id=parent.candidate_id,
            candidate_id=candidate.candidate_id if candidate else None,
            policy=policy_kind,
            verdict=policy.verdict,
            gates=gates,
            train_pairs=train_pairs,
            validation_pairs=validation_pairs,
            train_statistics=train_stats,
            validation_statistics=validation_stats,
            variance_decision=variance,
            reason_codes=policy.reason_codes,
            human_summary=policy.human_summary,
            frontier_eligible=policy.frontier_eligible,
            exploration_pool_eligible=policy.exploration_pool_eligible,
            rejected_record_id=rejection.record_id if rejection else None,
            total_usage=_sum_usage(gates),
            test_access_count=0,
        )
        if rejection is not None:
            rejection = rejection.model_copy(update={"decision_id": decision.decision_id})
            with RejectedEditStore(rejected_path) as rejected_store:
                rejected_store.add(rejection)
        with GateDecisionStore(self.run_dir / "gate-decisions.sqlite3") as decisions:
            decisions.add(decision)
        candidate_store_path = self.run_dir / "candidates.sqlite3"
        with CandidateStore(candidate_store_path) as store:
            store.add_candidate(parent, CandidateStatus.SEED)
            if candidate is not None:
                store.add_candidate(candidate, CandidateStatus.PROPOSED)
                current = CandidateStatus(
                    store.candidate_statuses()[candidate.candidate_id]
                )
                if current is not policy.verdict:
                    event = transition_candidate(
                        candidate.candidate_id,
                        current,
                        policy.verdict,
                        reason_code=policy.reason_codes[0],
                        gate_decision_id=decision.decision_id,
                    )
                    store.set_candidate_status(
                        candidate.candidate_id, policy.verdict, event=event
                    )
            store.save_state(
                {
                    "schema_version": "1.0.0",
                    "run_id": self.run_id,
                    "latest_decision_id": decision.decision_id,
                    "deployable_frontier": [
                        item.candidate_id for item in store.candidates((CandidateStatus.ACCEPTED,))
                    ],
                    "exploration_pool": [
                        item.candidate_id
                        for item in store.candidates((CandidateStatus.INCONCLUSIVE,))
                    ],
                }
            )
            store.write_checkpoint(self.run_dir)
        if record_evolution_candidate and candidate is not None and train_stats is not None:
            local_tasks = tuple(sorted(row.task_id for row in train_pairs if row.delta > 0))
            local_objectives = tuple(
                sorted(
                    key
                    for key, value in (secondary_objective_improvements or {}).items()
                    if value > 0
                )
            )
            structural_pass = all(gate.outcome is GateOutcome.PASSED for gate in (gate0, gate1))
            train_floor = gate2.outcome is GateOutcome.PASSED
            if structural_pass and train_floor and (local_tasks or local_objectives):
                entry = EvolutionPoolEntry(
                    candidate_id=candidate.candidate_id,
                    parent_candidate_id=parent.candidate_id,
                    patch_id=patch.patch_id,
                    package_id=(evolution_identity.package_id if evolution_identity else None),
                    source_package_ref=(
                        evolution_identity.source_package_ref if evolution_identity else None
                    ),
                    source_snapshot_hash=(
                        evolution_identity.source_snapshot_hash if evolution_identity else None
                    ),
                    lineage_root_candidate_id=(
                        evolution_identity.lineage_root_candidate_id if evolution_identity else None
                    ),
                    branch_id=(evolution_identity.branch_id if evolution_identity else None),
                    branch_root_candidate_id=(
                        evolution_identity.branch_root_candidate_id if evolution_identity else None
                    ),
                    failure_cluster_ids=(
                        evolution_identity.failure_cluster_ids if evolution_identity else ()
                    ),
                    ancestor_candidate_ids=(ancestor_candidate_ids if evolution_identity else ()),
                    candidate_content_hash=(
                        evolution_identity.content_hash if evolution_identity else None
                    ),
                    train_evidence_refs=tuple(
                        sorted(
                            {
                                *(item.parent_record_id for item in train_pairs),
                                *(item.candidate_record_id for item in train_pairs),
                            }
                        )
                    ),
                    exclusive_task_keys=local_tasks,
                    exclusive_objective_keys=local_objectives,
                    exclusive_component_ids=patch.selected_node_ids,
                    exclusive_closure_ids=exclusive_closure_ids,
                    train_mean_delta=train_stats.mean_delta,
                    train_floor_satisfied=train_floor,
                    gate_0_1_passed=structural_pass,
                    merge_eligibility=(
                        MergeEligibility.ELIGIBLE
                        if evolution_identity
                        else MergeEligibility.UNKNOWN_LEGACY
                    ),
                )
                with EvolutionPoolStore(self.run_dir / "evolution-pool.sqlite3") as pool:
                    pool.add(entry)
                    pool.snapshot(self.run_dir / "evolution-pool.json")
        if (
            candidate is not None
            and policy.verdict is CandidateStatus.ACCEPTED
            and validation_pairs
        ):
            entry = DeployableFrontierEntry(
                candidate_id=candidate.candidate_id,
                decision_id=decision.decision_id,
                validation_evidence_refs=tuple(
                    sorted(
                        {
                            *(item.parent_record_id for item in validation_pairs),
                            *(item.candidate_record_id for item in validation_pairs),
                            *secondary_evidence_refs,
                        }
                    )
                ),
            )
            with DeployableFrontierStore(
                self.run_dir / "deployable-frontier.sqlite3"
            ) as frontier_store:
                frontier_store.add(entry)
                frontier_store.snapshot(self.run_dir / "deployable-frontier.json")
        decision_dir = self.run_dir / "gate-decisions"
        decision_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(
            decision_dir / f"{decision.decision_id}.json",
            canonical_json_bytes(decision.model_dump(mode="json")),
        )
        return decision

    def audit(self) -> dict[str, Any]:
        with GateDecisionStore(self.run_dir / "gate-decisions.sqlite3") as store:
            decisions = store.all()
        accepted = [item for item in decisions if item.verdict is CandidateStatus.ACCEPTED]
        required = {
            GateLevel.GATE_0_SCHEMA,
            GateLevel.GATE_1_STATIC,
            GateLevel.GATE_2_MINIBATCH,
            GateLevel.GATE_3_VALIDATION,
        }
        accepted_complete = all(
            required <= {gate.level for gate in item.gates if gate.outcome is GateOutcome.PASSED}
            for item in accepted
        )
        missing = sum(not item.gates for item in decisions)
        test_access = sum(item.test_access_count for item in decisions)
        return {
            "schema_version": "1.0.0",
            "valid": bool(decisions) and accepted_complete and missing == 0 and test_access == 0,
            "decisions": len(decisions),
            "accepted": len(accepted),
            "rejected": sum(item.verdict is CandidateStatus.REJECTED for item in decisions),
            "invalid": sum(item.verdict is CandidateStatus.INVALID for item in decisions),
            "inconclusive": sum(item.verdict is CandidateStatus.INCONCLUSIVE for item in decisions),
            "accepted_gates_complete": accepted_complete,
            "missing_decision": missing,
            "test_access": test_access,
            "rows": [item.model_dump(mode="json") for item in decisions],
        }

    def write_audit(self, output: Path) -> dict[str, Any]:
        result = self.audit()
        atomic_write(output, canonical_json_bytes(result))
        return result
