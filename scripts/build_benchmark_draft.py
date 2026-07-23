"""Deterministically build the public S1 benchmark draft and fixtures."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Literal

from gepase.evals.schema import (
    AssertionSpec,
    BenchmarkManifest,
    BenchmarkPackage,
    CaseProvenance,
    EvidenceTier,
    SkillCapabilityManifest,
    TaskCase,
)
from gepase.evals.split import build_split_manifest
from gepase.store.artifacts import canonical_json_bytes

ROOT = Path.cwd().resolve()
BENCHMARKS = ROOT / "benchmarks"
SKILL_IDS = (
    "structured-report-builder",
    "tabular-context-builder",
    "policy-evidence-evaluator",
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def split_for_group(group: int) -> Literal["train", "validation", "test"]:
    if group < 6:
        return "train"
    if group < 8:
        return "validation"
    return "test"


def difficulty(group: int, variant: int) -> Literal["easy", "medium", "hard"]:
    value = (group + variant) % 5
    return "easy" if value < 2 else "medium" if value < 4 else "hard"


def assertion(
    identifier: str,
    family: Literal[
        "file_exists",
        "file_contains",
        "json_equals",
        "json_range",
        "forbidden_text",
        "html_contract",
    ],
    parameters: dict[str, Any],
    weight: float = 1.0,
) -> AssertionSpec:
    return AssertionSpec(
        assertion_id=identifier,
        family=family,
        parameters=parameters,
        weight=weight,
    )


def common_case(
    *,
    identifier: str,
    skill_id: str,
    prompt: str,
    fixture_ref: str,
    fixture_payload: dict[str, Any],
    assertions: tuple[AssertionSpec, ...],
    group: int,
    variant: int,
    category: str,
    capabilities: tuple[str, ...],
    output: dict[str, Any],
) -> TaskCase:
    return TaskCase(
        id=identifier,
        skill_id=skill_id,
        capability_manifest_ref=f"benchmarks/skills/{skill_id}/capability-manifest.json",
        prompt=prompt,
        input={"fixture_ref": fixture_ref, "requested_output": output},
        fixture_ref=fixture_ref,
        fixture_sha256=hashlib.sha256(canonical_json_bytes(fixture_payload)).hexdigest(),
        allowed_evidence_tiers=tuple(EvidenceTier),
        minimum_acceptance_tier=EvidenceTier.E3_EXECUTABLE,
        assertions=assertions,
        judge_rubric_ref="benchmarks/rubrics/blind-quality-v1.json",
        category=category,
        difficulty=difficulty(group, variant),
        risk_level="low",
        required_capability=capabilities,
        leakage_group=f"{skill_id}-family-{group:02d}",
        split=split_for_group(group),
        deterministic_weight=0.8,
        judge_weight=0.2,
        provenance=CaseProvenance(
            kind="synthetic",
            reference=f"gepase-generator/{skill_id}/family-{group:02d}/variant-{variant:02d}",
            license="Apache-2.0",
            generator_version="1.0.0",
        ),
    )


def report_case(group: int, variant: int) -> tuple[TaskCase, dict[str, Any]]:
    regions = ("North", "South", "East", "West", "Central")
    region = regions[(group + variant) % len(regions)]
    quarter = (group % 4) + 1
    title = f"{region} Performance Review Q{quarter} Scenario {group:02d}-{variant:02d}"
    revenue = 1200 + group * 137 + variant * 29
    orders = 40 + group * 3 + variant
    fixture = {
        "title": title,
        "subtitle": "Auditable synthetic operating summary",
        "metrics": [
            {"label": "Revenue", "value": revenue, "unit": " USD"},
            {"label": "Orders", "value": orders, "unit": ""},
            {"label": "Conversion", "value": round(0.12 + group * 0.005, 3), "unit": ""},
        ],
        "sections": [
            {
                "heading": f"Regional detail {region}",
                "summary": f"Synthetic evidence slice {group}-{variant}.",
                "table": {
                    "columns": ["Region", "Orders", "Revenue"],
                    "rows": [
                        {"Region": region, "Orders": orders, "Revenue": revenue},
                        {"Region": "Reference-A", "Orders": orders - 3, "Revenue": revenue - 90},
                        {"Region": "Reference-B", "Orders": orders + 2, "Revenue": revenue + 45},
                    ],
                },
            }
        ],
        "provenance": {"source": f"synthetic-report-{group:02d}-{variant:02d}"},
    }
    identifier = f"structured-report-{group:02d}-{variant:02d}"
    fixture_ref = f"benchmarks/structured-report-builder/fixtures/{identifier}.json"
    assertions = (
        assertion("report-exists", "file_exists", {"path": "report.html", "min_bytes": 500}),
        assertion(
            "report-values",
            "file_contains",
            {
                "path": "report.html",
                "values": [title, "Revenue", str(revenue), f"Regional detail {region}"],
            },
        ),
        assertion(
            "report-contract",
            "html_contract",
            {
                "path": "report.html",
                "regex": ["<h1[^>]*>", "<table", "<footer>Source:"],
                "no_remote_assets": True,
            },
        ),
    )
    prompt = (
        f"Create a self-contained accessible HTML report for {title}. Preserve the exact values "
        f"from fixture {fixture_ref}, including Revenue {revenue} and Orders {orders}; "
        "write report.html."
    )
    case = common_case(
        identifier=identifier,
        skill_id="structured-report-builder",
        prompt=prompt,
        fixture_ref=fixture_ref,
        fixture_payload=fixture,
        assertions=assertions,
        group=group,
        variant=variant,
        category="structured-html-report",
        capabilities=("render_html", "preserve_values", "validate_artifact"),
        output={"path": "report.html", "media_type": "text/html"},
    )
    return case, fixture


def tabular_case(group: int, variant: int) -> tuple[TaskCase, dict[str, Any]]:
    first_rows = 12 + ((group + variant) % 5)
    second_rows = 6 + (variant % 3)
    primary = f"orders_{group:02d}_{variant:02d}"
    lookup = f"segments_{group:02d}_{variant:02d}"
    fixture = {
        "title": f"Operations Context Pack {group:02d}-{variant:02d}",
        "tables": [
            {
                "name": primary,
                "columns": ["record_id", "segment", "amount"],
                "rows": [
                    {
                        "record_id": f"R{group:02d}{variant:02d}{row:03d}",
                        "segment": ("alpha", "beta", "gamma")[row % 3],
                        "amount": group * 100 + variant * 10 + row,
                    }
                    for row in range(first_rows)
                ],
            },
            {
                "name": lookup,
                "columns": ["segment", "owner"],
                "rows": [
                    {"segment": f"segment-{row}", "owner": f"owner-{group}-{variant}-{row}"}
                    for row in range(second_rows)
                ],
            },
        ],
    }
    identifier = f"tabular-context-{group:02d}-{variant:02d}"
    fixture_ref = f"benchmarks/tabular-context-builder/fixtures/{identifier}.json"
    assertions = (
        assertion("navigation-exists", "file_exists", {"path": "navigation.md"}),
        assertion("manifest-exists", "file_exists", {"path": "manifest.json"}),
        assertion(
            "primary-row-count",
            "json_equals",
            {"path": "manifest.json", "pointer": "/tables/0/row_count", "expected": first_rows},
        ),
        assertion(
            "table-navigation",
            "file_contains",
            {"path": "navigation.md", "values": [primary, lookup, f"{first_rows} rows"]},
        ),
        assertion(
            "complete-csv-exists",
            "file_exists",
            {"path": f"tables/{primary.replace('_', '-')}.csv", "min_bytes": 40},
        ),
    )
    prompt = (
        f"Build a bounded context pack from {fixture_ref}. Preserve all {first_rows} rows in the "
        f"{primary} CSV, preview at most 10 rows in Markdown, and write navigation plus manifest."
    )
    case = common_case(
        identifier=identifier,
        skill_id="tabular-context-builder",
        prompt=prompt,
        fixture_ref=fixture_ref,
        fixture_payload=fixture,
        assertions=assertions,
        group=group,
        variant=variant,
        category="tabular-context-pack",
        capabilities=("write_csv", "bound_context", "record_provenance"),
        output={"directory": ".", "manifest": "manifest.json"},
    )
    return case, fixture


def policy_case(group: int, variant: int) -> tuple[TaskCase, dict[str, Any]]:
    direction = "gte" if (group + variant) % 2 == 0 else "lte"
    threshold = 45 + group + variant
    records = [
        {
            "id": f"P{group:02d}{variant:02d}{row:03d}",
            "score": 25 + ((row * 11 + group * 7 + variant * 3) % 60),
            "segment": ("new", "returning", "priority")[row % 3],
        }
        for row in range(18 + (group % 4))
    ]
    accepted = sum(
        record["score"] >= threshold if direction == "gte" else record["score"] <= threshold
        for record in records
    )
    fixture = {
        "policy_id": f"policy-{group:02d}-{variant:02d}",
        "threshold": threshold,
        "direction": direction,
        "records": records,
    }
    identifier = f"policy-evidence-{group:02d}-{variant:02d}"
    fixture_ref = f"benchmarks/policy-evidence-evaluator/fixtures/{identifier}.json"
    assertions = (
        assertion("analysis-exists", "file_exists", {"path": "analysis.json"}),
        assertion("report-exists", "file_exists", {"path": "report.md"}),
        assertion(
            "accepted-count",
            "json_equals",
            {"path": "analysis.json", "pointer": "/summary/accepted", "expected": accepted},
        ),
        assertion(
            "total-count",
            "json_equals",
            {"path": "analysis.json", "pointer": "/summary/total", "expected": len(records)},
        ),
        assertion(
            "policy-id",
            "file_contains",
            {"path": "report.md", "values": [fixture["policy_id"], direction, str(threshold)]},
        ),
    )
    prompt = (
        f"Evaluate policy {fixture['policy_id']} from {fixture_ref} using the configured "
        f"{direction} "
        f"threshold {threshold}; write analysis.json and report.md without adding recommendations."
    )
    case = common_case(
        identifier=identifier,
        skill_id="policy-evidence-evaluator",
        prompt=prompt,
        fixture_ref=fixture_ref,
        fixture_payload=fixture,
        assertions=assertions,
        group=group,
        variant=variant,
        category="threshold-policy-evidence",
        capabilities=("apply_threshold", "aggregate_segments", "preserve_decisions"),
        output={"directory": ".", "analysis": "analysis.json"},
    )
    return case, fixture


def package_snapshot_hash(skill_root: Path) -> str:
    digest = hashlib.sha256()
    excluded = {"provenance.json", "capability-manifest.json"}
    for path in sorted(item for item in skill_root.rglob("*") if item.is_file()):
        if path.name in excluded:
            continue
        digest.update(path.relative_to(skill_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def write_public_metadata(skill_id: str) -> None:
    skill_root = BENCHMARKS / "skills" / skill_id
    shutil.copy2(ROOT / "LICENSE", skill_root / "LICENSE")
    profiles = {
        "structured-report-builder": (
            ("artifact_generation", "contains_python", "deterministic_renderer"),
            ("python", "filesystem"),
            ("writes_html",),
            "JSON report fixtures",
            "rendered HTML plus deterministic DOM assertions",
        ),
        "tabular-context-builder": (
            ("artifact_generation", "contains_python", "tabular_processing"),
            ("python", "filesystem"),
            ("writes_context_pack",),
            "multi-table JSON fixtures",
            "manifest, CSV hash, and preview replay",
        ),
        "policy-evidence-evaluator": (
            ("contains_python", "policy_analysis", "structured_output"),
            ("python", "filesystem"),
            ("writes_analysis",),
            "synthetic record and threshold fixtures",
            "analysis JSON and assertion replay",
        ),
    }
    labels, tools, side_effects, fixture, replay = profiles[skill_id]
    capability = SkillCapabilityManifest(
        skill_id=skill_id,
        source_manifest_ref=f"benchmarks/skills/{skill_id}/provenance.json",
        labels=labels,
        required_tools=tools,
        required_services=(),
        required_secrets=(),
        side_effects=side_effects,
        fixture_strategy=fixture,
        replay_strategy=replay,
        supported_evidence_tiers=tuple(EvidenceTier),
        degradation_reasons=(),
    )
    write_json(skill_root / "capability-manifest.json", capability.model_dump(mode="json"))
    provenance = {
        "schema_version": "1.0.0",
        "skill_id": skill_id,
        "license": "Apache-2.0",
        "origin": "independently authored public benchmark equivalent",
        "private_content_copied": False,
        "source_snapshot_hash": package_snapshot_hash(skill_root),
    }
    write_json(skill_root / "provenance.json", provenance)


def write_cards() -> None:
    descriptions = {
        "structured-report-builder": "Accessible HTML generation with exact-value preservation.",
        "tabular-context-builder": "Bounded previews backed by complete, hashed CSV evidence.",
        "policy-evidence-evaluator": (
            "Threshold decisions and segment aggregates with record provenance."
        ),
    }
    for skill_id, description in descriptions.items():
        card = (
            f"# {skill_id} benchmark\n\n{description}\n\n"
            "- Source: deterministic synthetic fixtures generated by GEPASE.\n"
            "- License: Apache-2.0.\n"
            "- Cases: 50 (30 train, 10 validation, 10 test).\n"
            "- Minimum acceptance evidence: E3 executable assertions.\n"
            "- Score composition: 80% deterministic assertions, 20% blind quality rubric.\n"
            "- Limitation: fixtures isolate artifact correctness; they do not represent every "
            "production integration or subjective user preference.\n"
        )
        path = BENCHMARKS / skill_id / "benchmark_card.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(card, encoding="utf-8")


def write_selection() -> None:
    selection_root = BENCHMARKS / "selection"
    selection_root.mkdir(parents=True, exist_ok=True)
    rows = [
        ("local-skill-001", "structured-report-builder", 5, 5, 5, 5, "selected"),
        ("local-skill-002", "policy-evidence-evaluator", 5, 5, 5, 5, "selected"),
        ("local-skill-003", "none", 4, 1, 1, 2, "private external service; local-real only"),
        ("local-skill-004", "none", 4, 3, 3, 3, "subjective judge share too high for v1"),
        ("local-skill-005", "tabular-context-builder", 5, 5, 5, 5, "selected"),
    ]
    with (selection_root / "skill_selection.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "private_alias",
                "public_equivalent",
                "real_value",
                "public_safety",
                "deterministic_oracle",
                "evidence_tier_coverage",
                "decision",
            ]
        )
        writer.writerows(rows)
    (selection_root / "decision.md").write_text(
        "# S1 public benchmark selection\n\n"
        "Selection uses capability and failure-mode coverage rather than mutually exclusive Skill "
        "types. Three independently authored public equivalents cover structured HTML artifacts, "
        "bounded tabular context, and deterministic policy evidence. No private Skill text, data, "
        "absolute path, internal service name, or credential is copied. One external-service "
        "package remains local-real only; one subjective frontend package is deferred because v1 "
        "requires at "
        "least 70% deterministic scoring.\n",
        encoding="utf-8",
    )


def execute() -> None:
    factories = {
        "structured-report-builder": report_case,
        "tabular-context-builder": tabular_case,
        "policy-evidence-evaluator": policy_case,
    }
    all_cases: list[TaskCase] = []
    for skill_id, factory in factories.items():
        package_root = BENCHMARKS / skill_id
        if package_root.exists():
            shutil.rmtree(package_root)
        fixtures_root = package_root / "fixtures"
        fixtures_root.mkdir(parents=True)
        cases: list[TaskCase] = []
        ledger: list[dict[str, Any]] = []
        for group in range(10):
            for variant in range(5):
                case, fixture = factory(group, variant)
                fixture_path = ROOT / case.fixture_ref
                write_json(fixture_path, fixture)
                cases.append(case)
                ledger.append(
                    {
                        "case_id": case.id,
                        "kind": case.provenance.kind,
                        "reference": case.provenance.reference,
                        "license": case.provenance.license,
                        "leakage_group": case.leakage_group,
                    }
                )
        dataset = "".join(
            json.dumps(case.model_dump(mode="json"), ensure_ascii=False, sort_keys=True) + "\n"
            for case in cases
        )
        (package_root / "dataset.jsonl").write_text(dataset, encoding="utf-8")
        ledger_text = "".join(json.dumps(item, sort_keys=True) + "\n" for item in ledger)
        (package_root / "source_ledger.jsonl").write_text(ledger_text, encoding="utf-8")
        all_cases.extend(cases)
    for skill_id in SKILL_IDS:
        write_public_metadata(skill_id)
    write_cards()
    write_selection()
    rubric = {
        "schema_version": "1.0.0",
        "rubric_id": "blind-quality-v1",
        "blind_fields": ["candidate_id", "variant", "parents", "optimizer", "expected_answer"],
        "dimensions": [
            {"name": "instruction_adherence", "weight": 0.5},
            {"name": "clarity", "weight": 0.3},
            {"name": "artifact_usability", "weight": 0.2},
        ],
    }
    write_json(BENCHMARKS / "rubrics/blind-quality-v1.json", rubric)
    split_manifest = build_split_manifest(all_cases, "draft-v1")
    write_json(BENCHMARKS / "splits/draft-v1.json", split_manifest.model_dump(mode="json"))
    packages = tuple(
        BenchmarkPackage(
            skill_id=skill_id,
            skill_path=f"benchmarks/skills/{skill_id}",
            dataset_path=f"benchmarks/{skill_id}/dataset.jsonl",
            capability_manifest_ref=f"benchmarks/skills/{skill_id}/capability-manifest.json",
            benchmark_card_ref=f"benchmarks/{skill_id}/benchmark_card.md",
            provenance_ref=f"benchmarks/skills/{skill_id}/provenance.json",
            license="Apache-2.0",
            case_count=50,
        )
        for skill_id in SKILL_IDS
    )
    manifest = BenchmarkManifest(
        version="draft-v1",
        name="GEPASE Public Skill Package Benchmark",
        packages=packages,
        split_manifest_ref="benchmarks/splits/draft-v1.json",
        rubric_refs=("benchmarks/rubrics/blind-quality-v1.json",),
        created_by="scripts/build_benchmark_draft.py@1.0.0",
    )
    write_json(BENCHMARKS / "manifest-draft.json", manifest.model_dump(mode="json"))
    write_json(ROOT / "schemas/task_case.schema.json", TaskCase.model_json_schema())
    print(json.dumps({"packages": len(packages), "cases": len(all_cases)}, sort_keys=True))


if __name__ == "__main__":
    execute()
