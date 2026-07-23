# Development Guide

## Supported environment

- Python 3.11 or newer
- `uv` for dependency and lockfile management
- `ruff`, `pyright`, and `pytest` through the development extra

## Layering

`src/gepase` contains reusable core logic. CLI commands call services; they do not implement a
second copy of configuration, evaluation, or storage behavior. Agent-facing Skills remain thin
adapters and cannot own candidate or experiment state.

R1 freezes one public model for each Core boundary. Package Graph, Candidate, PackagePatch, Gate,
EvolutionPool, and same-package merge algorithms are reusable components; stage-specific S5/S7/
S7.6/S8 controllers were removed. R4 reconnects those components through the single
`R4EvolutionController`; do not add another search or reporting subsystem.

R3 extends the single Eval Core rather than adding a second evaluator. Domain-neutral role models,
isolation and scoring live under `src/gepase/evals/`; the GIF-specific oracle remains under the
public canary. The completed paired run is negative on mean skill gain and must remain available as
immutable R4 input evidence. Do not rewrite it to manufacture a positive baseline.

R4 reuses that anchor only after complete `ReferenceEvidenceKey` and artifact-hash verification,
then executes the fresh candidate side. Its accepted and rejected results are sealed under
`artifacts/runs/r4-slack-gif-creator-evolution/`; future reporting must consume those artifacts and
must not rerun or silently rescore the search.

R5 follows the same single-source rule. `gepase.reporting.CanaryReportBuilder` is a read-only
projection over sealed R2–R4 artifacts; it cannot dispatch an Agent role, propose a candidate, or
change a Gate decision. A build copies only the selected task-native GIFs and deployable Package,
records every source/destination hash in `evidence-manifest.json`, emits a dependency-free Chinese
HTML page, and seals the output with `ArtifactStore`. `report verify` recollects the upstream facts
and requires exact equality for the report payload, evidence manifest, copied assets, deployable
archive, required sections, and local-only script/style policy.

The report output directory is immutable: `report build` refuses to overwrite it. Regeneration
must target a new/empty directory after the caller has explicitly preserved or removed the old
output. Do not make the report another Candidate, evaluator, or experiment state owner.

## Verification

```bash
uv run ruff check .
uv run pyright src tests scripts
uv run pytest -q
uv run python scripts/check_secrets.py --format json
uv run python scripts/check_markdown_links.py
```
