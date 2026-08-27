<p align="center">
  <img src="docs/assets/readme-hero.svg" alt="GEPASE — evidence-first evolution for complete Agent Skill packages" width="100%" />
</p>

<p align="center">
  <a href="README_zh.md">简体中文</a> ·
  <a href="artifacts/runs/f4e-slack-gif-creator-relative-efficiency-v2-report/index.html">Interactive report</a> ·
  <a href="docs/reproduction.md">Reproduce</a> ·
  <a href="learning.html">Learning guide</a>
</p>

<p align="center">
  <a href="https://github.com/luckyxinggo/GEPASE/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/luckyxinggo/GEPASE/actions/workflows/ci.yml/badge.svg" /></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white" />
  <img alt="263 tests" src="https://img.shields.io/badge/tests-263%20passed-16a34a" />
  <a href="LICENSE"><img alt="Apache 2.0" src="https://img.shields.io/badge/license-Apache--2.0-2563eb" /></a>
</p>

GEPASE is an evidence-first Python framework for **evaluating and evolving complete Agent Skill
packages**. Instead of treating only the prompt as mutable, it models `SKILL.md`, references,
scripts, assets, metadata, and their dependency graph as external trainable state while keeping
model weights frozen.

It combines real Agent execution, blind evaluation, GEPA-style reflective/Pareto search,
graph-guided typed patches, held-out validation, and conditional same-package Merge in one
auditable mainline.

> **Architecture boundary:** GEPASE is not another general Agent runtime. Codex, Claude Code, or
> another host executes isolated roles; GEPASE Core owns evidence, scores, candidates, search
> state, checkpoints, and acceptance.

## Why GEPASE?

| Complete candidate state | Evidence before claims | Structured evolution | Fail-closed deployment |
|---|---|---|---|
| Snapshot and reason over instructions, references, code, assets, metadata, and dependencies. | Compare no-skill, original, and candidate in isolated contexts with task-native outputs. | Localize failures with static+observed graphs and apply bounded, typed `PackagePatch` edits. | Admit only strict held-out improvements; preserve rejections, incomplete work, lineage, usage, and hashes. |

Most Skill tooling answers “can an Agent load this package?” GEPASE is designed to answer the
harder questions: **did it improve the task, why did it fail, what changed, and is the result safe
to deploy?**

## A real end-to-end result

The latest public run used the pinned multi-file `slack-gif-creator` Skill, one frozen EvalPlan,
and a bounded five-candidate search. It exercised generation-1, parent-bound generation-2, and
same-package Merge through the same Controller and Gate mainline.

| Search realized | Gate funnel | Deployable frontier | Best held-out result |
|---|---|---|---|
| 2 generation-1 + 2 generation-2 + 1 Merge | 5 candidates → 4 train-admitted → 3 validation-completed | **2 candidates** | **+0.09920** validation delta · relative cost **1.83254** |

The second deployable candidate achieved `+0.07906` validation delta at relative cost `1.93702`.
Both won all three held-out cases. Effective patches in this run still concentrate in `SKILL.md`,
so this is **not** evidence of successful cross-file optimization.

![F4e search lineage](docs/assets/f4e-search-lineage.svg)

### One held-out task, four real outputs

| no-skill | original Skill | deployable rank 1 | deployable rank 2 |
|---|---|---|---|
| ![No-skill GIF](artifacts/runs/f4e-slack-gif-creator-relative-efficiency-v2-report/evidence/gifs/44-gif-25cec5b0a95e5d31469c63ba.gif) | ![Original Skill GIF](artifacts/runs/f4e-slack-gif-creator-relative-efficiency-v2-report/evidence/gifs/45-gif-25bfb2d03163b320a64b30cf.gif) | ![Rank-1 candidate GIF](artifacts/runs/f4e-slack-gif-creator-relative-efficiency-v2-report/evidence/gifs/42-gif-4007d0e2d4dd38f41ac4437f.gif) | ![Rank-2 candidate GIF](artifacts/runs/f4e-slack-gif-creator-relative-efficiency-v2-report/evidence/gifs/41-gif-2de981b5ec17e4a6cb54b2ae.gif) |

The [self-contained Chinese narrative report](artifacts/runs/f4e-slack-gif-creator-relative-efficiency-v2-report/index.html)
shows all 51 task-native GIFs, the search lineage, six-dimensional scores, quality–cost trade-offs,
failure → graph → Patch → Gate traces, runtime accounting, provenance, and both deployable archives.

## How the engineering pipeline works

![GEPASE end-to-end pipeline](docs/assets/evolution-loop.svg)

1. **Compile the Package.** Build an immutable snapshot, typed IR, parse coverage, and a static
   dependency graph for the complete Skill directory.
2. **Freeze the evaluation contract.** Review and hash cases, fixtures, train/validation splits,
   rubrics, scoring, host/model, seed, timeout, and tool policy.
3. **Build a paired reference.** Run no-skill and original Skill in isolated contexts; ingest E2
   task outputs, deterministic E3 assertions, blind grading, comparison, and analysis.
4. **Turn failures into graph evidence.** Overlay observed package access onto the static graph,
   derive failure slices, and rank relevant targets without granting semantic guesses authority.
5. **Propose a bounded edit.** Reflection and the Proposer produce a typed `PackagePatch`; Core
   validates target scope, preconditions, dependency closure, impact, and atomic rollback.
6. **Materialize and evaluate a Candidate.** Candidate bundles bind Package, parent, Patch,
   application, graph, workspace, run metadata, and an immutable intermediate seal.
7. **Search without erasing failures.** Train Gate, task-level GEPA feedback, Pareto selection,
   rejected-edit memory, generation-2, and eligible same-package Merge share one Controller.
8. **Validate once, then report.** Held-out evidence cannot modify the candidate. Gate 3 applies
   strict improvement and protected-category floors before exporting the deployable frontier.

## Design choices that matter

- **Typed boundaries, isolated roles.** Executor, Independent Grader, Comparator, Analyzer,
  Reflection, and Proposer exchange only validated WorkItems/submissions; no hidden shared chat
  history decides the winner.
- **The graph changes decisions.** The active selector consumes only verified static and observed
  layers for localization, mutation scope, blast radius, dependency closure, and merge conflict
  analysis. It is not a decorative visualization.
- **Assertions are not overall quality.** `TaskScoreVector` keeps task correctness, output quality,
  skill gain, reliability, efficiency, and package quality separate.
- **Resumability is part of correctness.** Reservations, Host attempts, typed role failures,
  checkpoints, Candidate seals, and artifact indexes are append-only and hash-bound.
- **Package-aware does not mean unsafe mutation.** Text, code, and metadata have bounded typed edit
  operations. Binary assets are visible to snapshots, graphs, evidence, and Gates but remain
  immutable until a dedicated validator exists.
- **Merge is deliberately narrow.** Only compatible branches of the same Package, snapshot, and
  common root may merge. Cross-package Merge is a hard error.

New `2.0.0` evolution configs default to `relative_v2` efficiency, and new report configs default
to the `narrative_v1` presentation used above. `v1_legacy` and `classic` remain explicit options;
historical `1.0.0` configs retain their original interpretation and fingerprints.

## Quick start

Requirements: Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync --all-extras --frozen
uv run gepase --version
uv run gepase doctor --format json
```

Run a deterministic offline smoke test—no Agent or API call:

```bash
uv run gepase mock run \
  --config configs/examples/mock.yaml \
  --output artifacts/local/mock-run \
  --format json
uv run gepase artifact verify artifacts/local/mock-run --format json
```

Verify the latest published report in a clean clone:

```bash
uv run gepase artifact verify \
  artifacts/runs/f4e-slack-gif-creator-relative-efficiency-v2-report \
  --format json
```

The full [reproduction guide](docs/reproduction.md) covers EvalPlan review, Agent-native work
export/submit/ingest, resume, optional role-scoped Headless routing, report verification, and
deployable Package export.

## Repository architecture

| Path | Responsibility |
|---|---|
| [`src/gepase/package/`](src/gepase/package/) | Package snapshots, Markdown/Python/shell/config IR, graph layers, failure slices, graph diffs |
| [`src/gepase/evals/`](src/gepase/evals/) | EvalPlan, isolated role work, E2/E3 evidence, paired scoring, statistics, ledger, runtime |
| [`src/gepase/optimizer/`](src/gepase/optimizer/) | Candidate, GEPA/Pareto, generation-2, strict Gate, recovery, same-package Merge |
| [`src/gepase/mutation/`](src/gepase/mutation/) | Typed `PackagePatch`, target scope, impact checks, atomic apply/rollback |
| [`src/gepase/store/`](src/gepase/store/) | Artifact, Candidate, checkpoint, pool, rejection, and proposal stores |
| [`src/gepase/reporting/`](src/gepase/reporting/) | Read-only reports derived from sealed evidence |
| [`.agents/skills/gepase-orchestrator/`](.agents/skills/gepase-orchestrator/) | Thin Agent-host adapter; never a second evaluator or search system |
| [`tests/`](tests/) · [`schemas/`](schemas/) | Unit/integration/fault/contract coverage and generated exchange schemas |

## Evidence, status, and limits

GEPASE separates three claims instead of blending them:

| Claim | Public evidence |
|---|---|
| **Code implemented** | One Python package contains the Package, Eval, Graph, Candidate, Patch, Controller, Gate, Runtime, Store, and reporting mainline. |
| **Engineering mechanism tested** | 263 tests plus Ruff, Pyright, schema idempotence, security, license, link, artifact-seal, resume, isolation, and fault checks. |
| **Algorithm effect observed** | The public relative-efficiency v2 result is `strict_improvement` with a two-candidate deployable frontier. |

Current effect evidence covers **one public Skill, one frozen EvalPlan, one host/model snapshot, and
one bounded run**. It does not establish cross-Skill, cross-model, multi-seed, graph-vs-random, or
Package-vs-SKILL-only superiority. An earlier independent graph-hardening run closed honestly as
`no_strict_improvement`; its [sealed report](artifacts/runs/gh-e1-slack-gif-creator-report/final/index.html)
is retained rather than hidden.

Raw Agent workspaces and machine-local research evidence remain outside Git. A clean clone can
verify curated report/stage seals, but does not claim to include unpublished raw evolution runs.
See [artifact policy](docs/artifacts.md), [benchmark scope](docs/benchmark.md), and the authoritative
[project state / decision log](state.md).

## Documentation

- [Evaluation model](docs/evaluation.md) · [Agent-native orchestration](docs/orchestrator.md)
- [Configuration](docs/configuration.md) · [Artifact contract](docs/artifacts.md)
- [Reproduction](docs/reproduction.md) · [Benchmark scope](docs/benchmark.md)
- [Project state](state.md) · [Algorithm learning guide](learning.html)
- [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md)

## Method lineage

GEPASE uses the pinned `gepa==0.1.4` implementation as its reflective-search skeleton. Its
evaluation design is informed by Anthropic skill-creator; bounded edits and validation discipline
by SkillOpt; iterative execution history by Darwin-skill; and the frozen-model/external-policy
view by Heuristic Learning. GEPASE extends this lineage by making the **complete Package plus its
dependency graph** the candidate state and connecting it to typed evidence, Patch, Merge, and
held-out acceptance. Exact reuse and extension boundaries are documented in
[learning.html](learning.html).

The public canary is pinned from [`anthropics/skills`](https://github.com/anthropics/skills) at
commit `fa0fa64bdc967915dc8399e803be67759e1e62b8`; upstream provenance, Apache-2.0 attribution, and
tree/blob hashes are preserved under [`benchmarks/canaries/slack-gif-creator/`](benchmarks/canaries/slack-gif-creator/).

## License

GEPASE is licensed under [Apache-2.0](LICENSE). Vendored or pinned public fixtures retain their own
license and provenance files.
