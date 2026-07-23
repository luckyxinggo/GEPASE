from __future__ import annotations

from pathlib import Path

from gepase.mutation.proposer import (
    PatchProposalStore,
    PatchProposalWorkItem,
    PatchTargetSnapshot,
    build_patch_submission,
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
