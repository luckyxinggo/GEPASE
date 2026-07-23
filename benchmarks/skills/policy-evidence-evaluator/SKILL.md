---
name: policy-evidence-evaluator
description: Evaluate threshold-based decision policies over structured records and produce reproducible aggregate evidence. Use when an agent must apply an explicit greater-than or less-than threshold, preserve record-level provenance, summarize segment outcomes, and generate deterministic JSON and Markdown artifacts.
---

# Policy Evidence Evaluator

Use the bundled evaluator for all counts and rates. Do not calculate policy metrics manually or
turn an interpretation into a source fact.

## Workflow

1. Validate the fixture with `references/policy-contract.md`.
2. Run:

   ```bash
   python scripts/evaluate_policy.py --input <fixture.json> --output-dir <result-dir>
   ```

3. Check `analysis.json` for the resolved rule, totals, per-segment metrics, and record decisions.
4. Check `report.md` for a human-readable view and cite `analysis.json` as the numeric source.

Stop on missing IDs, non-numeric scores, duplicate records, or an unsupported direction. Keep
generated artifacts outside the Skill package.

## Evidence rules

- `gte` means score is greater than or equal to the threshold; `lte` means less than or equal.
- Preserve every input record ID and segment in record-level decisions.
- Compute rates as accepted / total and round only the displayed rate to six decimals.
- Read `references/policy-contract.md` when checking fields or interpreting an input rejection.
