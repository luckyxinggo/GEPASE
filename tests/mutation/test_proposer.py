from __future__ import annotations

from pathlib import Path

import pytest

from gepase.mutation.proposer import (
    PatchProposalStore,
    PatchProposalWorkItem,
    PatchTargetSnapshot,
    ProposalWorkStatus,
    build_failed_patch_submission,
    build_patch_submission,
    proposal_accounting_increments,
)
from gepase.mutation.schema import PatchEditBudget, PatchOperationKind
from gepase.optimizer.selectors import (
    FeatureContribution,
    RankedSelection,
)


def _work() -> PatchProposalWorkItem:
    selection = RankedSelection(
        rank=1,
        node_id="node-test",
        path="SKILL.md",
        locator="section/test",
        score=1.0,
        contributions=(
            FeatureContribution(
                feature="failure_coverage", raw_value=1.0, weight=1.0, contribution=1.0
            ),
        ),
        evidence_refs=("record:test",),
        reason_code="graph_failure_impact_priority",
    )
    return PatchProposalWorkItem(
        work_id="patch-work-test",
        run_id="s6-test",
        task_id="task-test",
        parent_candidate_id="candidate-test",
        parent_snapshot_hash="1" * 64,
        parent_content_hash="2" * 64,
        selector="graph_guided",
        targets=(
            PatchTargetSnapshot(
                node_id="node-test",
                node_kind="instruction",
                path="SKILL.md",
                locator="section/test",
                content_hash="3" * 64,
                content="- Existing instruction.\n",
                selection=selection,
            ),
        ),
        allowed_operations=(PatchOperationKind.REPLACE_MARKDOWN_BLOCK,),
        edit_budget=PatchEditBudget(max_operations=1, max_changed_files=1),
        evidence_refs=("record:test",),
        actionable_side_information={"failure": "ambiguous instruction"},
        output_instructions="Return JSON only.",
    )


def test_proposal_store_resume_and_idempotent_ingest(tmp_path: Path) -> None:
    work = _work()
    with PatchProposalStore(tmp_path / "proposals.sqlite3") as store:
        assert store.add_work(work)
        assert not store.add_work(work)
        assert store.next_work() == work
        assert store.resume() == 1
        assert store.next_work() == work
        submission = build_patch_submission(
            work,
            {
                "summary": "Clarify the bounded instruction.",
                "operations": [
                    {
                        "operation_id": "op-clarify",
                        "op": "replace_markdown_block",
                        "target_node_id": "node-test",
                        "path": "SKILL.md",
                        "precondition_hash": "3" * 64,
                        "replacement": "- Clarified instruction.\n",
                        "evidence_refs": ["record:test"],
                        "expected_benefit": "Remove ambiguity.",
                        "regression_risk": "low",
                        "rationale": "Directly addresses the supplied failure.",
                    }
                ],
            },
            host="codex",
            model="agent-host",
            host_task_id="task-agent-1",
            duration_ms=10,
            token_estimate=100,
        )
        assert store.ingest(submission)
        assert not store.ingest(submission)
        assert store.status()["completed"] == 1


def test_failed_proposal_is_preserved_before_one_bounded_repair(tmp_path: Path) -> None:
    work = _work()
    with PatchProposalStore(tmp_path / "proposals.sqlite3") as store:
        assert store.add_work(work)
        assert store.next_work() == work
        failed = build_failed_patch_submission(
            work,
            host="codex",
            model="agent-host",
            host_task_id="task-agent-invalid-json",
            duration_ms=10,
            token_estimate=100,
            failure_kind="submission_validation_failure",
            failure_detail="raw response used operation instead of op",
        )
        assert failed.status is ProposalWorkStatus.FAILED
        assert store.ingest(failed)
        repair = store.plan_repair(work.work_id, max_repair_attempts=1)
        assert repair.work_id == "patch-work-test-repair-1"
        assert repair.repair_attempt == 1
        assert repair.rejected_history[-1]["failed_work_id"] == work.work_id
        assert proposal_accounting_increments(work) == (1, 0)
        assert proposal_accounting_increments(repair) == (0, 1)
        assert store.next_work() == repair
        assert store.ingest(
            build_failed_patch_submission(
                repair,
                host="codex",
                model="agent-host",
                host_task_id="task-agent-repair-invalid-json",
                duration_ms=10,
                token_estimate=100,
                failure_kind="submission_validation_failure",
                failure_detail="repair raw response is still invalid",
            )
        )
        with pytest.raises(ValueError, match="proposal repair budget"):
            store.plan_repair(repair.work_id, max_repair_attempts=1)
