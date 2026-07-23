from __future__ import annotations

from gepase.optimizer.merge.closure import dependency_closure
from gepase.package.ir import (
    EdgeKind,
    NodeKind,
    PackageGraph,
    make_edge,
    make_node,
)


def test_dependency_closure_keeps_container_reference_and_test() -> None:
    package = make_node("p", NodeKind.PACKAGE, ".", "package", "p", "p")
    file = make_node("p", NodeKind.FILE, "SKILL.md", "file", "skill", "f")
    section = make_node("p", NodeKind.SECTION, "SKILL.md", "section", "workflow", "s")
    reference = make_node(
        "p", NodeKind.REFERENCE_CHUNK, "references/a.md", "chunk", "a", "r"
    )
    test = make_node("p", NodeKind.FILE, "checks/test.md", "test", "test", "t")
    sibling = make_node("p", NodeKind.FILE, "references/unrelated.md", "file", "other", "u")
    graph = PackageGraph(
        package_id="p",
        snapshot_hash="0" * 64,
        nodes=(package, file, section, reference, test, sibling),
        edges=(
            make_edge(package.node_id, file.node_id, EdgeKind.CONTAINS),
            make_edge(package.node_id, sibling.node_id, EdgeKind.CONTAINS),
            make_edge(file.node_id, section.node_id, EdgeKind.CONTAINS),
            make_edge(section.node_id, reference.node_id, EdgeKind.REFERENCES),
            make_edge(test.node_id, section.node_id, EdgeKind.TESTS),
        ),
    )
    closed = set(dependency_closure(graph, {section.node_id}))
    assert closed == {
        package.node_id,
        file.node_id,
        section.node_id,
        reference.node_id,
        test.node_id,
    }
    assert sibling.node_id not in closed
