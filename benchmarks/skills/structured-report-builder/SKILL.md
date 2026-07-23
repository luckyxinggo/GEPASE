---
name: structured-report-builder
description: Build deterministic, accessible, self-contained HTML reports from structured JSON metrics and tables. Use when an agent must create an auditable business or analytical report artifact, preserve source values exactly, avoid remote assets, and validate the output contract.
---

# Structured Report Builder

Create reports through the bundled deterministic renderer; do not recalculate, round, or invent
source values in prose.

## Workflow

1. Inspect the input JSON and confirm it follows `references/report-contract.md`.
2. Run:

   ```bash
   python scripts/render_report.py --input <fixture.json> --output <report.html>
   ```

3. Confirm the output exists, has one `<h1>`, includes every section heading and metric label,
   and contains no remote `src` or `href` URLs.
4. Report the output path and any rejected input field. Never claim success without checking the
   generated file.

Keep all generated artifacts outside this Skill package.

## Evidence rules

- Treat the JSON input as the numeric source of truth.
- Escape all source text before inserting it into HTML.
- Preserve table row order and column order.
- Use the embedded stylesheet; do not add network fonts, scripts, trackers, or images.
- Read `references/report-contract.md` only when validating fields or debugging a rejection.
