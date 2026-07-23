import json
from pathlib import Path

from gepase.store.artifacts import ArtifactStore


def test_artifact_round_trip_and_tamper_detection(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    store.write_json("evidence/payload.json", {"ok": True})
    assert store.verify().valid
    (tmp_path / "evidence/payload.json").write_text("tampered", encoding="utf-8")
    result = store.verify()
    assert not result.valid
    assert result.hash_mismatch == 1


def test_event_log_is_append_only(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    store.append_event("events.jsonl", {"n": 1})
    store.append_event("events.jsonl", {"n": 2})
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["n"] for line in lines] == [1, 2]


def test_existing_external_file_can_be_indexed_without_rewrite(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    external = tmp_path / "traces/agent.json"
    external.parent.mkdir(parents=True)
    external.write_text('{"observed": true}\n', encoding="utf-8")
    before = external.stat()

    reference = store.index_existing("traces/agent.json", "application/json")

    after = external.stat()
    verification = store.verify()
    assert reference.size_bytes == external.stat().st_size
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert verification.valid and verification.unindexed_files == 0


def test_prune_missing_removes_only_stale_index_entries(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    store.write_text("keep.txt", "keep")
    store.write_text("remove.txt", "remove")
    (tmp_path / "remove.txt").unlink()
    assert store.prune_missing() == 1
    assert store.verify().valid
    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "keep"
