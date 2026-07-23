from __future__ import annotations

from pathlib import Path

from gepase.mutation.schema import PatchEditBudget, package_patch_from_proposal
from gepase.store.rejected import RejectedEditStore, rejected_record


def _patch():
    return package_patch_from_proposal(
        {
            "proposal_work_id": "work-rejected",
            "base_candidate_id": "candidate-parent",
            "base_snapshot_hash": "1" * 64,
            "base_content_hash": "2" * 64,
            "selector": "graph_guided",
            "selected_node_ids": ["node-1"],
            "operations": [
                {
                    "operation_id": "op-rejected",
                    "op": "replace_markdown_block",
                    "target_node_id": "node-1",
                    "path": "SKILL.md",
                    "precondition_hash": "3" * 64,
                    "replacement": "- rejected edit\n",
                    "evidence_refs": ["record:1"],
                    "expected_benefit": "test",
                    "regression_risk": "low",
                    "rationale": "test",
                }
            ],
            "edit_budget": PatchEditBudget(max_operations=1, max_changed_files=1),
            "evidence_refs": ["record:1"],
            "summary": "Rejected test patch.",
        }
    )


def test_rejected_store_is_immutable_and_searchable(tmp_path: Path) -> None:
    patch = _patch()
    record = rejected_record(
        patch,
        parent_candidate_id="candidate-parent",
        candidate_id="candidate-child",
        evidence_refs=("record:1",),
        failed_gate="gate_3_validation",
        score_delta=-0.1,
        error_type="rejected",
        reason_codes=("held_out_primary_regression",),
    )
    with RejectedEditStore(tmp_path / "rejected.sqlite3") as store:
        assert store.add(record)
        assert not store.add(record)
        assert store.exact(patch.fingerprint, "candidate-parent") == record
        assert store.relevant(("node-1",)) == [record]
