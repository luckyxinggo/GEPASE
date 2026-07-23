from pathlib import Path

from gepase.skills.inventory import package_hash


def test_package_hash_changes_with_content_not_absolute_location(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root in (first, second):
        (root / "references").mkdir(parents=True)
        (root / "SKILL.md").write_text("---\nname: sample\ndescription: test\n---\n")
        (root / "references/spec.md").write_text("contract")
    assert package_hash(first) == package_hash(second)
    (second / "references/spec.md").write_text("changed")
    assert package_hash(first)[0] != package_hash(second)[0]
