# Contributing

GEPASE is evidence-first. A change is complete only when its contract, tests, generated evidence,
and relevant `state.md` Diff Log entry agree.

## Development workflow

1. Install with `uv sync --all-extras --frozen`.
2. Keep private Skill corpora outside Git. The local `skills_test/` directory is intentionally
   ignored.
3. Add typed schemas before adding a new state transition.
4. Run `uv run ruff check .`, `uv run pyright src tests scripts`, and `uv run pytest -q`.
5. Regenerate public schemas with `uv run python scripts/export_core_schemas.py` and review the
   diff rather than editing generated schema files by hand.
6. Run the secret, Markdown-link, license-attribution, artifact and report checks before opening a
   change. See [the reproduction guide](docs/reproduction.md).

Do not commit API keys, production prompts, private traces, absolute private paths, or benchmark
test labels. Negative results and rejected candidates are part of the evidence and must not be
silently removed.

Historical stage outputs are not public result fixtures. New local diagnostics belong under
`artifacts/local/`; only intentionally curated, redacted, content-indexed evidence should be added
under `artifacts/runs/` or `artifacts/stages/`.
