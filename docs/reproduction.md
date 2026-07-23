# Reproduction and release evidence

This guide separates offline engineering checks, sealed-result reproduction, and a new
Agent-native evolution run. Only the third path dispatches Agent roles.

## 1. Install from a checkout

GEPASE requires Python 3.11 or newer and uses the committed `uv.lock`.

```bash
uv sync --all-extras --frozen
uv run gepase --version
uv run gepase doctor --format json
```

The deterministic smoke path does not call an Agent or external API:

```bash
uv run gepase mock run \
  --config configs/examples/mock.yaml \
  --output artifacts/local/mock-run \
  --format json
uv run gepase artifact verify artifacts/local/mock-run --format json
```

## 2. Reproduce the sealed canary result without rerunning Agents

The committed R2–R5 evidence is content indexed. These commands verify the original EvalPlan,
paired run, evolution run, and report, then independently recompute the six R5 release Gates:

```bash
uv run gepase artifact verify artifacts/runs/r2-slack-gif-creator-evalplan --format json
uv run gepase artifact verify artifacts/runs/r3-slack-gif-creator-paired --format json
uv run gepase artifact verify artifacts/runs/r4-slack-gif-creator-evolution --format json
uv run gepase report verify \
  --config configs/canaries/slack-gif-creator-r5.json \
  --report-dir artifacts/runs/r5-slack-gif-creator-report \
  --format json
uv run python scripts/run_r5_gates.py \
  --report-dir artifacts/runs/r5-slack-gif-creator-report
```

Rebuild the report into a new ignored directory; the sealed report is intentionally immutable:

```bash
uv run gepase report build \
  --config configs/canaries/slack-gif-creator-r5.json \
  --output artifacts/local/r5-rebuilt \
  --format json
```

Verify and materialize the deployable Package into a new directory:

```bash
uv run gepase report deploy \
  --config configs/canaries/slack-gif-creator-r5.json \
  --report-dir artifacts/runs/r5-slack-gif-creator-report \
  --output artifacts/local/deployed-slack-gif-creator \
  --format json
```

None of the commands in this section runs R3/R4 again, searches for a candidate, or calls a
Headless provider.

## 3. Start a new Agent-native run

The generic flow is resumable rather than a single opaque command:

1. `gepase eval onboarding-start` parses and pins a Package, exports an isolated Eval Designer
   work item, and records a checkpoint.
2. A host Agent follows `.agents/skills/gepase-orchestrator/`, returns the typed Designer
   submission, and Core renders `review.html`.
3. `gepase eval import-review` freezes explicit review decisions; `gepase eval resume` continues
   the same run.
4. Executor, Independent Grader, Comparator, and Analyzer submissions are dispatched in separate
   contexts and ingested through the `gepase eval` commands.
5. `gepase optimizer r4-*` operates the single candidate/GEPA/Patch/Gate state machine and can
   resume from its checkpoint.

Inspect the exact options in the installed version:

```bash
uv run gepase eval --help
uv run gepase optimizer --help
uv run gepase mutation --help
uv run gepase report --help
```

See [evaluation.md](evaluation.md) for the complete command protocol and
[orchestrator.md](orchestrator.md) for role-isolation rules.

## 4. Optional role-scoped Headless configuration

`configs/examples/headless-roles.yaml` demonstrates the public routing contract. It stores only an
environment-variable name, never a credential value:

```bash
uv run gepase config validate configs/examples/headless-roles.yaml --format json
```

In v0.1 this is a validated provider-neutral interface, not a built-in API runtime. A host adapter
may consume the configuration and dispatch roles through an OpenAI-compatible provider, but it
must still preserve isolated contexts and the same typed WorkItem/submission boundaries. The
default path remains Agent-native and requires no additional API key.

## 5. Evidence interpretation

- Code existence is shown by import, schema and CLI checks.
- Engineering behavior is shown by unit/integration/fault tests and content-addressed artifacts.
- Algorithm effect is supported only for the sealed `slack-gif-creator` run: held-out mean delta
  `+0.12427`, three wins in three validation cases, one deployable candidate.
- The accepted edit changed one bounded `SKILL.md` instruction node. The run does not establish
  cross-file superiority, graph-method superiority, or generalization across Skills/models/seeds.
