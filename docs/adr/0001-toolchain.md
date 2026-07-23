# ADR-0001: Python Core and Evidence-First Toolchain

- Status: accepted
- Date: 2026-07-15

## Decision

Use Python 3.11+, `uv`, a `src/` package layout, Typer, Pydantic v2, pytest, Ruff, and Pyright.
Persist state through versioned schemas and content-addressed artifacts. Keep Agent orchestration
outside the deterministic core behind work-item and evidence contracts.

## Consequences

The Core remains executable without an LLM for configuration, replay, artifact verification, and
later deterministic gates. Agent-native and optional headless integrations must consume the same
schemas. Schema migrations require compatibility tests and a `state.md` Diff Log entry.

