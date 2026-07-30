from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from gepase.optimizer.merge.models import MergeOutcome, MergeOutcomeStatus
from gepase.reporting.outcome import (
    CandidateOutcomeRow,
    EffectOutcome,
    EvolutionOutcomeCompiler,
    EvolutionOutcomeReportBuilder,
    EvolutionOutcomeReportConfig,
    EvolutionOutcomeReportInput,
    FrontierReportEntry,
    ReportEvidenceAsset,
)
from gepase.store.artifacts import sha256_bytes

ROOT = Path(__file__).resolve().parents[2]
OBJECTIVES = {
    "task_correctness": 0.1,
    "output_quality": 0.1,
    "skill_gain": 0.1,
    "reliability": 0.1,
    "efficiency": 0.1,
    "package_quality": 0.1,
}


def _merge() -> MergeOutcome:
    return MergeOutcome(
        status=MergeOutcomeStatus.NO_ELIGIBLE_PARENT_SET,
        considered_parent_candidate_ids=("candidate-a",),
        considered_parent_set_count=0,
        eligible_parent_set_count=0,
        rejected_parent_set_count=0,
        rejection_reason_counts={"insufficient_parents": 1},
        cross_package_pair_count=0,
        enumeration_ref="artifacts/local/fixture/merge-enumeration.json",
    )


def _candidate(candidate_id: str) -> CandidateOutcomeRow:
    return CandidateOutcomeRow(
        candidate_id=candidate_id,
        parent_ids=("seed",),
        patch_refs=(f"artifacts/local/fixture/{candidate_id}-patch.json",),
        graph_path_refs=(f"artifacts/local/fixture/{candidate_id}-graph.json",),
        train_mean_delta=0.02,
        validation_mean_delta=0.01,
        train_objective_deltas=OBJECTIVES,
        validation_objective_deltas=OBJECTIVES,
        validation_wins=3,
        gate_status="accepted",
    )


@pytest.mark.parametrize(
    ("outcome", "frontier_count", "search_complete"),
    [
        (EffectOutcome.NO_STRICT_IMPROVEMENT, 0, True),
        (EffectOutcome.STRICT_IMPROVEMENT, 1, True),
        (EffectOutcome.STRICT_IMPROVEMENT, 2, True),
        (EffectOutcome.BUDGET_INCOMPLETE, 1, False),
    ],
)
def test_zero_one_many_and_budget_incomplete_reports_are_verifiable(
    outcome: EffectOutcome,
    frontier_count: int,
    search_complete: bool,
) -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="outcome-report-", dir=local) as temporary:
        root = Path(temporary)
        candidate_ids = tuple(f"candidate-{index}" for index in range(frontier_count))
        frontier: list[FrontierReportEntry] = []
        for candidate_id in candidate_ids:
            package = root / "packages" / candidate_id
            package.mkdir(parents=True)
            (package / "SKILL.md").write_text(f"# {candidate_id}\n", encoding="utf-8")
            package_ref = package.relative_to(ROOT).as_posix()
            frontier.append(
                FrontierReportEntry(
                    candidate_id=candidate_id,
                    package_ref=package_ref,
                    provisional=not search_complete,
                    lineage_refs=(f"artifacts/local/fixture/{candidate_id}-lineage.json",),
                    patch_refs=(f"artifacts/local/fixture/{candidate_id}-patch.json",),
                    validation_summary_ref=(
                        f"artifacts/local/fixture/{candidate_id}-validation.json"
                    ),
                    gate_decision_ref=f"artifacts/local/fixture/{candidate_id}-gate.json",
                )
            )
        rows = tuple(_candidate(candidate_id) for candidate_id in candidate_ids)
        if not rows:
            rows = (
                _candidate("candidate-rejected").model_copy(
                    update={
                        "validation_mean_delta": -0.01,
                        "validation_wins": 0,
                        "validation_losses": 3,
                        "gate_status": "rejected",
                        "rejection_reasons": ("validation_regression",),
                    }
                ),
            )
        report_input = EvolutionOutcomeReportInput(
            run_id="fixture-run",
            package_id="slack-gif-creator",
            outcome=outcome,
            search_complete=search_complete,
            reference_summary={"fresh": True},
            candidates=rows,
            deployable_frontier=tuple(frontier),
            merge_outcome=_merge(),
            gate_funnel={"proposed": len(rows), "deployable": frontier_count},
            rejected_memory_refs=("artifacts/local/fixture/rejected.sqlite3",),
            runtime={"active_wall_clock_ms": 1000, "paused_ms": 2000},
            budget_checkpoint_refs=("artifacts/local/fixture/checkpoint.json",),
            continuation_decision_refs=("artifacts/local/fixture/decision.json",),
            pending_work_ids=("pending-work",) if not search_complete else (),
            provenance={"fixture": True},
        )
        input_path = root / "effect-outcome-report-input.json"
        input_path.write_text(report_input.model_dump_json(indent=2), encoding="utf-8")
        config = EvolutionOutcomeReportConfig(
            report_id="fixture-report",
            title_zh="GEPASE 多结局报告验证",
            package_id="slack-gif-creator",
            outcome_input_ref=input_path.relative_to(ROOT).as_posix(),
        )
        config_path = root / "report-config.json"
        config_path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
        output = root / "report"
        builder = EvolutionOutcomeReportBuilder.from_config(ROOT, config_path)
        built = builder.build(output)
        verified = builder.verify(output)

        assert built["frontier_count"] == frontier_count
        assert verified["valid"], verified
        assert len(tuple((output / "packages").glob("*.zip"))) == frontier_count
        html = (output / "index.html").read_text(encoding="utf-8")
        assert all(
            f'id="{section}"' in html
            for section in ("outcome", "candidates", "merge", "runtime", "deployable")
        )
        if frontier_count == 0:
            assert "没有可部署 Package 归档" in html
        if not search_complete:
            assert "provisional evidence" in html


def test_report_config_requires_exactly_one_existing_outcome_input() -> None:
    config = EvolutionOutcomeReportConfig(
        report_id="fixture",
        title_zh="fixture",
        package_id="slack-gif-creator",
        outcome_input_refs=("missing-a.json", "missing-b.json"),
    )
    with pytest.raises(ValueError, match="exactly one"):
        EvolutionOutcomeReportBuilder(ROOT, config).collect()


def test_six_dimensional_candidate_vector_is_required_when_present() -> None:
    with pytest.raises(ValueError, match="six objectives"):
        CandidateOutcomeRow(
            candidate_id="candidate",
            parent_ids=("seed",),
            patch_refs=(),
            graph_path_refs=(),
            train_objective_deltas={"task_correctness": 0.1},
            gate_status="rejected",
        )


def test_outcome_compiler_reads_typed_paired_delta_without_legacy_delta(tmp_path) -> None:
    run_dir = tmp_path / "run"
    split_dir = run_dir / "evals/candidate-a/train"
    split_dir.mkdir(parents=True)
    reference = {key: 0.5 for key in OBJECTIVES}
    candidate = {key: 0.6 for key in OBJECTIVES}
    (tmp_path / "reference.json").write_text(json.dumps(reference), encoding="utf-8")
    (tmp_path / "candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
    (split_dir / "paired-scores.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "candidate_score": 0.8,
                        "parent_score": 0.7,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (split_dir / "candidate-run-summary.json").write_text(
        json.dumps(
            {
                "pair_summaries": [
                    {
                        "paired_delta": 0.1,
                        "candidate_vector_ref": "candidate.json",
                        "reference_vector_ref": "reference.json",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    mean_delta, objective_deltas, counts = EvolutionOutcomeCompiler(
        tmp_path, run_dir
    )._split_metrics("candidate-a", "train")

    assert mean_delta == pytest.approx(0.1)
    assert objective_deltas == pytest.approx({key: 0.1 for key in OBJECTIVES})
    assert counts == (1, 0, 0)


def test_report_copies_hash_bound_task_native_gif_into_seal() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="outcome-gallery-", dir=local) as temporary:
        root = Path(temporary)
        gif = root / "source.gif"
        gif.write_bytes(b"GIF89a\x01\x00\x01\x00")
        asset = ReportEvidenceAsset(
            asset_id="gif-fixture",
            task_id="functional-validation-fixture-001",
            split="validation",
            variant="candidate",
            candidate_id="candidate-rejected",
            execution_status="completed",
            source_ref=gif.relative_to(ROOT).as_posix(),
            sha256=sha256_bytes(gif.read_bytes()),
            media_type="image/gif",
            size_bytes=gif.stat().st_size,
            label_zh="验证候选 GIF",
        )
        report_input = EvolutionOutcomeReportInput(
            run_id="fixture-run",
            package_id="slack-gif-creator",
            outcome=EffectOutcome.NO_STRICT_IMPROVEMENT,
            search_complete=True,
            reference_summary={"fresh": True},
            candidates=(_candidate("candidate-rejected"),),
            deployable_frontier=(),
            merge_outcome=_merge(),
            gate_funnel={"proposed": 1, "deployable": 0},
            rejected_memory_refs=(),
            runtime={"usage": {"agent_calls": 1}},
            evidence_gallery=(asset,),
            process_evidence={"package_graph": {"layer_counts": {"observed": 1}}},
            provenance={"fixture": True},
        )
        input_path = root / "effect-outcome-report-input.json"
        input_path.write_text(report_input.model_dump_json(indent=2), encoding="utf-8")
        config = EvolutionOutcomeReportConfig(
            report_id="fixture-gallery",
            title_zh="证据画廊",
            package_id="slack-gif-creator",
            outcome_input_ref=input_path.relative_to(ROOT).as_posix(),
        )
        config_path = root / "report-config.json"
        config_path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
        output = root / "report"
        builder = EvolutionOutcomeReportBuilder.from_config(ROOT, config_path)

        built = builder.build(output)
        verified = builder.verify(output)

        assert built["artifacts"]["checked"] == 4
        assert verified["valid"] is True
        copied = next((output / "evidence/gifs").glob("*.gif"))
        assert copied.read_bytes() == gif.read_bytes()
        html = (output / "index.html").read_text(encoding="utf-8")
        assert 'id="evidence"' in html
        assert "functional-validation-fixture-001" in html
