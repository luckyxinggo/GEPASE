"""Render a validated, self-contained HTML report from JSON."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any


def text(value: Any) -> str:
    return html.escape(str(value), quote=True)


def validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("title"), str) or not data["title"]:
        raise ValueError("title must be a non-empty string")
    if not isinstance(data.get("metrics"), list) or not isinstance(data.get("sections"), list):
        raise ValueError("metrics and sections must be lists")
    for metric in data["metrics"]:
        if not isinstance(metric, dict) or not {"label", "value"} <= metric.keys():
            raise ValueError("each metric requires label and value")
    for section in data["sections"]:
        table = section.get("table") if isinstance(section, dict) else None
        if not isinstance(section, dict) or not isinstance(section.get("heading"), str):
            raise ValueError("each section requires heading")
        if not isinstance(table, dict) or not isinstance(table.get("columns"), list):
            raise ValueError("each section requires table.columns")
        columns = table["columns"]
        if len(columns) != len(set(columns)) or not all(isinstance(item, str) for item in columns):
            raise ValueError("table columns must be unique strings")
        if not isinstance(table.get("rows"), list):
            raise ValueError("table.rows must be a list")
        if any(not isinstance(row, dict) or not set(row) <= set(columns) for row in table["rows"]):
            raise ValueError("table row contains an undeclared column")
    return data


def render(data: dict[str, Any]) -> str:
    metrics = "".join(
        f'<article class="metric"><strong>{text(item["value"])}{text(item.get("unit", ""))}'
        f'</strong><span>{text(item["label"])}</span></article>'
        for item in data["metrics"]
    )
    sections: list[str] = []
    for section in data["sections"]:
        columns = section["table"]["columns"]
        header = "".join(f"<th scope=\"col\">{text(column)}</th>" for column in columns)
        rows = "".join(
            "<tr>"
            + "".join(f"<td>{text(row.get(column, ''))}</td>" for column in columns)
            + "</tr>"
            for row in section["table"]["rows"]
        )
        sections.append(
            f'<section><h2>{text(section["heading"])}</h2>'
            f'<p>{text(section.get("summary", ""))}</p>'
            f'<div class="table-wrap"><table><thead><tr>{header}</tr></thead>'
            f"<tbody>{rows}</tbody></table></div></section>"
        )
    source = text(data.get("provenance", {}).get("source", "unspecified"))
    subtitle = text(data.get("subtitle", ""))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{text(data["title"])}</title><style>
:root{{--ink:#17202a;--muted:#586473;--surface:#f4f7fa;--accent:#155eef}}
*{{box-sizing:border-box}}
body{{margin:0;font:16px/1.5 system-ui;color:var(--ink);background:var(--surface)}}
main{{max-width:1080px;margin:auto;padding:40px 24px}}
header,section{{background:white;padding:24px;margin:0 0 20px;border-radius:12px}}
.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}
.metric{{padding:16px;border:1px solid #d8e0e8}}
.metric strong,.metric span{{display:block}}.metric strong{{font-size:1.5rem;color:var(--accent)}}
.table-wrap{{overflow:auto}}
table{{border-collapse:collapse;width:100%}}
th,td{{padding:10px;border:1px solid #d8e0e8;text-align:left}}
footer{{color:var(--muted)}}
</style></head><body><main><header><h1>{text(data["title"])}</h1><p>{subtitle}</p>
<div class="metrics">{metrics}</div></header>{''.join(sections)}
<footer>Source: {source}</footer></main></body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    data = validate(json.loads(args.input.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(data), encoding="utf-8")
    print(json.dumps({"status": "ok", "output": args.output.name}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
