"""S1-B paired calibration over E1 proxy and pre-registered E3 evidence."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from gepase.evals.evidence import EvaluationRecord
from gepase.evals.schema import EvidenceTier
from gepase.store.artifacts import atomic_write

E1_RELIABILITY_CAP = 0.85


def _records(run_dir: Path) -> list[EvaluationRecord]:
    return [
        EvaluationRecord.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted((run_dir / "records").glob("*.json"))
    ]


def _pair_scores(
    records: list[EvaluationRecord], tier: EvidenceTier
) -> dict[tuple[str, str], dict[str, float]]:
    values: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for record in records:
        if record.evidence_tier is tier and record.score is not None:
            values[(record.skill_id, record.task_id)][record.variant] = record.score
    return values


def _work_items(run_dir: Path) -> dict[str, dict[str, Any]]:
    return {
        payload["work_id"]: payload
        for path in sorted((run_dir / "work-items").glob("*.json"))
        if isinstance(payload := json.loads(path.read_text(encoding="utf-8")), dict)
    }


def deterministic_plan_quality(
    record: EvaluationRecord, *, requested_output: dict[str, Any], fixture_ref: str
) -> dict[str, Any]:
    execution_steps = [
        step for step in record.planned_trace if step.action not in {"select_node", "risk"}
    ]
    text = " ".join(
        " ".join(value for value in (step.action, step.target, step.tool) if value)
        for step in record.planned_trace
    ).lower()
    requested = [str(value).lower() for value in requested_output.values()]
    validation = any(
        token in text for token in ("validate", "verify", "check", "test", "hash", "assert")
    )
    risk = any(step.action == "risk" for step in record.planned_trace)
    dimensions = {
        "fixture_grounding": "fixture" in text or fixture_ref.lower() in text,
        "output_grounding": any(value in text for value in requested)
        or any(token in text for token in ("write", "render", "build", "produce")),
        "ordered_steps": len(execution_steps) >= 4,
        "tool_specificity": len({step.tool for step in execution_steps if step.tool}) >= 2,
        "validation_step": validation,
        "risk_identification": risk,
        "risk_mitigation": risk and validation,
        "resource_grounding": any(step.action == "select_node" for step in record.planned_trace)
        or sum(bool(step.target) for step in execution_steps) >= 2,
    }
    structural_score = sum(dimensions.values()) / len(dimensions)
    # Planned-only evidence has an epistemic cap: it cannot establish that execution succeeded.
    # The cap is tier policy, not a fitted transform of the E3 labels.
    return {
        "score": structural_score * E1_RELIABILITY_CAP,
        "structural_score": structural_score,
        "dimensions": dimensions,
        "reliability_cap": E1_RELIABILITY_CAP,
        "method": "deterministic-plan-quality-v1",
    }


def _plan_quality(record: EvaluationRecord, item: dict[str, Any]) -> dict[str, Any]:
    return deterministic_plan_quality(
        record,
        requested_output=item["requested_output"],
        fixture_ref=item["fixture_ref"],
    )


def _plan_pair_scores(
    records: list[EvaluationRecord], items: dict[str, dict[str, Any]]
) -> tuple[dict[tuple[str, str], dict[str, float]], dict[str, dict[str, Any]]]:
    pairs: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    details: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.evidence_tier is not EvidenceTier.E1_SIMULATED:
            continue
        detail = _plan_quality(record, items[record.work_id])
        pairs[(record.skill_id, record.task_id)][record.variant] = detail["score"]
        details[record.record_id] = detail
    return pairs, details


def paired_calibration(
    e1_run: Path,
    high_fidelity_run: Path,
    decision_ledger: Path,
) -> dict[str, Any]:
    e1_records = _records(e1_run)
    high_records = _records(high_fidelity_run)
    self_scores = _pair_scores(e1_records, EvidenceTier.E1_SIMULATED)
    e1, plan_score_details = _plan_pair_scores(e1_records, _work_items(e1_run))
    e3 = _pair_scores(high_records, EvidenceTier.E3_EXECUTABLE)
    complete_e1 = {
        key: value for key, value in e1.items() if {"no-skill", "original"}.issubset(value)
    }
    complete_e3 = {
        key: value for key, value in e3.items() if {"no-skill", "original"}.issubset(value)
    }
    by_skill: dict[str, list[dict[str, float]]] = defaultdict(list)
    decisions: list[dict[str, Any]] = []
    proxy_real_direction_disagreements = 0
    for (skill_id, task_id), scores in sorted(complete_e1.items()):
        no_skill = scores["no-skill"]
        original = scores["original"]
        proxy_delta = original - no_skill
        by_skill[skill_id].append(scores)
        high = complete_e3.get((skill_id, task_id))
        self_pair = self_scores.get((skill_id, task_id), {})
        self_delta = (
            self_pair["original"] - self_pair["no-skill"]
            if {"no-skill", "original"}.issubset(self_pair)
            else None
        )
        high_delta = None if high is None else high["original"] - high["no-skill"]
        direction_disagreement = bool(
            high_delta is not None
            and proxy_delta != 0
            and high_delta != 0
            and (proxy_delta > 0) != (high_delta > 0)
        )
        proxy_real_direction_disagreements += int(direction_disagreement)
        flags: list[str] = []
        if abs(proxy_delta) < 0.05:
            flags.append("non_discriminative_proxy")
        if original < 0.1:
            flags.append("floor")
        if original > 0.9:
            flags.append("ceiling")
        if direction_disagreement:
            flags.append("proxy_real_direction_disagreement")
        self_direction_disagreement = bool(
            high_delta is not None
            and self_delta is not None
            and self_delta != 0
            and high_delta != 0
            and (self_delta > 0) != (high_delta > 0)
        )
        if self_direction_disagreement:
            flags.append("raw_self_score_real_direction_disagreement")
        decisions.append(
            {
                "schema_version": "1.0.0",
                "skill_id": skill_id,
                "task_id": task_id,
                "e1_no_skill": no_skill,
                "e1_original": original,
                "e1_delta": proxy_delta,
                "e1_score_method": "deterministic-plan-quality-v1",
                "e1_reliability_cap": E1_RELIABILITY_CAP,
                "raw_self_score_no_skill": self_pair.get("no-skill"),
                "raw_self_score_original": self_pair.get("original"),
                "raw_self_score_delta": self_delta,
                "e3_no_skill": None if high is None else high["no-skill"],
                "e3_original": None if high is None else high["original"],
                "e3_delta": high_delta,
                "flags": flags,
                "decision": "review" if flags else "retain",
            }
        )
    decision_text = "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in decisions
    )
    atomic_write(decision_ledger, decision_text.encode())
    skill_metrics = {
        skill_id: {
            "e1_pairs": len(rows),
            "no_skill_mean": mean(row["no-skill"] for row in rows),
            "original_mean": mean(row["original"] for row in rows),
            "mean_delta": mean(row["original"] - row["no-skill"] for row in rows),
            "in_calibrated_band": 0.1 <= mean(row["original"] for row in rows) <= 0.9,
            "e3_pairs": sum(key[0] == skill_id for key in complete_e3),
        }
        for skill_id, rows in sorted(by_skill.items())
    }
    invalid_e1_records = sum(
        record.evidence_tier is EvidenceTier.E1_SIMULATED and record.score is None
        for record in e1_records
    )
    provenance = {
        (record.provenance.host, record.provenance.model)
        for record in e1_records
        if record.evidence_tier is EvidenceTier.E1_SIMULATED
    }
    raw_self_metrics = {
        skill_id: {
            "no_skill_mean": mean(
                self_scores[key]["no-skill"]
                for key in self_scores
                if key[0] == skill_id and {"no-skill", "original"}.issubset(self_scores[key])
            ),
            "original_mean": mean(
                self_scores[key]["original"]
                for key in self_scores
                if key[0] == skill_id and {"no-skill", "original"}.issubset(self_scores[key])
            ),
        }
        for skill_id in sorted(by_skill)
    }
    criteria = {
        "all_150_e1_pairs": len(complete_e1) == 150,
        "three_skills_50_pairs_each": len(skill_metrics) == 3
        and all(value["e1_pairs"] == 50 for value in skill_metrics.values()),
        "original_metric_calibrated": all(
            value["in_calibrated_band"] for value in skill_metrics.values()
        ),
        "pre_registered_e3_pairs": len(complete_e3) >= 6
        and all(value["e3_pairs"] >= 2 for value in skill_metrics.values()),
        "no_invalid_proxy_records": invalid_e1_records == 0,
        "model_provenance_complete": len(provenance) >= 1
        and all(host and model for host, model in provenance),
    }
    return {
        "valid": all(criteria.values()),
        "criteria": criteria,
        "e1_records": sum(
            record.evidence_tier is EvidenceTier.E1_SIMULATED for record in e1_records
        ),
        "e1_pairs": len(complete_e1),
        "e3_pairs": len(complete_e3),
        "invalid_e1_records": invalid_e1_records,
        "proxy_real_direction_disagreements": proxy_real_direction_disagreements,
        "raw_self_score_real_direction_disagreements": sum(
            "raw_self_score_real_direction_disagreement" in item["flags"] for item in decisions
        ),
        "raw_self_score_ceiling_skills": sum(
            value["original_mean"] > 0.9 for value in raw_self_metrics.values()
        ),
        "non_discriminative_proxy_cases": sum(
            "non_discriminative_proxy" in item["flags"] for item in decisions
        ),
        "decision_ledger": decision_ledger.as_posix(),
        "skill_metrics": skill_metrics,
        "raw_self_score_metrics": raw_self_metrics,
        "score_method": {
            "id": "deterministic-plan-quality-v1",
            "dimensions": 8,
            "e1_reliability_cap": E1_RELIABILITY_CAP,
            "raw_llm_self_score_used_as_main_metric": False,
            "scored_records": len(plan_score_details),
        },
        "model_provenance": [{"host": host, "model": model} for host, model in sorted(provenance)],
    }
