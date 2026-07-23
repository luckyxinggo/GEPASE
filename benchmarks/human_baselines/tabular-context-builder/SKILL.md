---
name: tabular-context-builder-human-v1
description: Turn tabular fixtures into deterministic, evidence-grounded context artifacts.
---

# Tabular context builder

Inspect the full fixture schema, preserve column names and row values, and produce exactly the requested context file. Separate source facts from derived summaries.

## Procedure

1. Read the fixture and record its ordered columns, row count, identifiers, nulls, and numeric types.
2. Serialize a stable schema section and concise row-grounded context; do not invent missing values.
3. Preserve exact values that appear in the task prompt.
4. Validate the output path, non-empty size, required headings, row count, and every requested literal.
5. Report which checks were observed. If execution is unavailable, label the steps as a plan.
