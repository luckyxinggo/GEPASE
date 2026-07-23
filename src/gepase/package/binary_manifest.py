"""Immutable binary inventory with safe JAR/ZIP manifest inspection."""

from __future__ import annotations

import zipfile
from pathlib import Path

from gepase.package.ir import EdgeKind, IRNode, NodeKind, make_node
from gepase.package.parsing import ParsedFile, RelationFact


def parse_binary(
    package_id: str,
    package_root: Path,
    relative_path: str,
    file_node: IRNode,
) -> ParsedFile:
    path = package_root / relative_path
    data = path.read_bytes()
    metadata: dict[str, object] = {"immutable": True, "archive_entries": 0}
    if path.suffix.lower() in {".jar", ".zip"}:
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                metadata["archive_entries"] = len(names)
                manifest = next(
                    (name for name in names if name.upper() == "META-INF/MANIFEST.MF"),
                    None,
                )
                if manifest:
                    metadata["manifest"] = archive.read(manifest).decode(
                        "utf-8", errors="replace"
                    )
        except zipfile.BadZipFile as error:
            metadata["archive_error"] = type(error).__name__
    node = make_node(
        package_id,
        NodeKind.BINARY,
        relative_path,
        "binary-manifest",
        relative_path,
        data,
        mutable=False,
        metadata=metadata,
    )
    return ParsedFile(
        (node,),
        (RelationFact(file_node.node_id, EdgeKind.CONTAINS, target_locator=node.locator),),
    )
