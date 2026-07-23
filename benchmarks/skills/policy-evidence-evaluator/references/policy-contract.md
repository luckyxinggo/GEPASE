# Policy fixture contract

Required fields:

- `policy_id`: stable string identifier.
- `threshold`: numeric boundary.
- `direction`: `gte` or `lte`.
- `records`: non-empty list of `{id, score, segment}`.

Record IDs must be unique, scores must be numeric but not boolean, and segments must be non-empty
strings. The evaluator writes `analysis.json` and `report.md`. `analysis.json` contains exact totals,
six-decimal rates, a stable segment map, and one decision per input record. It must not make a
recommendation beyond the configured rule.
