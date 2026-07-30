# Agent-native orchestration

The repo-scoped `.agents/skills/gepase-orchestrator/` Skill is a thin adapter. It explains how a
host Agent should dispatch an isolated Eval Designer, blind `no-skill` and `original` Executors,
and bounded Patch workers through the Core CLI. It does not contain EvalPlan state, candidate
pools, Pareto state, GEPA reflection memory, benchmark labels, review decisions, or acceptance
decisions.

For onboarding, Core exports `designer-work-item.json`; the host gives one isolated Designer only
the referenced complete Package, design brief, graph/diagnostics, provenance, fixtures, and output
schema. The Designer records all Package reads and returns a typed submission, but cannot ingest,
approve, freeze, or execute cases. Core owns deterministic checks and the `awaiting_review →
eval_plan_frozen → execution_ready` transition.

Agent-native mode reuses the host's existing subagents, tools, and authenticated integrations; it
does not require a second API key. A future role-scoped Headless Provider must implement the same
EvalWorkItem and ExecutionBundle contracts, remain optional, and cannot silently upgrade simulated
E1 evidence to E2. The old all-case E1 calibration backend was removed in R1 because it truncated
Package context and was not the role-isolated backend required by the corrected design.

For R3, Core owns the role sequence and typed boundaries:

1. sixteen fresh Executors receive oracle-free work items for eight no-skill/original pairs;
2. sixteen fresh Independent Graders each inspect one anonymous GIF;
3. six fresh Comparators perform AB/BA review for three validation cases;
4. eight fresh Analyzers consume only post-evaluation typed evidence and Package Graph hints.

The host never forwards role conversations. A failed schema, evidence-boundary, context-isolation,
artifact-hash, or graph-node check is returned to the same role context for correction; Core alone
ingests the corrected submission. The adapter cannot compute TaskScoreVectors, reconcile AB/BA,
change the frozen plan, or declare a candidate accepted.

正式调用顺序中，耗尽的角色 work 先以 `gepase eval terminalize-role-attempts` 交给现有 Functional Core 收口，随后才准备其余 case 的 Comparator/Analyzer；完成 train admission 与 Reflection ingest 后，可用 `gepase optimizer r4-plan-generation2` 调用唯一 Controller 的有界 generation-2 planner。

GH-E1 further distinguishes deterministic evidence packaging/metadata correction from real Agent
re-execution. Deterministic correction preserves the original workspace bytes and does not consume
an Agent repair; an additional real re-execution requires a hash-bound authorization. Required
evidence containing sensitive data fails closed, while optional diagnostics may be excluded only
with their original hash and reason retained. Final usage is reconciled from ActiveSessionRuntime,
reservation settlements, and HostAttemptAccounting, including contexts that never produced an
accepted submission.

The GH-P1 semantic-hypothesis Analyzer extension is retired from active orchestration. Historical
work/submission models remain readable for sealed-evidence verification, but new Analyzer work
cannot request semantic enrichment and Core no longer creates semantic overlays or lets them affect
selector ranking, failure slices, Patch authorization, Merge, or Gate decisions.

To inspect the adapter:

```bash
python ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py \
  .agents/skills/gepase-orchestrator
```

See [Multi-fidelity evaluation](evaluation.md) for the Core protocol.
