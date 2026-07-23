"""Agent System Interface (ASI) construction from multi-fidelity package evidence."""

from __future__ import annotations

import difflib
import json
from collections.abc import Sequence

from pydantic import Field

from gepase.optimizer.candidate import PackageCandidate
from gepase.optimizer.gepa_adapter import CandidateEvaluation
from gepase.package.ir import Diagnostic, FailureSlice
from gepase.schemas.common import FrozenModel


class OmittedSection(FrozenModel):
    section_id: str
    reason: str
    token_estimate: int = Field(ge=0)


class ASIResult(FrozenModel):
    schema_version: str = "1.0.0"
    candidate_id: str
    component_ids: tuple[str, ...]
    reflective_dataset: dict[str, list[dict[str, object]]]
    token_budget: int = Field(ge=1)
    token_estimate: int = Field(ge=0)
    omitted_sections: tuple[OmittedSection, ...]
    required_evidence_coverage: float = Field(ge=0, le=1)
    planned_observed_confusion: int = Field(ge=0)


def _tokens(value: object) -> int:
    return max(1, len(json.dumps(value, ensure_ascii=False, sort_keys=True)) // 4)


def _diff(before: str, after: str, path: str) -> str:
    if before == after:
        return "no prior change"
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"parent/{path}",
            tofile=f"current/{path}",
            n=3,
        )
    )


class ASIBuilder:
    def build(
        self,
        candidate: PackageCandidate,
        evaluation: CandidateEvaluation,
        component_ids: tuple[str, ...],
        *,
        parent: PackageCandidate | None = None,
        failure_slice: FailureSlice | None = None,
        diagnostics: Sequence[Diagnostic] = (),
        token_budget: int = 16_000,
        max_examples: int = 5,
    ) -> ASIResult:
        if token_budget < 1_024:
            raise ValueError("ASI token budget must be at least 1024")
        components = candidate.component_map
        unknown = set(component_ids) - set(components)
        if unknown:
            raise ValueError(f"ASI references unknown components: {sorted(unknown)}")
        # GEPA reflection benefits from failures and at least one successful contrast.
        ordered = sorted(
            evaluation.rows,
            key=lambda row: (row.failure_kind is None, row.score, row.task_id),
        )
        selected = list(ordered[:max_examples])
        if selected and all(row.score < 0.999 for row in selected):
            best = max(evaluation.rows, key=lambda row: (row.score, row.task_id))
            if best not in selected:
                selected[-1] = best

        per_component_budget = max(512, token_budget // len(component_ids))
        dataset: dict[str, list[dict[str, object]]] = {}
        omissions: list[OmittedSection] = []
        confusion = 0
        covered = 0
        total_required = len(selected) * len(component_ids)
        for component_id in component_ids:
            component = components[component_id]
            parent_component = (
                parent.component_map.get(component_id) if parent is not None else None
            )
            base_context: dict[str, object] = {
                "component": {
                    "component_id": component.component_id,
                    "source_node_id": component.source_node_id,
                    "kind": component.kind.value,
                    "path": component.path,
                    "content": component.content,
                    "content_hash": component.content_hash,
                },
                "candidate": {
                    "candidate_id": candidate.candidate_id,
                    "content_hash": candidate.content_hash,
                    "parents": list(candidate.parent_ids),
                    "generation": candidate.generation,
                },
            }
            optional_sections: list[tuple[str, object]] = [
                (
                    "file_diff",
                    _diff(
                        parent_component.content if parent_component else component.content,
                        component.content,
                        component.path,
                    ),
                ),
                (
                    "graph_slice",
                    failure_slice.model_dump(mode="json") if failure_slice else None,
                ),
                (
                    "structural_diagnostics",
                    [item.model_dump(mode="json") for item in diagnostics],
                ),
            ]
            current_context = dict(base_context)
            for section_id, value in optional_sections:
                if value in (None, [], "no prior change"):
                    continue
                estimate = _tokens({section_id: value})
                if _tokens(current_context) + estimate <= per_component_budget // 2:
                    current_context[section_id] = value
                else:
                    omissions.append(
                        OmittedSection(
                            section_id=f"{component_id}:{section_id}",
                            reason="token_budget",
                            token_estimate=estimate,
                        )
                    )

            rows: list[dict[str, object]] = []
            for row in selected:
                if row.evidence_tier.value in {"E0", "E1"} and row.observed_trace:
                    confusion += 1
                if row.evidence_tier.value in {"E2", "E3"} and not row.observed_trace:
                    confusion += 1
                trace = {
                    "planned": [item.model_dump(mode="json") for item in row.planned_trace],
                    "observed": [item.model_dump(mode="json") for item in row.observed_trace],
                    "boundary": (
                        "planned-only"
                        if row.evidence_tier.value in {"E0", "E1"}
                        else "observed-execution"
                    ),
                }
                required: dict[str, object] = {
                    "Inputs": {
                        "task_id": row.task_id,
                        **current_context,
                    },
                    "Generated Outputs": {
                        "score": row.score,
                        "objectives": row.objective_scores,
                        "output": row.output,
                        "trace": trace,
                    },
                    "Feedback": {
                        "failure_kind": row.failure_kind,
                        "assertions": list(row.assertion_feedback),
                        "uncertainty": row.uncertainty,
                    },
                    "Evidence": {
                        "tier": row.evidence_tier.value,
                        "record_id": row.record_id,
                        "record_ref": row.record_ref,
                        "artifact_refs": [item.model_dump(mode="json") for item in row.artifacts],
                        "provenance": row.provenance,
                    },
                }
                estimate = _tokens(required)
                if _tokens(rows) + estimate <= per_component_budget:
                    rows.append(required)
                    covered += 1
                else:
                    omissions.append(
                        OmittedSection(
                            section_id=f"{component_id}:task:{row.task_id}",
                            reason="token_budget",
                            token_estimate=estimate,
                        )
                    )
            if not rows:
                raise ValueError(f"ASI budget cannot fit required evidence for {component_id}")
            dataset[component_id] = rows
        total_tokens = _tokens(dataset)
        if total_tokens > token_budget:
            raise ValueError("ASI builder exceeded its global token budget")
        return ASIResult(
            candidate_id=candidate.candidate_id,
            component_ids=component_ids,
            reflective_dataset=dataset,
            token_budget=token_budget,
            token_estimate=total_tokens,
            omitted_sections=tuple(omissions),
            required_evidence_coverage=(covered / total_required if total_required else 1.0),
            planned_observed_confusion=confusion,
        )
