"""Markdown AST projection with semantic, line-independent node locators."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from markdown_it import MarkdownIt

from gepase.package.ir import EdgeKind, IRNode, NodeKind, SourceSpan, make_node
from gepase.package.parsing import ParsedFile, RelationFact

_PATH_RE = re.compile(
    r"(?<![\w.-])((?:references|scripts|core|assets|agents|tests)/[A-Za-z0-9_./-]+)"
)
_PYTHON_COMMAND_RE = re.compile(r"(?:python(?:3)?\s+)([A-Za-z0-9_./-]+\.py)")
_PYTHON_IMPORT_RE = re.compile(
    r"(?:from\s+|import\s+)((?:core|scripts)(?:\.[A-Za-z_][A-Za-z0-9_]*)+)"
)
_SECRET_RE = re.compile(r"\b([A-Z][A-Z0-9_]{3,}(?:KEY|TOKEN|SECRET|PASSWORD))\b")


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", value.casefold()).strip("-")
    return normalized or "section"


def _frontmatter(text: str) -> tuple[dict[str, Any], int, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, 0, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            try:
                value = yaml.safe_load("\n".join(lines[1:index])) or {}
            except yaml.YAMLError as error:
                value = {"__parse_error__": type(error).__name__}
            return (
                (value if isinstance(value, dict) else {}),
                index + 1,
                "\n".join(lines[index + 1 :]),
            )
    return {}, 0, text


def _resolved_relative(source_path: str, target: str) -> tuple[str | None, str | None]:
    clean = target.split("#", 1)[0].strip()
    if not clean or "://" in clean or clean.startswith(("mailto:", "#")):
        return None, clean or target
    root_anchored = clean.startswith(
        ("references/", "scripts/", "core/", "assets/", "agents/", "tests/")
    )
    pure = PurePosixPath(clean) if root_anchored else PurePosixPath(source_path).parent / clean
    parts: list[str] = []
    unsafe = False
    for part in pure.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if parts:
                parts.pop()
            else:
                unsafe = True
        else:
            parts.append(part)
    return (None, f"unsafe path: {target}") if unsafe else ("/".join(parts), None)


def parse_markdown(
    package_id: str,
    package_root: Path,
    relative_path: str,
    file_node: IRNode,
) -> ParsedFile:
    text = (package_root / relative_path).read_text(encoding="utf-8")
    frontmatter, offset, body = _frontmatter(text)
    parser = MarkdownIt("commonmark", {"html": True})
    tokens = parser.parse(body)
    nodes: list[IRNode] = []
    relations: list[RelationFact] = []
    if frontmatter:
        parse_error = frontmatter.get("__parse_error__")
        node = make_node(
            package_id,
            NodeKind.FRONTMATTER,
            relative_path,
            "frontmatter",
            "frontmatter",
            yaml.safe_dump(frontmatter, sort_keys=True),
            span=SourceSpan(start_line=1, end_line=max(1, offset)),
            metadata={
                "keys": sorted(str(key) for key in frontmatter),
                "parse_error": parse_error,
            },
        )
        nodes.append(node)
        relations.append(
            RelationFact(file_node.node_id, EdgeKind.CONTAINS, target_locator=node.locator)
        )
        allowed = frontmatter.get("allowed-tools") or frontmatter.get("allowed_tools") or []
        if isinstance(allowed, str):
            allowed = re.split(r"[,\s]+", allowed)
        if isinstance(allowed, list):
            for tool in allowed:
                relations.append(
                    RelationFact(node.node_id, EdgeKind.USES_TOOL, external_name=str(tool))
                )

    heading_stack: list[tuple[int, IRNode]] = []
    heading_counts: defaultdict[str, int] = defaultdict(int)
    block_counts: defaultdict[str, int] = defaultdict(int)
    current_container = file_node
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type == "heading_open" and index + 1 < len(tokens):
            inline = tokens[index + 1]
            title = inline.content.strip()
            level = int(token.tag[1:])
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            parent_path = "/".join(item[1].locator.rsplit("/", 1)[-1] for item in heading_stack)
            base = f"{parent_path}/{_slug(title)}".strip("/")
            heading_counts[base] += 1
            locator = f"section/{base}#{heading_counts[base]}"
            span = token.map or [0, 1]
            node = make_node(
                package_id,
                NodeKind.SECTION,
                relative_path,
                locator,
                title,
                title,
                span=SourceSpan(start_line=span[0] + offset + 1, end_line=span[1] + offset),
                metadata={"heading_level": level, "semantic_path": base},
            )
            nodes.append(node)
            parent = heading_stack[-1][1] if heading_stack else file_node
            relations.append(
                RelationFact(parent.node_id, EdgeKind.CONTAINS, target_locator=locator)
            )
            heading_stack.append((level, node))
            current_container = node
            index += 2
            continue
        if token.type in {"paragraph_open", "fence", "code_block"}:
            span = token.map or [0, 1]
            content = token.content.strip()
            scan = index
            if token.type == "paragraph_open":
                scan = index + 1
                contents: list[str] = []
                while scan < len(tokens) and tokens[scan].type not in {
                    "paragraph_close",
                    "list_item_close",
                }:
                    if tokens[scan].type == "inline":
                        contents.append(tokens[scan].content)
                    scan += 1
                content = " ".join(contents).strip()
            if content:
                container_key = current_container.locator
                block_counts[container_key] += 1
                kind = (
                    NodeKind.INSTRUCTION
                    if relative_path == "SKILL.md"
                    else NodeKind.REFERENCE_CHUNK
                )
                locator = f"{container_key}/block#{block_counts[container_key]}"
                node = make_node(
                    package_id,
                    kind,
                    relative_path,
                    locator,
                    content[:120],
                    content,
                    span=SourceSpan(
                        start_line=span[0] + offset + 1,
                        end_line=max(span[0] + offset + 1, span[1] + offset),
                    ),
                    metadata={"block_type": token.type},
                )
                nodes.append(node)
                relations.append(
                    RelationFact(
                        current_container.node_id,
                        EdgeKind.CONTAINS,
                        target_locator=locator,
                    )
                )
                _extract_relations(relative_path, node, content, relations)
                if token.type == "paragraph_open":
                    for child in tokens[index + 1 : scan + 1]:
                        if child.type != "inline" or not child.children:
                            continue
                        for inline_child in child.children:
                            if inline_child.type == "link_open":
                                href = inline_child.attrGet("href")
                                if isinstance(href, str) and href:
                                    target_path, external = _resolved_relative(relative_path, href)
                                    relations.append(
                                        RelationFact(
                                            node.node_id,
                                            EdgeKind.REFERENCES,
                                            target_path=target_path,
                                            external_name=external,
                                            reason=(
                                                external
                                                if external and external.startswith("unsafe path:")
                                                else "external link"
                                                if external
                                                else None
                                            ),
                                        )
                                    )
        index += 1
    return ParsedFile(tuple(nodes), tuple(relations))


def _extract_relations(
    source_path: str,
    node: IRNode,
    content: str,
    relations: list[RelationFact],
) -> None:
    seen: set[tuple[EdgeKind, str]] = set()
    for match in _PATH_RE.finditer(content):
        target_path, external = _resolved_relative(source_path, match.group(1))
        key = (EdgeKind.REFERENCES, target_path or str(external))
        if key not in seen:
            relations.append(
                RelationFact(
                    node.node_id,
                    EdgeKind.REFERENCES,
                    target_path=target_path,
                    external_name=external,
                )
            )
            seen.add(key)
    for match in _PYTHON_COMMAND_RE.finditer(content):
        target_path, external = _resolved_relative(source_path, match.group(1))
        key = (EdgeKind.EXECUTES, target_path or str(external))
        if key not in seen:
            relations.append(
                RelationFact(
                    node.node_id,
                    EdgeKind.EXECUTES,
                    target_path=target_path,
                    external_name=external,
                )
            )
            seen.add(key)
    for match in _PYTHON_IMPORT_RE.finditer(content):
        target_path = match.group(1).replace(".", "/") + ".py"
        key = (EdgeKind.REFERENCES, target_path)
        if key not in seen:
            relations.append(
                RelationFact(node.node_id, EdgeKind.REFERENCES, target_path=target_path)
            )
            seen.add(key)
    for match in _SECRET_RE.finditer(content):
        relations.append(
            RelationFact(node.node_id, EdgeKind.REQUIRES_SECRET, external_name=match.group(1))
        )
