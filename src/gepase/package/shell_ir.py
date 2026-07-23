"""Conservative shell command, path, environment, and entrypoint extraction."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from gepase.package.ir import EdgeKind, IRNode, NodeKind, SourceSpan, make_node
from gepase.package.parsing import ParsedFile, RelationFact

_ENV_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
_PATH_RE = re.compile(r"(?:^|\s)(\.?\.?/[A-Za-z0-9_./-]+|[A-Za-z0-9_-]+/[A-Za-z0-9_./-]+)")


def parse_shell(
    package_id: str,
    package_root: Path,
    relative_path: str,
    file_node: IRNode,
) -> ParsedFile:
    text = (package_root / relative_path).read_text(encoding="utf-8")
    nodes: list[IRNode] = []
    relations: list[RelationFact] = []
    occurrence: dict[str, int] = {}
    for line_number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            words = shlex.split(stripped, comments=True)
        except ValueError as error:
            locator = f"error/shell-parse/{line_number}"
            node = make_node(
                package_id,
                NodeKind.ERROR,
                relative_path,
                locator,
                "ShellParseError",
                str(error),
                span=SourceSpan(start_line=line_number, end_line=line_number),
                metadata={"error_type": "shell_parse_error", "message": str(error)},
            )
            nodes.append(node)
            relations.append(
                RelationFact(file_node.node_id, EdgeKind.CONTAINS, target_locator=locator)
            )
            continue
        if not words:
            continue
        command = words[0]
        occurrence[command] = occurrence.get(command, 0) + 1
        locator = f"shell-command/{command}#{occurrence[command]}"
        node = make_node(
            package_id,
            NodeKind.SHELL_COMMAND,
            relative_path,
            locator,
            command,
            stripped,
            span=SourceSpan(start_line=line_number, end_line=line_number),
            metadata={"arguments": words[1:]},
        )
        nodes.append(node)
        relations.extend(
            (
                RelationFact(file_node.node_id, EdgeKind.CONTAINS, target_locator=locator),
                RelationFact(node.node_id, EdgeKind.CALLS, external_name=command),
            )
        )
        for name in _ENV_RE.findall(stripped):
            kind = (
                EdgeKind.REQUIRES_SECRET
                if any(marker in name.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
                else EdgeKind.READS
            )
            relations.append(RelationFact(node.node_id, kind, external_name=f"env:{name}"))
        for match in _PATH_RE.finditer(stripped):
            candidate = match.group(1).lstrip("./")
            relations.append(RelationFact(node.node_id, EdgeKind.REFERENCES, target_path=candidate))
    return ParsedFile(tuple(nodes), tuple(relations))
