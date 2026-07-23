"""GateDecision-derived JSON/HTML audit rendering without verdict re-interpretation."""

from __future__ import annotations

import html
import json

from gepase.optimizer.acceptance.models import GateDecision


def gate_audit_payload(decisions: list[GateDecision]) -> dict[str, object]:
    verdicts: dict[str, int] = {}
    funnel: dict[str, int] = {
        "proposed": len(decisions),
        "gate_0_passed": 0,
        "gate_1_passed": 0,
        "gate_2_passed": 0,
        "gate_3_passed": 0,
        "accepted": 0,
    }
    for decision in decisions:
        verdicts[decision.verdict.value] = verdicts.get(decision.verdict.value, 0) + 1
        outcomes = {item.level.value: item.outcome.value for item in decision.gates}
        for level in range(4):
            if (
                outcomes.get(
                    f"gate_{level}_{['schema', 'static', 'minibatch', 'validation'][level]}"
                )
                == "passed"
            ):
                funnel[f"gate_{level}_passed"] += 1
        funnel["accepted"] += int(decision.frontier_eligible)
    return {
        "schema_version": "1.0.0",
        "decisions": len(decisions),
        "funnel": funnel,
        "verdicts": dict(sorted(verdicts.items())),
        "rejected_reason_counts": _reason_counts(decisions),
        "rows": [item.model_dump(mode="json") for item in decisions],
    }


def _reason_counts(decisions: list[GateDecision]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions:
        if decision.frontier_eligible:
            continue
        for reason in decision.reason_codes:
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def render_gate_report(decisions: list[GateDecision]) -> str:
    payload = gate_audit_payload(decisions)
    cards = "".join(
        f'<div class="card"><strong>{html.escape(key)}</strong><span>{value}</span></div>'
        for key, value in payload["funnel"].items()  # type: ignore[union-attr]
    )
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(item.decision_id)}</td>"
        f"<td>{html.escape(item.patch_id)}</td>"
        f"<td>{html.escape(item.verdict.value)}</td>"
        f"<td>{html.escape(', '.join(item.reason_codes))}</td>"
        f"<td>{item.total_usage.metric_calls}</td>"
        "</tr>"
        for item in decisions
    )
    embedded = html.escape(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>GEPASE Gate Audit</title><style>
body{{font-family:system-ui,sans-serif;margin:2rem;background:#f6f7fb;color:#182033}}
.funnel{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem}}
.card{{background:white;padding:1rem;border-radius:12px;box-shadow:0 2px 10px #0001}}
.card span{{display:block;font-size:2rem;margin-top:.4rem}}
table{{width:100%;border-collapse:collapse;background:white;margin-top:2rem}}
th,td{{padding:.7rem;border-bottom:1px solid #ddd;text-align:left}}code{{word-break:break-all}}
</style></head><body><h1>GEPASE Gate Audit</h1><div class="funnel">{cards}</div>
<table><thead><tr><th>Decision</th><th>Patch</th><th>Verdict</th>
<th>Reasons</th><th>Metric calls</th></tr></thead>
<tbody>{rows}</tbody></table><details><summary>Machine payload</summary>
<code>{embedded}</code></details>
</body></html>"""
