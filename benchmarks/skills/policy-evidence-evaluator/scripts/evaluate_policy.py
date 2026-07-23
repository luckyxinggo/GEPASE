"""Evaluate an explicit threshold policy over structured records."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("policy_id"), str):
        raise ValueError("policy_id is required")
    if data.get("direction") not in {"gte", "lte"}:
        raise ValueError("direction must be gte or lte")
    threshold = data.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ValueError("threshold must be numeric")
    records = data.get("records")
    if not isinstance(records, list) or not records:
        raise ValueError("records must be non-empty")
    identifiers: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            raise ValueError("each record requires string id")
        score = record.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("record score must be numeric")
        if not isinstance(record.get("segment"), str) or not record["segment"]:
            raise ValueError("record segment must be non-empty")
        if record["id"] in identifiers:
            raise ValueError("record ids must be unique")
        identifiers.add(record["id"])
    return data


def evaluate(data: dict[str, Any]) -> dict[str, Any]:
    threshold = float(data["threshold"])
    direction = data["direction"]
    decisions: list[dict[str, Any]] = []
    segments: dict[str, list[bool]] = defaultdict(list)
    for record in data["records"]:
        accepted = (
            record["score"] >= threshold
            if direction == "gte"
            else record["score"] <= threshold
        )
        decisions.append(
            {
                "id": record["id"],
                "score": record["score"],
                "segment": record["segment"],
                "accepted": accepted,
            }
        )
        segments[record["segment"]].append(accepted)
    accepted_count = sum(item["accepted"] for item in decisions)
    segment_metrics = {
        segment: {
            "total": len(values),
            "accepted": sum(values),
            "acceptance_rate": round(sum(values) / len(values), 6),
        }
        for segment, values in sorted(segments.items())
    }
    return {
        "schema_version": "1.0.0",
        "policy_id": data["policy_id"],
        "rule": {"direction": direction, "threshold": data["threshold"]},
        "summary": {
            "total": len(decisions),
            "accepted": accepted_count,
            "rejected": len(decisions) - accepted_count,
            "acceptance_rate": round(accepted_count / len(decisions), 6),
        },
        "segments": segment_metrics,
        "decisions": decisions,
    }


def report(analysis: dict[str, Any]) -> str:
    summary = analysis["summary"]
    lines = [
        f"# Policy {analysis['policy_id']}",
        "",
        f"Rule: `{analysis['rule']['direction']}` `{analysis['rule']['threshold']}`.",
        "",
        f"Accepted {summary['accepted']} of {summary['total']} records "
        f"(rate {summary['acceptance_rate']:.6f}).",
        "",
        "## Segments",
        "",
        "| Segment | Total | Accepted | Rate |",
        "| --- | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| {segment} | {metrics['total']} | {metrics['accepted']} | "
        f"{metrics['acceptance_rate']:.6f} |"
        for segment, metrics in analysis["segments"].items()
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    data = validate(json.loads(args.input.read_text(encoding="utf-8")))
    analysis = evaluate(data)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "analysis.json").write_text(
        json.dumps(analysis, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output_dir / "report.md").write_text(report(analysis), encoding="utf-8")
    print(json.dumps({"status": "ok", **analysis["summary"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
