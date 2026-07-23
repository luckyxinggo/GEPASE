"""Build a bounded, traceable context pack from multi-table JSON."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any


def slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "table"


def validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or not isinstance(data.get("title"), str):
        raise ValueError("title is required")
    tables = data.get("tables")
    if not isinstance(tables, list) or not tables:
        raise ValueError("tables must be a non-empty list")
    names: set[str] = set()
    for table in tables:
        if not isinstance(table, dict) or not isinstance(table.get("name"), str):
            raise ValueError("each table requires a name")
        if table["name"] in names:
            raise ValueError("table names must be unique")
        names.add(table["name"])
        columns = table.get("columns")
        rows = table.get("rows")
        if not isinstance(columns, list) or len(columns) != len(set(columns)):
            raise ValueError("columns must be a unique list")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("rows must be objects")
        if any(not set(row) <= set(columns) for row in rows):
            raise ValueError("row contains an undeclared column")
    return data


def markdown_table(columns: list[str], rows: list[dict[str, Any]]) -> str:
    def cell(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    lines.extend(
        "| " + " | ".join(cell(row.get(column, "")) for column in columns) + " |"
        for row in rows[:10]
    )
    return "\n".join(lines) + "\n"


def build(data: dict[str, Any], output: Path) -> dict[str, Any]:
    table_root = output / "tables"
    table_root.mkdir(parents=True, exist_ok=True)
    manifest_tables: list[dict[str, Any]] = []
    navigation = [
        f"# {data['title']}",
        "",
        "Read CSV for complete evidence; Markdown is a preview.",
        "",
    ]
    for table in data["tables"]:
        safe_name = slug(table["name"])
        csv_path = table_root / f"{safe_name}.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=table["columns"], extrasaction="raise")
            writer.writeheader()
            writer.writerows(table["rows"])
        preview_path = table_root / f"{safe_name}.md"
        preview_path.write_text(markdown_table(table["columns"], table["rows"]), encoding="utf-8")
        csv_hash = hashlib.sha256(csv_path.read_bytes()).hexdigest()
        item = {
            "name": table["name"],
            "columns": table["columns"],
            "row_count": len(table["rows"]),
            "csv": csv_path.relative_to(output).as_posix(),
            "preview": preview_path.relative_to(output).as_posix(),
            "csv_sha256": csv_hash,
        }
        manifest_tables.append(item)
        navigation.append(f"- [{table['name']}]({item['preview']}) — {item['row_count']} rows")
    manifest = {"schema_version": "1.0.0", "title": data["title"], "tables": manifest_tables}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (output / "navigation.md").write_text("\n".join(navigation) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    data = validate(json.loads(args.input.read_text(encoding="utf-8")))
    manifest = build(data, args.output_dir)
    print(json.dumps({"status": "ok", "tables": len(manifest["tables"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
