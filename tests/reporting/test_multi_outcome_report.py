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
    build_outcome_presentation,
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
        rows = tuple(
            _candidate(candidate_id).model_copy(
                update={
                    "relative_efficiency": {
                        "relative_cost_ratio": 1.0,
                        "relative_efficiency_score": 0.5,
                        "availability": "comparable",
                    },
                    "pareto_layer": 1,
                    "display_rank": index,
                }
            )
            for index, candidate_id in enumerate(candidate_ids, 1)
        )
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
            policy_evaluation={
                "policy_id": "relative_efficiency_v2",
                "policy_hash": "a" * 64,
                "max_relative_cost_ratio": 2.0,
            },
            frontier_ranking={
                "schema_version": "2.0.0",
                "ranks": [
                    {
                        "candidate_id": candidate_id,
                        "pareto_layer": 1,
                        "display_rank": index,
                    }
                    for index, candidate_id in enumerate(candidate_ids, 1)
                ],
            },
            provenance={"fixture": True},
        )
        input_path = root / "effect-outcome-report-input.json"
        input_path.write_text(report_input.model_dump_json(indent=2), encoding="utf-8")
        config = EvolutionOutcomeReportConfig(
            schema_version="1.0.0",
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
            for section in (
                "outcome",
                "efficiency",
                "candidates",
                "merge",
                "runtime",
                "deployable",
            )
        )
        assert "v1 绝对预算诊断" in html
        if search_complete:
            assert "零 Agent policy replay" in html
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


def test_narrative_projection_is_generic_and_fail_closed_for_missing_evidence() -> None:
    candidates = [
        {
            **_candidate("candidate-alpha").model_dump(mode="json"),
            "gate_status": "validation_evidence_incomplete",
            "validation_mean_delta": None,
            "validation_evidence_status": "evidence_incomplete",
            "rejection_reasons": ["validation_evidence_incomplete"],
        },
        {
            **_candidate("candidate-beta").model_dump(mode="json"),
            "parent_ids": ["candidate-alpha"],
            "gate_status": "train_rejected",
            "validation_mean_delta": None,
            "validation_evidence_status": "not_run",
            "rejection_reasons": ["minibatch_regression"],
        },
    ]
    presentation = build_outcome_presentation(
        {
            "outcome": "no_strict_improvement",
            "claim_boundary_zh": "通用 fixture",
            "candidates": candidates,
            "deployable_frontier": [],
            "gate_funnel": {
                "proposed": 2,
                "validation_evidence_incomplete": 1,
                "deployable": 0,
            },
            "evidence_gallery": [],
            "process_evidence": {},
            "runtime": {},
        }
    )

    first, second = presentation["candidates"]
    assert first["alias_zh"] == "第1代 A"
    assert second["alias_zh"] == "第2代 A"
    assert second["parent_aliases_zh"] == ["第1代 A"]
    assert first["status_zh"] == "验证证据不完整"
    assert first["relative_efficiency"]["availability"] == "unavailable"
    assert first["graph"]["available"] is False
    assert first["operation_count"] == 0
    assert presentation["tasks"] == []
    assert presentation["headline"]["deployable"] == 0


def test_narrative_projection_derives_merge_patch_graph_and_stable_ranking() -> None:
    first = _candidate("candidate-alpha").model_copy(
        update={
            "display_rank": 1,
            "pareto_layer": 1,
            "relative_efficiency": {
                "availability": "comparable",
                "relative_cost_ratio": 1.25,
                "relative_efficiency_score": 0.444,
                "axis_aggregates": [
                    {
                        "axis": "tokens",
                        "median_ratio": None,
                        "task_ratios": {},
                        "excluded_tasks": {"task-a": "unavailable"},
                    }
                ],
            },
        }
    )
    second = _candidate("candidate-beta").model_copy(
        update={"display_rank": 2, "pareto_layer": 2}
    )
    merge = _candidate("candidate-merge").model_copy(
        update={
            "parent_ids": ("candidate-alpha", "candidate-beta"),
            "gate_status": "rejected",
            "rejection_reasons": ("protected_objective_regression",),
        }
    )
    patch_ref = merge.patch_refs[0]
    presentation = build_outcome_presentation(
        {
            "outcome": "strict_improvement",
            "claim_boundary_zh": "通用 fixture",
            "candidates": [
                first.model_dump(mode="json"),
                second.model_dump(mode="json"),
                merge.model_dump(mode="json"),
            ],
            "deployable_frontier": [
                {"candidate_id": "candidate-alpha"},
                {"candidate_id": "candidate-beta"},
            ],
            "gate_funnel": {"proposed": 3, "deployable": 2},
            "evidence_gallery": [],
            "policy_evaluation": {"max_relative_cost_ratio": 2.0},
            "process_evidence": {
                "patches": [
                    {
                        "patch_ref": patch_ref,
                        "summary": "通用合并修改",
                        "operations": [
                            {
                                "op": "replace_markdown_block",
                                "path": "GUIDE.md",
                                "target_node_id": "node-generic",
                                "evidence_refs": ["evidence/reflection.json"],
                            }
                        ],
                    }
                ],
                "package_graph": {
                    "bindings": [
                        {
                            "parent_candidate_id": "candidate-alpha",
                            "mapped_access_events": 2,
                            "layer_counts": {"static": 4, "observed": 2},
                        }
                    ]
                },
            },
            "runtime": {"usage": {"agent_calls": 3}},
        }
    )

    merge_view = next(
        row for row in presentation["candidates"] if row["candidate_id"] == "candidate-merge"
    )
    assert merge_view["alias_zh"] == "合并候选"
    assert merge_view["operator_zh"] == "同 Package 多父 Merge"
    assert merge_view["modified_files"] == ["GUIDE.md"]
    assert merge_view["graph"]["mapped_access_events"] == 2
    assert presentation["headline"]["first"]["candidate_id"] == "candidate-alpha"
    assert presentation["headline"]["second"]["candidate_id"] == "candidate-beta"


def test_narrative_report_mode_keeps_machine_json_folded_and_html_accessible() -> None:
    local = ROOT / "artifacts/local"
    local.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="outcome-narrative-", dir=local) as temporary:
        root = Path(temporary)
        report_input = EvolutionOutcomeReportInput(
            run_id="generic-run",
            package_id="generic-package",
            outcome=EffectOutcome.NO_STRICT_IMPROVEMENT,
            search_complete=True,
            reference_summary={"fresh": True},
            candidates=(
                _candidate("candidate-generic").model_copy(
                    update={
                        "gate_status": "rejected",
                        "rejection_reasons": ("protected_objective_regression",),
                    }
                ),
            ),
            deployable_frontier=(),
            merge_outcome=_merge(),
            gate_funnel={"proposed": 1, "deployable": 0},
            rejected_memory_refs=(),
            runtime={"usage": {"agent_calls": 0}},
            policy_evaluation={
                "policy_id": "relative_efficiency_v2",
                "max_relative_cost_ratio": 2.0,
            },
            provenance={"fixture": True},
        )
        input_path = root / "effect-outcome-report-input.json"
        input_path.write_text(report_input.model_dump_json(indent=2), encoding="utf-8")
        config = EvolutionOutcomeReportConfig(
            report_id="generic-narrative",
            title_zh="通用中文叙事报告",
            package_id="generic-package",
            outcome_input_ref=input_path.relative_to(ROOT).as_posix(),
            presentation_mode="narrative_v1",
        )
        output = root / "report"
        builder = EvolutionOutcomeReportBuilder(ROOT, config)
        builder.build(output)
        verified = builder.verify(output)
        html_text = (output / "index.html").read_text(encoding="utf-8")

        assert verified["valid"], verified
        assert verified["presentation_mode"] == "narrative_v1"
        assert all(
            f'id="{section}"' in html_text
            for section in ("overview", "process", "scores", "tasks", "package", "evidence")
        )
        assert "证据与复现" in html_text
        assert "<details><summary>Policy" in html_text
        assert "http://" not in html_text and "https://" not in html_text


def test_narrative_production_template_has_no_run_specific_identifiers() -> None:
    sources = "".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "src/gepase/reporting/outcome.py",
            ROOT / "src/gepase/reporting/canary_html.py",
        )
    )
    assert "slack-gif-creator" not in sources
    assert "functional-validation-" not in sources
    assert "functional-train-" not in sources
