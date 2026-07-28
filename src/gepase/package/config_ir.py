"""Deterministic key-path projection for auditable configuration files."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from gepase.package.ir import EdgeKind, IRNode, NodeKind, ParseStatus, make_node
from gepase.package.parsing import ParsedFile, RelationFact

_PATH_VALUE = re.compile(r"^(?!https?://)(?:\.?\.?/)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+$")


def _load(path: Path) -> Any:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        return json.loads(text)
    if suffix == ".toml":
        return tomllib.loads(text)
    return yaml.safe_load(text)


def _rows(value: Any, prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    rows: list[tuple[tuple[str, ...], Any]] = []
    if isinstance(value, Mapping):
        for key in sorted(value, key=str):
            current = (*prefix, str(key))
            rows.append((current, value[key]))
            rows.extend(_rows(value[key], current))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            current = (*prefix, str(index))
            rows.append((current, item))
            rows.extend(_rows(item, current))
    return rows


def parse_config(
    package_id: str,
    package_root: Path,
    relative_path: str,
    file_node: IRNode,
) -> ParsedFile:
    path = package_root / relative_path
    try:
        value = _load(path)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        tomllib.TOMLDecodeError,
        yaml.YAMLError,
    ) as error:
        return ParsedFile(
            (),
            (),
            status=ParseStatus.ERROR,
            parser="config",
            detail=f"{type(error).__name__}: {error}",
        )
    nodes: list[IRNode] = []
    relations: list[RelationFact] = []
    for key_path, item in _rows(value):
        dotted = ".".join(key_path)
        locator = f"config/{dotted}"
        rendered = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        node = make_node(
            package_id,
            NodeKind.CONFIG_KEY,
            relative_path,
            locator,
            dotted,
            rendered,
            metadata={"key_path": list(key_path), "value_type": type(item).__name__},
        )
        nodes.append(node)
        parent_locator = f"config/{'.'.join(key_path[:-1])}" if len(key_path) > 1 else None
        relations.append(
            RelationFact(
                file_node.node_id,
                EdgeKind.CONTAINS,
                target_locator=locator,
            )
            if parent_locator is None
            else RelationFact(
                "",
                EdgeKind.CONTAINS,
                target_locator=locator,
                metadata={"parent_locator": parent_locator},
            )
        )
        if isinstance(item, str) and _PATH_VALUE.match(item.strip()):
            clean = item.strip().removeprefix("./")
            resolved = (package_root / clean).resolve()
            if resolved.is_relative_to(package_root.resolve()) and resolved.is_file():
                relations.append(
                    RelationFact(node.node_id, EdgeKind.REFERENCES, target_path=clean)
                )
    # Replace placeholder parent sources after all stable node ids are known.
    by_locator = {node.locator: node.node_id for node in nodes}
    fixed = tuple(
        RelationFact(
            source=(
                by_locator[str(row.metadata["parent_locator"])]
                if not row.source
                else row.source
            ),
            kind=row.kind,
            target_path=row.target_path,
            target_locator=row.target_locator,
            external_name=row.external_name,
            reason=row.reason,
            metadata={key: val for key, val in row.metadata.items() if key != "parent_locator"},
        )
        for row in relations
    )
    return ParsedFile(tuple(nodes), fixed, parser="config")
