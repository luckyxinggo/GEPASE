"""Privacy-preserving read-only inventory for the local Skill corpus."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from gepase.evals.schema import EvidenceTier, SkillCapabilityManifest, SkillSourceManifest

PROFILE_BY_NAME: dict[str, dict[str, Any]] = {
    "html-report-see": {
        "labels": ("artifact_parsing", "contains_python", "structured_output"),
        "tools": ("python", "filesystem"),
        "services": (),
        "secrets": (),
        "side_effects": ("writes_artifacts",),
        "fixture": "sanitized HTML fixtures",
        "replay": "content-addressed parser artifacts",
        "tiers": ("E0", "E1", "E2", "E3"),
        "degradation": (),
    },
    "quota-strategy-evaluator": {
        "labels": ("contains_python", "data_analysis", "artifact_generation"),
        "tools": ("python", "filesystem"),
        "services": ("optional-domain-review-skill",),
        "secrets": (),
        "side_effects": ("writes_reports",),
        "fixture": "synthetic tabular records and policy configs",
        "replay": "frozen aggregate evidence packs",
        "tiers": ("E0", "E1", "E2", "E3"),
        "degradation": ("domain review dependency replaced in public equivalent",),
    },
    "spark-hive-data-fetch": {
        "labels": ("contains_shell", "external_service", "high_side_effect"),
        "tools": ("shell", "spark", "hadoop", "yarn"),
        "services": ("private-data-platform",),
        "secrets": ("platform-credentials",),
        "side_effects": ("submits_remote_job", "writes_remote_data"),
        "fixture": "command-contract fixtures only",
        "replay": "sanitized manifest and status replay",
        "tiers": ("E0", "E1", "E3"),
        "degradation": ("E2 unavailable outside authorized private environment",),
    },
    "super-frontend": {
        "labels": ("artifact_generation", "subjective_output", "contains_python"),
        "tools": ("python", "filesystem"),
        "services": (),
        "secrets": (),
        "side_effects": ("writes_html", "writes_checkpoints"),
        "fixture": "self-contained HTML input fixtures",
        "replay": "render and DOM-contract assertions",
        "tiers": ("E0", "E1", "E2", "E3"),
        "degradation": ("visual quality retains bounded blind-judge component",),
    },
    "xlsx2llm": {
        "labels": ("contains_python", "artifact_parsing", "structured_output"),
        "tools": ("python", "filesystem", "spreadsheet-parser"),
        "services": ("optional-local-ocr",),
        "secrets": (),
        "side_effects": ("writes_context_pack",),
        "fixture": "synthetic workbook-equivalent fixtures",
        "replay": "frozen context packs and route assertions",
        "tiers": ("E0", "E1", "E2", "E3"),
        "degradation": ("external OCR disabled by default",),
    },
}


def package_hash(path: Path) -> tuple[str, int, int]:
    digest = hashlib.sha256()
    count = 0
    size = 0
    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = file_path.relative_to(path).as_posix()
        data = file_path.read_bytes()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
        count += 1
        size += len(data)
    return digest.hexdigest(), count, size


def tracked_private_files(root: Path) -> list[str]:
    process = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "skills_test"],
        check=False,
        capture_output=True,
    )
    return [item.decode() for item in process.stdout.split(b"\0") if item]


def audit_corpus(root: Path, corpus: Path) -> dict[str, Any]:
    packages = sorted(path for path in corpus.iterdir() if path.is_dir())
    sources: list[dict[str, Any]] = []
    capabilities: list[dict[str, Any]] = []
    missing_capabilities = 0
    for index, path in enumerate(packages, start=1):
        alias = f"local-skill-{index:03d}"
        source_hash, file_count, total_bytes = package_hash(path)
        source = SkillSourceManifest(
            source_id=alias,
            source_hash=source_hash,
            file_count=file_count,
            total_bytes=total_bytes,
            visibility="private-local",
            mutation_policy="read-only",
        )
        profile = PROFILE_BY_NAME.get(path.name)
        if profile is None:
            missing_capabilities += 1
            continue
        capability = SkillCapabilityManifest(
            skill_id=alias,
            source_manifest_ref=f"#/sources/{index - 1}",
            labels=profile["labels"],
            required_tools=profile["tools"],
            required_services=profile["services"],
            required_secrets=profile["secrets"],
            side_effects=profile["side_effects"],
            fixture_strategy=profile["fixture"],
            replay_strategy=profile["replay"],
            supported_evidence_tiers=tuple(EvidenceTier(tier) for tier in profile["tiers"]),
            degradation_reasons=profile["degradation"],
        )
        sources.append(source.model_dump(mode="json"))
        capabilities.append(capability.model_dump(mode="json"))
    tracked = tracked_private_files(root)
    source_hash_missing = sum(not source["source_hash"] for source in sources)
    return {
        "schema_version": "1.0.0",
        "corpus_id": "local-private-skill-corpus",
        "local_sources": len(packages),
        "source_hash_missing": source_hash_missing,
        "capability_manifest_missing": missing_capabilities,
        "tracked_private_files": len(tracked),
        "source_mutation": 0,
        "sources": sources,
        "capabilities": capabilities,
    }


def write_audit(root: Path, corpus: Path, output: Path) -> dict[str, Any]:
    result = audit_corpus(root, corpus)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return result
