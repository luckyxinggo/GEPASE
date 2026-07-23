from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from gepase.optimizer.candidate import build_seed_candidate, derive_candidate
from gepase.optimizer.components import ComponentKind


def test_seed_candidate_exposes_ir_backed_package_components() -> None:
    candidate = build_seed_candidate(
        Path.cwd(),
        "benchmarks/skills/structured-report-builder",
        run_id="candidate-test",
    )

    assert [item.kind for item in candidate.components[:3]] == [
        ComponentKind.SKILL_INSTRUCTIONS,
        ComponentKind.REFERENCE_CHUNK,
        ComponentKind.SCRIPT_UNIT,
    ]
    assert all(item.source_node_id.startswith("node-") for item in candidate.components)
    assert candidate.content_hash == candidate.snapshot_hash
    assert candidate.parent_ids == ()


def test_candidate_is_immutable_and_derivation_preserves_parent_lineage() -> None:
    candidate = build_seed_candidate(
        Path.cwd(),
        "benchmarks/skills/structured-report-builder",
        run_id="candidate-test",
    )
    instruction = candidate.components[0]
    with pytest.raises(TypeError):
        candidate.component_map[instruction.component_id] = instruction  # type: ignore[index]
    with pytest.raises(ValidationError):
        candidate.generation = 9  # type: ignore[misc]

    child = derive_candidate(
        candidate,
        {instruction.component_id: instruction.content + "\nAdded validation instruction.\n"},
        operator="reflective_mutation",
        run_id="candidate-test",
    )

    assert child.parent_ids == (candidate.candidate_id,)
    assert child.generation == 1
    assert child.content_hash != candidate.content_hash
    assert candidate.component_map[instruction.component_id].content == instruction.content


def test_candidate_derivation_rejects_noop_and_unknown_component() -> None:
    candidate = build_seed_candidate(
        Path.cwd(),
        "benchmarks/skills/structured-report-builder",
        run_id="candidate-test",
    )
    component = candidate.components[0]

    with pytest.raises(ValueError, match="no-op"):
        derive_candidate(
            candidate,
            {component.component_id: component.content},
            operator="reflective_mutation",
            run_id="candidate-test",
        )
    with pytest.raises(ValueError, match="unknown component"):
        derive_candidate(
            candidate,
            {"component-unknown": "replacement"},
            operator="reflective_mutation",
            run_id="candidate-test",
        )
