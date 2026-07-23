from pathlib import Path

import yaml


def test_orchestrator_is_thin_and_has_valid_skill_metadata() -> None:
    root = Path(".agents/skills/gepase-orchestrator")
    text = (root / "SKILL.md").read_text(encoding="utf-8")
    _, frontmatter, body = text.split("---", 2)
    metadata = yaml.safe_load(frontmatter)
    assert metadata["name"] == "gepase-orchestrator"
    assert set(metadata) == {"name", "description"}
    assert "gepase eval export-work" not in body or "export" in body
    assert "Do not write candidate pools" in body
    files = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    assert not any(
        token in path
        for path in files
        for token in ("candidate", "frontier", "optimizer-state", "ledger.sqlite")
    )


def test_orchestrator_requires_isolated_blind_pairs() -> None:
    text = Path(".agents/skills/gepase-orchestrator/SKILL.md").read_text(encoding="utf-8")
    assert "isolated" in text
    assert "assertions, expected values, sibling output" in text
    assert "E1" in text and "E2" in text
