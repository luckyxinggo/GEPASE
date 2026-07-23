# Agent-host submission protocols

## Eval Designer submission

Read `<run-dir>/designer-work-item.json` first. The isolated Designer may then read only its
repository-relative refs and the complete `skill_ref` Package. Its result must validate against
`schemas/eval_designer_submission.schema.json` and include:

- the exact `work_id`, `skill_id`, and `package_snapshot_hash`;
- one isolated `role_run` with stable host/context/task identity, timing, and non-empty usage;
- ordered `package_access` entries covering every `required_package_reads` path;
- distinct `trigger_cases` and `functional_cases`, including train/validation split metadata;
- concise Chinese design notes, without chain-of-thought.

Write the raw result under the run directory, then let Core ingest it:

```bash
uv run gepase eval submit-design \
  --run-dir <run-dir> \
  --submission <run-dir>/agent/designer-submission.json
```

The Designer must not see or produce review decisions. After Core renders `review.html`, export a
complete `review.json`, import it, and resume the same run:

```bash
uv run gepase eval import-review --run-dir <run-dir> --review <review.json>
uv run gepase eval resume --run-dir <run-dir>
```

## ExecutionBundle submission

## Workspace

Use `<run-dir>/workspaces/<work-id>/` for E2 artifacts and
`<run-dir>/execution-submissions/<work-id>.json` for the generated submission. Never reuse a sibling
workspace.

## Trace JSON

Both trace files contain a list or an object with `planned_trace` / `observed_trace`. Each item is:

```json
{"sequence": 0, "action": "inspect_fixture", "target": "repository-relative path", "tool": "read", "outcome": "completed"}
```

E1 uses planned steps only and must not claim outcomes. E2 observed steps name actual actions,
tools, repository-relative targets, and outcomes. Do not include chain-of-thought; record concise
operational events.

## Create and ingest

```bash
uv run gepase eval submit-work \
  --run-dir <run-dir> --work-id <work-id> \
  --host <host> --model <model> --host-task-id <task-id> --context-id <context-id> \
  --duration-ms <measured-or-estimated-ms> \
  --artifact-root <run-dir>/workspaces/<work-id> \
  --transcript <run-dir>/workspaces/<work-id>/transcript.md \
  --package-access <run-dir>/workspaces/<work-id>/package-access.json \
  --observed-trace <observed.json> \
  --input-tokens <count> --output-tokens <count> --tool-calls <count> \
  --token-count-kind <reported|estimated|unavailable> \
  --output <run-dir>/execution-submissions/<work-id>.json

uv run gepase eval ingest \
  --run-dir <run-dir> \
  --submission <run-dir>/execution-submissions/<work-id>.json
```

Omit artifact and observed arguments for E1. For a real failure, pass a typed `--failure-kind`
and a concise `--failure-detail`; do not fabricate output files.

For R3 Package access, use only these fields:

```json
{"sequence": 0, "kind": "read", "path": "SKILL.md", "node_id": "node-...", "bytes_loaded": 7841, "tokens_loaded": 1960}
```

Allowed `kind` values are `available`, `read`, and `executed`. Use the exact `path` and `node_id`
from the work item. Read events require real byte accounting and a declared token estimate. Keep
all text paths repository-relative, run Package imports with bytecode writing disabled, and never
put dependency caches or virtual environments inside the artifact workspace.

## Blind grading, comparison, and analysis

Prepare role work only after its upstream evidence is complete:

```bash
uv run gepase eval prepare-grading --run-dir <run-dir>
uv run gepase eval submit-grade --run-dir <run-dir> --submission <grade.json>
uv run gepase eval prepare-comparison --run-dir <run-dir>
uv run gepase eval submit-comparison --run-dir <run-dir> --submission <comparison.json>
uv run gepase eval prepare-analysis --run-dir <run-dir>
uv run gepase eval submit-analysis --run-dir <run-dir> --submission <analysis.json>
uv run gepase eval finalize-functional --run-dir <run-dir>
```

- Give an Independent Grader only one `grader-work-items/*.json` and its blind artifact refs. It
  returns every frozen rubric criterion, evidence refs, an exactly reproducible weighted score,
  feedback, and a unique `role_run`.
- Give a Comparator only one `comparator-work-items/*.json`. AB and BA are distinct fresh contexts;
  neither sees variant identity, deterministic scores, independent grades, or expected winner.
- Give an Analyzer only one `analyzer-work-items/*.json`. It may use the typed pair evidence and
  graph node hints in that item, but no candidate/search history or upstream conversations. Every
  target node and evidence ref must resolve to the exported graph/evidence.
- Write each raw role response outside another role's workspace. Core validates and stores the
  canonical copy; the host never computes TaskScoreVector or reconciles side identities.

## Host compatibility

- Codex: delegate isolated evaluation and reflection tasks to subagents and use the returned task
  identifier as `host_task_id`.
- Claude Code: use isolated Agent/Task workers and preserve their task identifier.
- Other hosts: they must provide isolated worker contexts, stable task IDs, file artifacts, and a
  non-empty usage record. If not, restrict them to E0/E1 rather than upgrading evidence tier.

## PackagePatch proposal submission

Export exactly one work item:

```bash
uv run gepase mutation next-work \
  --run-dir <run-dir> \
  --output <run-dir>/exports/<work-id>.json
```

The isolated worker returns only:

```json
{
  "summary": "concise evidence-linked summary",
  "operations": [
    {
      "operation_id": "op-stable-name",
      "op": "replace_markdown_block",
      "target_node_id": "exact exported node id",
      "path": "exact exported path",
      "precondition_hash": "exact exported content hash",
      "replacement": "complete replacement for the bounded block",
      "evidence_refs": ["exact exported evidence ref"],
      "expected_benefit": "bounded expected outcome",
      "regression_risk": "low",
      "rationale": "concise evidence-linked rationale"
    }
  ]
}
```

Submit and ingest:

```bash
uv run gepase mutation submit-proposal \
  --run-dir <run-dir> --work-id <work-id> \
  --proposal <proposal.json> --output <submission.json> \
  --host <host> --model <model> --host-task-id <task-id> \
  --duration-ms <milliseconds> --token-estimate <estimate>

uv run gepase mutation ingest \
  --run-dir <run-dir> --submission <submission.json>
```

Use `update_frontmatter` only for a `frontmatter` target,
`replace_python_function` only for a `function` target, and
`replace_markdown_block` only for section/instruction/reference-chunk targets. The Core rejects
all other type/target combinations during atomic application.
