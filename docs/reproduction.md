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

Verify the published relative-efficiency v2 narrative report without access to private raw Agent
workspaces:

```bash
uv run gepase artifact verify \
  artifacts/runs/f4e-slack-gif-creator-relative-efficiency-v2-report \
  --format json
uv run gepase artifact verify artifacts/stages/POST-F4E-RELEASE --format json
```

The first command checks the 56-file self-contained report seal. The second verifies the curated
release-stage evidence, including the F4e Machine Gate and user visual confirmation. Rebuilding the
report from scratch still requires the unpublished sealed F4b/F4c raw evidence and is intentionally
not promised by a clean public clone.

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

### Verify the GH-E1 graph-hardening result

On the `codex/graph-hardening` branch, a clean clone can verify the published GH-E1 negative-result
report and every public graph-hardening/finalization stage without dispatching an Agent:

```bash
uv run gepase artifact verify \
  artifacts/runs/gh-e1-slack-gif-creator-report --format json
uv run gepase artifact verify \
  artifacts/runs/gh-e1-slack-gif-creator-report/final --format json
uv run gepase artifact verify artifacts/stages/GH-E0.5 --format json
uv run gepase artifact verify artifacts/stages/GH-E1 --format json
uv run gepase artifact verify artifacts/stages/POST-GH-E1-CLEANUP --format json
uv run gepase artifact verify artifacts/stages/POST-GH-E1-FINALIZATION --format json
```

Expected outcome: `no_strict_improvement`, zero deployable candidates, and GHE1-G00–G09 all
passed in the published report/stage evidence. These commands verify content-addressed public
evidence and do not rerun Executor, Grader, Comparator, Analyzer, Reflection, or Proposer roles.

The complete GH-E1 reference/evolution directories are byte-preserved, sealed local research
evidence and are intentionally excluded from Git because they contain raw Agent workspaces and
optional machine-local diagnostics. Consequently, a clean clone does not run the full raw-evidence
projection in `scripts/run_gh_e1_gates.py` and does not claim to verify the unpublished 485/579
object run seals. Historical stage-construction and recovery scripts were retired after sealing;
the published append-only report/stage artifacts are the public reproducibility contract.

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
- Algorithm effect has two separately scoped `slack-gif-creator` observations: v0.1 produced one
  deployable candidate at held-out `+0.12427` with three wins; GH-E1 independently produced
  `no_strict_improvement` and an empty frontier after a protected-category validation regression.
- The accepted edit changed one bounded `SKILL.md` instruction node. The run does not establish
  cross-file superiority, graph-method superiority, or generalization across Skills/models/seeds.
