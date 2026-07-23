# B2 human-improved baseline log

- Operator: root-agent acting as the documented human editor for the S4 engineering reference.
- Visible data: seed `SKILL.md`, train TaskCase prompts, capability manifests, and S1 train calibration summaries only.
- Forbidden data: validation outputs used for editing and every test TaskCase/output. Test access count is zero.
- Work accounting: 54 minutes of review/editing across three packages; this cost is not represented as zero or compared as a fully automatic method.
- Editing rule: preserve the public package truth, add explicit task-to-procedure mappings and validation checklists, and avoid references to files that do not exist.

| Skill | Minutes | Main train-derived change | Snapshot |
|---|---:|---|---|
| structured-report-builder | 18 | exact-value preservation and standalone HTML checks | `structured-report-builder/SKILL.md` |
| tabular-context-builder | 17 | deterministic column/context validation | `tabular-context-builder/SKILL.md` |
| policy-evidence-evaluator | 19 | evidence-tier separation and auditable decision output | `policy-evidence-evaluator/SKILL.md` |

These snapshots are engineering references. They are not automatically generated candidates and are not selected using held-out results.
