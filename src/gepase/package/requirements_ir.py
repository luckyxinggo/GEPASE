"""Deterministic projection of pip requirement manifests into PackageGraph nodes."""

from __future__ import annotations

import re
from pathlib import Path

from gepase.package.ir import EdgeKind, IRNode, NodeKind, SourceSpan, make_node
from gepase.package.parsing import ParsedFile, RelationFact

_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)")
_IMPORT_ALIASES = {
    "pillow": "PIL",
    "pyyaml": "yaml",
    "scikit-learn": "sklearn",
}


def parse_requirements(
    package_id: str,
    package_root: Path,
    relative_path: str,
    file_node: IRNode,
) -> ParsedFile:
    """Represent declared distributions without executing a package manager."""
    text = (package_root / relative_path).read_text(encoding="utf-8")
    nodes: list[IRNode] = []
    relations: list[RelationFact] = []
    seen: set[str] = set()
    for line_number, raw in enumerate(text.splitlines(), start=1):
        value = raw.split("#", 1)[0].strip()
        if not value or value.startswith(("-r", "--requirement", "--index-url")):
            continue
        match = _NAME_RE.match(value)
        if match is None:
            continue
        distribution = match.group(1)
        normalized = distribution.casefold().replace("_", "-")
        if normalized in seen:
            continue
        seen.add(normalized)
        import_name = _IMPORT_ALIASES.get(normalized, normalized.replace("-", "_"))
        locator = f"dependency/{normalized}"
        node = make_node(
            package_id,
            NodeKind.DEPENDENCY,
            relative_path,
            locator,
            distribution,
            value,
            span=SourceSpan(start_line=line_number, end_line=line_number),
            mutable=False,
            metadata={
                "distribution": normalized,
                "import_names": [import_name],
                "requirement": value,
            },
        )
        nodes.append(node)
        relations.append(
            RelationFact(file_node.node_id, EdgeKind.CONTAINS, target_locator=locator)
        )
        relations.append(
            RelationFact(node.node_id, EdgeKind.IMPORTS, external_name=import_name)
        )
    return ParsedFile(tuple(nodes), tuple(relations))
