from pathlib import Path

from gepase.package.analyzer import PackageAnalyzer


def test_invalid_yaml_frontmatter_is_a_diagnostic_not_a_parser_crash(
    tmp_path: Path,
) -> None:
    package = tmp_path / "invalid-frontmatter"
    package.mkdir()
    (package / "SKILL.md").write_text(
        "---\n"
        "name: invalid-frontmatter\n"
        "description: invalid scalar: colon\n"
        "---\n\n"
        "# Contract\n\n"
        "Use the package.\n",
        encoding="utf-8",
    )
    analysis = PackageAnalyzer().analyze(package)
    assert any(
        item.kind == "invalid_frontmatter" and item.severity == "error"
        for item in analysis.graph.diagnostics
    )
