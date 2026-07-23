"""Deterministic S7 fault, regression, variance, and rejection-memory gates."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from gepase.evals.statistics import PairedScore, paired_statistics
from gepase.evals.variance import VarianceAction, VariancePolicy, variance_decision
from gepase.mutation.applier import apply_package_patch
from gepase.mutation.proposer import (
    PatchProposalStore,
    inject_rejected_history,
)
from gepase.mutation.schema import PatchEditBudget, package_patch_from_proposal
from gepase.mutation.validators.schema_gate import run_schema_gate
from gepase.optimizer.acceptance.models import (
    AcceptancePolicyKind,
    GateLevel,
    GateOutcome,
    GateResult,
)
from gepase.optimizer.acceptance.policy import AcceptancePolicy, decide_acceptance
from gepase.optimizer.acceptance.validation import ValidationPolicy, run_validation_gate
from gepase.optimizer.candidate import PackageCandidate, build_seed_candidate
from gepase.optimizer.status import CandidateStatus
from gepase.package.analyzer import PackageAnalyzer
from gepase.package.ir import IRNode, NodeKind
from gepase.store.rejected import RejectedEditStore, rejected_record

ROOT_PACKAGE = "benchmarks/skills/structured-report-builder"


def _node(graph: object, kind: NodeKind, *, path: str) -> IRNode:
    from gepase.package.ir import PackageGraph

    typed = graph if isinstance(graph, PackageGraph) else PackageGraph.model_validate(graph)
    return next(item for item in typed.nodes if item.kind is kind and item.path == path)


def _patch(
    parent: PackageCandidate,
    node: IRNode,
    replacement: str,
    *,
    work_id: str,
    operation: str,
    precondition: str | None = None,
) -> object:
    return package_patch_from_proposal(
        {
            "proposal_work_id": work_id,
            "base_candidate_id": parent.candidate_id,
            "base_snapshot_hash": parent.snapshot_hash,
            "base_content_hash": parent.content_hash,
            "selector": "graph_guided",
            "selected_node_ids": [node.node_id],
            "operations": [
                {
                    "operation_id": f"op-{work_id}",
                    "op": operation,
                    "target_node_id": node.node_id,
                    "path": node.path,
                    "precondition_hash": precondition or node.content_hash,
                    "replacement": replacement,
                    "evidence_refs": [f"fault:{work_id}"],
                    "expected_benefit": "Exercise an early validation failure.",
                    "regression_risk": "high",
                    "rationale": "Pre-registered Gate fault injection.",
                }
            ],
            "edit_budget": PatchEditBudget(max_operations=1, max_changed_files=1),
            "evidence_refs": [f"fault:{work_id}"],
            "summary": f"Gate fault {work_id}.",
        }
    )


def early_gate_fault_suite(project_root: Path) -> dict[str, Any]:
    root = project_root.resolve()
    parent = build_seed_candidate(root, ROOT_PACKAGE, run_id="s7-gate-faults")
    graph = PackageAnalyzer().analyze(root / ROOT_PACKAGE).graph
    instruction = _node(graph, NodeKind.INSTRUCTION, path="SKILL.md")
    function = _node(graph, NodeKind.FUNCTION, path="scripts/render_report.py")
    cases: list[dict[str, Any]] = []
    invalid_raw = {
        "proposal_work_id": "invalid-path",
        "base_candidate_id": parent.candidate_id,
        "base_snapshot_hash": parent.snapshot_hash,
        "base_content_hash": parent.content_hash,
        "selector": "graph_guided",
        "selected_node_ids": [instruction.node_id],
        "operations": [
            {
                "operation_id": "op-invalid-path",
                "op": "replace_markdown_block",
                "target_node_id": instruction.node_id,
                "path": "../../outside",
                "precondition_hash": instruction.content_hash,
                "replacement": "invalid",
                "evidence_refs": ["fault:path"],
                "expected_benefit": "none",
                "regression_risk": "high",
                "rationale": "fault",
            }
        ],
        "edit_budget": PatchEditBudget(max_operations=1, max_changed_files=1),
        "evidence_refs": ["fault:path"],
        "summary": "invalid path",
    }
    try:
        package_patch_from_proposal(invalid_raw)
        invalid_rejected = False
    except (ValidationError, ValueError):
        invalid_rejected = True
    cases.append(
        {
            "fault": "path",
            "early_rejected": invalid_rejected,
            "failed_gate": "schema_construction",
            "target_calls_after_reject": 0,
        }
    )
    stale = _patch(
        parent,
        instruction,
        "- stale precondition\n",
        work_id="stale",
        operation="replace_markdown_block",
        precondition="0" * 64,
    )
    assert hasattr(stale, "patch_id")
    stale_gate = run_schema_gate(parent, stale, graph)  # type: ignore[arg-type]
    cases.append(
        {
            "fault": "schema_precondition",
            "early_rejected": stale_gate.outcome is GateOutcome.FAILED,
            "failed_gate": stale_gate.level.value,
            "target_calls_after_reject": 0,
        }
    )
    static_faults = (
        (
            "syntax",
            function,
            "def broken(:\n    pass\n",
            "replace_python_function",
        ),
        (
            "reference",
            instruction,
            "- Read [missing](references/definitely-missing.md).\n",
            "replace_markdown_block",
        ),
        (
            "security",
            function,
            "def text(value):\n    import os\n    os.system(value)\n    return value\n",
            "replace_python_function",
        ),
    )
    local = root / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    for fault, target, replacement, operation in static_faults:
        patch = _patch(
            parent,
            target,
            replacement,
            work_id=fault,
            operation=operation,
        )
        with tempfile.TemporaryDirectory(prefix=f"s7-{fault}-", dir=local) as temporary:
            application, child = apply_package_patch(
                root,
                parent,
                patch,  # type: ignore[arg-type]
                Path(temporary),
                run_id=f"s7-{fault}",
            )
            from gepase.mutation.validators.static_gate import run_static_gate

            static_gate = run_static_gate(root, application)
            cases.append(
                {
                    "fault": fault,
                    "candidate_constructed": child is not None,
                    "early_rejected": static_gate.outcome is GateOutcome.FAILED,
                    "failed_gate": static_gate.level.value,
                    "reason_codes": static_gate.reason_codes,
                    "target_calls_after_reject": 0,
                }
            )
    rejected = sum(bool(item["early_rejected"]) for item in cases)
    target_calls = sum(int(item["target_calls_after_reject"]) for item in cases)
    return {
        "schema_version": "1.0.0",
        "valid": rejected == len(cases) and target_calls == 0,
        "faults": len(cases),
        "early_rejected": rejected,
        "early_reject_rate": rejected / len(cases),
        "target_calls_after_reject": target_calls,
        "rows": cases,
    }


def regression_floor_diagnostic() -> dict[str, Any]:
    rows = (
        PairedScore(
            task_id="validation-normal-1",
            category="normal",
            risk_level="low",
            parent_score=0.5,
            candidate_score=0.65,
            evidence_tier="E3",
            minimum_acceptance_tier="E3",
            parent_record_id="parent-1",
            candidate_record_id="candidate-1",
        ),
        PairedScore(
            task_id="validation-normal-2",
            category="normal",
            risk_level="low",
            parent_score=0.5,
            candidate_score=0.65,
            evidence_tier="E3",
            minimum_acceptance_tier="E3",
            parent_record_id="parent-2",
            candidate_record_id="candidate-2",
        ),
        PairedScore(
            task_id="validation-critical",
            category="critical",
            risk_level="critical",
            parent_score=1.0,
            candidate_score=0.9,
            evidence_tier="E3",
            minimum_acceptance_tier="E3",
            parent_record_id="parent-3",
            candidate_record_id="candidate-3",
        ),
    )
    validation = run_validation_gate(rows, policy=ValidationPolicy())

    def pass_gate(level: GateLevel) -> GateResult:
        return GateResult(
            level=level,
            outcome=GateOutcome.PASSED,
            reason_codes=("fixture_pass",),
            human_summary="Fixture gate passed.",
        )

    verdict = decide_acceptance(
        (
            pass_gate(GateLevel.GATE_0_SCHEMA),
            pass_gate(GateLevel.GATE_1_STATIC),
            pass_gate(GateLevel.GATE_2_MINIBATCH),
            validation.gate,
        ),
        policy=AcceptancePolicy(kind=AcceptancePolicyKind.CONSERVATIVE),
    )
    return {
        "schema_version": "1.0.0",
        "valid": (
            validation.statistics.mean_delta > 0
            and validation.gate.outcome is GateOutcome.FAILED
            and verdict.verdict is CandidateStatus.REJECTED
            and not verdict.frontier_eligible
            and "protected_objective_regression" in validation.gate.reason_codes
        ),
        "overall_delta": validation.statistics.mean_delta,
        "category_deltas": validation.category_deltas,
        "verdict": verdict.verdict.value,
        "frontier_contains": verdict.frontier_eligible,
        "reason_code": validation.gate.reason_codes[0],
    }


def variance_policy_diagnostic() -> dict[str, Any]:
    rows = tuple(
        PairedScore(
            task_id=f"validation-{index}",
            category="stochastic",
            risk_level="medium",
            parent_score=0.0 if index % 2 == 0 else 1.0,
            candidate_score=1.0 if index % 2 == 0 else 0.0,
            evidence_tier="E2",
            minimum_acceptance_tier="E2",
            parent_record_id=f"parent-{index}",
            candidate_record_id=f"candidate-{index}",
            uncertainty=0.5,
        )
        for index in range(8)
    )
    statistics = paired_statistics(rows, seed=42, bootstrap_samples=2_000)
    policy = VariancePolicy(max_reevaluations=2)
    initial = variance_decision(
        statistics, mean_uncertainty=0.5, reevaluations_used=0, policy=policy
    )
    exhausted = variance_decision(
        statistics, mean_uncertainty=0.5, reevaluations_used=2, policy=policy
    )
    return {
        "schema_version": "1.0.0",
        "valid": (
            initial.action is VarianceAction.REEVALUATE
            and exhausted.action is VarianceAction.EXHAUSTED_INCONCLUSIVE
        ),
        "initial": initial.model_dump(mode="json"),
        "exhausted": exhausted.model_dump(mode="json"),
        "statistics": statistics.model_dump(mode="json"),
        "final_verdict": "inconclusive",
    }


def rejected_memory_diagnostic(project_root: Path, mutation_run: Path) -> dict[str, Any]:
    parent = PackageCandidate.model_validate_json(
        (mutation_run / "parent-candidate.json").read_text(encoding="utf-8")
    )
    with PatchProposalStore(mutation_run / "proposals.sqlite3") as proposal_store:
        submission = next(item for item in proposal_store.submissions() if item.patch is not None)
        work = proposal_store.get_work(submission.work_id)
    assert submission.patch is not None
    record = rejected_record(
        submission.patch,
        parent_candidate_id=parent.candidate_id,
        candidate_id=None,
        evidence_refs=tuple(submission.patch.evidence_refs),
        failed_gate="gate_3_validation",
        score_delta=-0.1,
        error_type="rejected",
        reason_codes=("held_out_primary_regression",),
    )
    local = project_root / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="s7-rejected-", dir=local) as temporary:
        store_path = Path(temporary) / "rejected.sqlite3"
        with RejectedEditStore(store_path) as store:
            store.add(record)
            before = hashlib.sha256(record.model_dump_json().encode()).hexdigest()
            injected = inject_rejected_history(work, store)
            graph = PackageAnalyzer().analyze(mutation_run / "parent/package").graph
            gate = run_schema_gate(parent, submission.patch, graph, rejected_store=store)
            after_record = store.exact(submission.patch.fingerprint, parent.candidate_id)
            assert after_record is not None
            after = hashlib.sha256(after_record.model_dump_json().encode()).hexdigest()
    return {
        "schema_version": "1.0.0",
        "valid": (
            gate.outcome is GateOutcome.FAILED
            and "rejected_patch_repetition" in gate.reason_codes
            and bool(injected.rejected_history)
            and before == after
        ),
        "blocked_or_warned": gate.outcome is GateOutcome.FAILED,
        "history_injected": bool(injected.rejected_history),
        "record_hash_before": before,
        "record_hash_after": after,
        "record_immutable": before == after,
        "gate": gate.model_dump(mode="json"),
        "injected_history": injected.rejected_history,
    }
