---
name: tabular-context-builder
description: Convert structured multi-table JSON fixtures into bounded, traceable context packs for LLM and Agent reading. Use when complete CSV evidence, compact Markdown previews, a navigation index, and a manifest with row and column provenance are required.
---

# Tabular Context Builder

Build the context pack with the bundled script so complete rows remain in CSV while model-facing
Markdown stays bounded.

## Workflow

1. Inspect the fixture against `references/context-pack-contract.md`.
2. Run:

   ```bash
   python scripts/build_context_pack.py --input <fixture.json> --output-dir <pack-dir>
   ```

3. Read `navigation.md`, then `manifest.json`. Use `tables/*.md` only for previews and use the
   referenced CSV whenever an answer depends on a value or a row not shown in the preview.
4. Confirm every table has a manifest entry, CSV, preview, exact row count, and ordered columns.

Never truncate the CSV source, silently coerce values, or write output inside the Skill package.

## Evidence rules

- Preserve input table and row order.
- Quote CSV values through the standard CSV writer.
- Make missing values explicit as empty cells.
- Read `references/context-pack-contract.md` when checking schema and provenance fields.
