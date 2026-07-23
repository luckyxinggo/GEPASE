# Multi-fidelity evaluation

GEPASE separates cheap coverage evidence from functional acceptance evidence:

| Tier | Meaning | Planned trace | Observed trace | Typical use |
|---|---|---:|---:|---|
| E0 | Static package evidence | optional | forbidden | structure and capability preflight |
| E1 | Optional simulated plan | required | forbidden | explicit diagnostics only; default off |
| E2 | Delegated Agent execution | optional | required | actual task behavior and artifacts |
| E3 | Executable assertions/replay | inherited | inherited | content-level deterministic evidence |

E1 cannot prove that a tool or task succeeded and cannot enter acceptance. E2/E3 records require
host, model, host task, submission, usage, artifact hash, and trace provenance. An E3 assertion
pass rate remains one evidence channel; it is not the six-dimensional TaskScoreVector. Assertions,
rubrics, expected answers, sibling output, and candidate identity are excluded from Executor work.

## EvalPlan onboarding

Before paired execution, a pinned Package receives one reviewed and immutable EvalPlan revision.
R2 implements this flow with the public `slack-gif-creator` canary while keeping the contracts
domain-neutral:

```bash
uv run gepase eval onboarding-start \
  --run-dir artifacts/runs/r2-slack-gif-creator-evalplan \
  --package benchmarks/canaries/slack-gif-creator/package \
  --provenance benchmarks/canaries/slack-gif-creator/source-provenance.json \
  --design-brief benchmarks/canaries/slack-gif-creator/design-brief.json

# An isolated Eval Designer writes one typed submission after reading the complete Package.
uv run gepase eval submit-design \
  --run-dir artifacts/runs/r2-slack-gif-creator-evalplan \
  --submission artifacts/runs/r2-slack-gif-creator-evalplan/agent/designer-submission.json

uv run gepase eval review \
  --run-dir artifacts/runs/r2-slack-gif-creator-evalplan
```

Core deterministically checks schema, source/license binding, fixture hashes, train/validation
leakage, weak assertions, evidence policy, and Executor-view isolation. A valid draft then stops at
`awaiting_review`. The self-contained Chinese `review.html` supports search, filtering, full-case
JSON edits, low-risk train batching, approve/reject/regenerate decisions, Package Graph inspection,
and local `review.json` export. It does not upload data or require a web server.

Every case needs one explicit decision. Core records the reviewer kind (`human`, `maintainer`, or
truthfully labelled `agent-assisted`), rechecks edited cases, freezes the plan hash, and resumes the
same run only after successful import:

```bash
uv run gepase eval import-review \
  --run-dir artifacts/runs/r2-slack-gif-creator-evalplan \
  --review review.json
uv run gepase eval resume --run-dir artifacts/runs/r2-slack-gif-creator-evalplan
```

An Eval Designer cannot review, freeze, or execute its own cases. GIF prompts, fixtures, rubrics,
and oracles live in this canary's frozen EvalPlan; they are not hard-coded in the generic engine.
The same frozen revision must be reused for original, no-skill, candidate, and merge-child runs.

## Execution CLI flow

The frozen public canary uses the stricter R3 path:

```bash
uv run gepase eval plan-frozen \
  --run-dir artifacts/runs/r3-slack-gif-creator-paired \
  --frozen-plan artifacts/runs/r2-slack-gif-creator-evalplan/frozen-eval-plan.json \
  --scoring-policy configs/canaries/slack-gif-creator-r3-scoring.json \
  --skill-ref benchmarks/canaries/slack-gif-creator/package \
  --package-graph-ref artifacts/runs/r2-slack-gif-creator-evalplan/package/graph.json

# Executors receive separate oracle-free work items. Core then ingests their typed submissions.
uv run gepase eval prepare-grading --run-dir artifacts/runs/r3-slack-gif-creator-paired
uv run gepase eval prepare-comparison --run-dir artifacts/runs/r3-slack-gif-creator-paired
uv run gepase eval prepare-analysis --run-dir artifacts/runs/r3-slack-gif-creator-paired
uv run gepase eval finalize-functional --run-dir artifacts/runs/r3-slack-gif-creator-paired
uv run gepase eval verify-functional --run-dir artifacts/runs/r3-slack-gif-creator-paired
```

Every successful Executor submission contains a real task-native GIF, transcript, observed trace,
usage/timing, artifact hash, and context identity. Original-Skill executions additionally record
ordered Package read/execute events mapped to the frozen Package Graph; no-skill access must be
empty. Core derives E3 measurements from the GIF itself and generates contact sheets for blind
quality review.

Independent Graders see only one anonymous artifact. Comparators see two anonymous artifacts for
the three validation cases, with AB and BA order reversal. Analyzer/ASI runs only after comparison
reconciliation and may cite only its typed E2/E3/grade/comparator/access evidence and valid graph
nodes. Executor, Grader, Comparator, and Analyzer contexts must all be unique.

The six functional dimensions are `task_correctness`, `output_quality`, `skill_gain`,
`reliability`, `efficiency`, and `package_quality`. `skill_gain` is a paired delta, Trigger Eval is
stored separately, and `verify-functional` reconstructs every vector from raw evidence. The first
R3 run measured original mean skill gain `-0.0455`; this is an evaluation/headroom result, not an
optimization-effect claim. R4 then reused the fully hashed original anchor and freshly evaluated
candidate outputs: one candidate passed frozen validation at mean delta `+0.12427`, while a timeout
branch and a merge child with a protected category regression were rejected. That is a
single-canary held-out result; it must not be generalized beyond the frozen run.

## R5 evidence presentation

R5 does not introduce another evaluation tier and does not execute a role. It is a sealed,
read-only projection of R2–R4 evidence into a Chinese interactive HTML report. The report preserves
the distinction between E2 task-native outputs, E3 deterministic assertions, independent quality
grades, anonymous comparison, TaskScoreVector, and strict Gate decisions; assertion pass rate is
never relabelled as overall Skill quality.

The R5 machine Gate independently recomputes the deployable candidate's held-out mean delta,
3/3 win record, category floors, merge rejection, copied GIF hashes, deployable ZIP contents,
upstream seals, and zero Agent/API calls. Reporting cannot repair or reinterpret a failed upstream
Gate.

```bash
uv run gepase eval plan \
  --run-dir artifacts/local/eval-demo \
  --manifest benchmarks/manifest-draft.json \
  --splits validation --tiers E2 --variants no-skill,original

uv run gepase eval export-work \
  --run-dir artifacts/local/eval-demo \
  --output artifacts/local/eval-demo/exports/work.json

uv run gepase eval status --run-dir artifacts/local/eval-demo
```

An Agent worker creates a submission with `eval submit-work`; the orchestrator ingests it with
`eval ingest`. `status`, `resume`, `aggregate`, and `replay` read the durable ledger. The Python
Core does not implement an Agent planner, tool scheduler, login session, or general sandbox.

## Pairing and cache identity

A pair is comparable only when task, tier, prompt hash, fixture hash, policy hash, provider
snapshot, host/model snapshot, and seed match. Variant and candidate snapshot are the only allowed
differences. Cache identity includes all of those fields, so changing the host/model invalidates a
cached result.

## Security

Work items and submissions use repository-relative paths. Schema validation rejects user-home
absolute paths and key-like values; ingest verifies every artifact hash and scans UTF-8 artifacts
for the same prohibited patterns. Private raw traces belong only in ignored local storage.
