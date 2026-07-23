"""Secure, deterministic traversal and capability extraction for Skill packages."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

from gepase.package.ir import (
    CapabilityFacts,
    FileKind,
    PackageFile,
    PackageSnapshot,
    content_hash,
)

_BINARY_SUFFIXES = {
    ".7z",
    ".class",
    ".docx",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".pptx",
    ".pyc",
    ".tar",
    ".webp",
    ".xlsx",
    ".zip",
}


def _kind(path: Path) -> tuple[FileKind, str | None]:
    relative = path.as_posix()
    if relative == "SKILL.md":
        return FileKind.SKILL, None
    if relative == "LICENSE" or path.name.startswith("LICENSE."):
        return FileKind.LICENSE, None
    if relative.startswith("references/"):
        return FileKind.REFERENCE, None
    if relative.startswith(("scripts/", "core/")) and path.suffix.lower() == ".py":
        return FileKind.SCRIPT, None
    if relative.startswith("tests/") or path.name.startswith("test_"):
        return FileKind.TEST, None
    if relative.startswith("agents/") and path.suffix.lower() in {".yaml", ".yml", ".json"}:
        return FileKind.AGENT_CONFIG, None
    if path.name in {"capability-manifest.json", "provenance.json"}:
        return FileKind.METADATA, None
    if path.name in {
        "requirements.txt",
        "requirements.lock",
        "pyproject.toml",
        "uv.lock",
    }:
        return FileKind.METADATA, None
    if relative.startswith("assets/") or path.suffix.lower() in _BINARY_SUFFIXES:
        return FileKind.ASSET, None
    return FileKind.UNKNOWN, "path did not match a registered package role"


def _binary(path: Path, data: bytes) -> bool:
    if path.suffix.lower() in _BINARY_SUFFIXES:
        return True
    return b"\x00" in data[:4096]


def _read_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.suffix == ".json"
            else yaml.safe_load(path.read_text(encoding="utf-8"))
        )
    except (json.JSONDecodeError, UnicodeDecodeError, yaml.YAMLError) as error:
        return {"__parse_error__": type(error).__name__}
    return value if isinstance(value, dict) else {}


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(sorted({str(item) for item in value if str(item).strip()}))


def _skill_frontmatter(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    for index in range(1, len(lines)):
        if lines[index].strip() != "---":
            continue
        try:
            value = yaml.safe_load("\n".join(lines[1:index])) or {}
        except yaml.YAMLError:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def load_package(package_root: Path) -> PackageSnapshot:
    root = package_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("package root must be a directory")
    skill_md = root / "SKILL.md"
    if not skill_md.is_file():
        raise ValueError("package has no SKILL.md")

    files: list[PackageFile] = []
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in sorted(directory_names):
            child = current_path / name
            if child.is_symlink() and not child.resolve().is_relative_to(root):
                raise ValueError(f"symlink escapes package root: {child.relative_to(root)}")
        for name in sorted(file_names):
            path = current_path / name
            relative = path.relative_to(root)
            if path.is_symlink():
                target = path.resolve(strict=True)
                if not target.is_relative_to(root):
                    raise ValueError(f"symlink escapes package root: {relative}")
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise ValueError(f"file escapes package root: {relative}")
            data = resolved.read_bytes()
            kind, reason = _kind(relative)
            is_binary = _binary(relative, data)
            files.append(
                PackageFile(
                    path=relative.as_posix(),
                    kind=kind,
                    sha256=content_hash(data),
                    size_bytes=len(data),
                    binary=is_binary,
                    mutable=not is_binary,
                    reason=reason,
                )
            )

    capability = _read_mapping(root / "capability-manifest.json")
    agent_configs: dict[str, Any] = {}
    agents = root / "agents"
    if agents.is_dir():
        for path in sorted(agents.glob("*.y*ml")):
            agent_configs[path.relative_to(root).as_posix()] = _read_mapping(path)
    frontmatter = _skill_frontmatter(skill_md)
    skill_id = str(capability.get("skill_id") or frontmatter.get("name") or root.name)
    hosts = capability.get("required_hosts", [])
    if not isinstance(hosts, list):
        hosts = []
    for config in agent_configs.values():
        if isinstance(config, dict):
            host = config.get("host") or config.get("runtime")
            if host:
                hosts.append(host)
    facts = CapabilityFacts(
        skill_id=skill_id,
        required_hosts=_strings(hosts),
        required_tools=_strings(capability.get("required_tools")),
        required_services=_strings(capability.get("required_services")),
        required_secrets=_strings(capability.get("required_secrets")),
        side_effects=_strings(capability.get("side_effects")),
        agent_config=agent_configs,
    )
    inventory = [
        {
            "path": item.path,
            "sha256": item.sha256,
            "kind": item.kind.value,
            "binary": item.binary,
        }
        for item in sorted(files, key=lambda item: item.path)
    ]
    snapshot_hash = hashlib.sha256(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return PackageSnapshot(
        package_id=skill_id,
        root_name=root.name,
        snapshot_hash=snapshot_hash,
        files=tuple(sorted(files, key=lambda item: item.path)),
        capabilities=facts,
    )
