"""Capability fact accessors kept separate from graph construction."""

from __future__ import annotations

from gepase.package.ir import CapabilityFacts


def capability_rows(facts: CapabilityFacts) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    for relation, values in (
        ("requires_host", facts.required_hosts),
        ("uses_tool", facts.required_tools),
        ("calls_external_service", facts.required_services),
        ("requires_secret", facts.required_secrets),
    ):
        rows.extend((relation, value) for value in values)
    return tuple(rows)
