---
name: gepase-orchestrator
description: Orchestrate isolated GEPASE EvalPlan design, paired Skill execution, blind grading/comparison/analysis, and bounded package-edit work inside an Agent host. Use when Codex, Claude Code, or another subagent-capable host must execute EvalDesigner, Executor, IndependentGrader, Comparator, Analyzer, Reflection, or PackagePatch work items, preserve typed provenance, and submit results through the GEPASE CLI into Core-owned state.
---

# GEPASE Orchestrator

Use the host Agent only as an execution and delegation surface. Keep all evaluation state in the
GEPASE Core ledger and artifacts.

## EvalPlan onboarding workflow

1. Let Core parse the pinned public Package and export `designer-work-item.json`:

   ```bash
   uv run gepase eval onboarding-start \
     --run-dir <run-dir> --package <package-dir> \
     --provenance <source-provenance.json> --design-brief <design-brief.json>
   ```

2. Launch exactly one isolated Eval Designer. Give it only the work item, its referenced complete
   Package, design brief, graph/diagnostics, provenance, fixtures, and submission schema. Do not give
   it search feedback, candidate identity, expected winner, sibling output, or later review state.
3. Require the Designer to read every path in `required_package_reads`, record those reads in
   `package_access`, propose separate Trigger and Functional cases, and return one typed
   `EvalDesignerSubmission`. It must not ingest, approve, freeze, or execute its own cases.
4. Ingest with `gepase eval submit-design`. Core runs deterministic design checks and, on success,
   enters `awaiting_review` and renders the offline Chinese `review.html`.
5. A human, maintainer, or explicitly identified agent-assisted reviewer makes an exact decision for
   every case. Import `review.json` with `gepase eval import-review`; Core rechecks the edited plan and
   freezes its immutable hash. Run `gepase eval resume` to enter `execution_ready` in the same run.

Do not bypass `awaiting_review`, invent review decisions inside the Designer context, or call an
Executor before Core reports `execution_ready`.

## Evaluation workflow

1. Check the run without changing it:

   ```bash
   uv run gepase eval status --run-dir <run-dir> --format json
   ```

2. For a frozen Functional EvalPlan, let Core create the same-round pair with `eval plan-frozen`.
   Give each worker only its individual `executor-work-items/<work-id>.json`, never the internal
   ledger work item or a batch containing siblings.
3. For each pair, launch the `no-skill` and `original` workers in the same round with isolated
   contexts. Give each worker only its own work item, prompt, fixture, requested output, and—only
   for `original`—`skill_ref`.
4. Never give workers assertions, expected values, sibling output, candidate lineage, optimization
   diagnostics, or which variant is expected to win.
5. R3 Functional work uses E2, not E1. Require the task-native output, `transcript.md`, ordered
   `observed-trace.json`, usage/timing, and `package-access.json`. A with-skill worker must read
   `SKILL.md`, execute Package code, use exact graph node IDs, and report loaded bytes/tokens. A
   no-skill worker must submit an empty Package-access list.
6. Have the host call `gepase eval submit-work`; this hashes artifacts and creates the canonical
   ExecutionBundle. Then ingest with `gepase eval ingest`, which derives E3 from the real output.
7. After every pair completes, use the role pipeline in order:
   `prepare-grading` → isolated `submit-grade` workers → `prepare-comparison` → isolated AB/BA
   `submit-comparison` workers → `prepare-analysis` → isolated `submit-analysis` workers →
   `finalize-functional`. Never reuse a context across these roles or work items.
8. Check `status`, `aggregate`, and `replay`. On interruption, run `gepase eval resume`, then
   re-export pending work; completed work must not be rerun.

## Structured PackagePatch workflow

1. Inspect `gepase mutation status`, then export exactly one item with `gepase mutation next-work`.
2. Give one isolated worker only that PackagePatchProposalWorkItem and
   `prompts/patch_proposer.md`. The Core has already selected the bounded target nodes.
3. The worker must return JSON containing only `summary` and typed `operations`. It may use only
   listed node IDs, paths, precondition hashes, evidence refs, operation types, and the edit budget.
   It must not edit the repository, choose new files, return shell/write actions, or manufacture
   candidate/patch identities.
4. Create the immutable submission with `gepase mutation submit-proposal`, then ingest it with
   `gepase mutation ingest`. Core validation assigns `patch_id`; the host never does.
5. If schema validation fails, a bounded repair is a new isolated work attempt and counts against
   the declared repair budget. If no valid edit exists, submit a typed failure.
6. Applying a submitted patch, reparsing the package, computing graph diff/impact, gating, candidate
   construction, lineage, rejection memory, and rollback are Core responsibilities.

The Core chooses component IDs and owns candidate construction, official GEPA selection, lineage,
frontier, budget, checkpoint, and replay state. The integrated evolution controller is intentionally
not exposed by this thin adapter until R4 reconnects evaluation, Patch, Gate, and merge through one
Core-owned state machine.

Read `references/work-submission.md` for exact Eval Designer, ExecutionBundle, Independent Grader,
Comparator, and Analyzer submission protocols, workspace conventions, failure handling, and
cross-host notes.

## Boundaries

- Do not edit benchmark Skill packages, fixtures, assertions, or test splits during evaluation.
- Do not write candidate pools, Pareto frontiers, reflection memory, lineage, budgets, or gate
  decisions into this Skill directory; all optimizer state belongs to the Core run store.
- Do not select unlisted components, alter file paths, or mutate source Skill packages during
  reflection or patch proposal. Do not add graph-guided selection behavior in the host layer.
- Do not simulate E2 with prose. If execution is unavailable, submit the typed environment or
  unsupported failure and preserve the sample.
- Do not call an external LLM API unless the run explicitly selects a Headless Provider.
- Keep credentials, private absolute paths, raw private traces, and hidden labels out of all work
  bundles and submissions.
