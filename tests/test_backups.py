"""Tests for the cognition backup/undo subsystem (Phase 5.6)."""

from __future__ import annotations

from pathlib import Path

from olano.cognition.backups import restore, snapshot_backup


def test_snapshot_then_restore_reverts_edit(tmp_path: Path) -> None:
    target = tmp_path / "MEMORY.md"
    target.write_text("original", encoding="utf-8")

    job_id = snapshot_backup(tmp_path, ["MEMORY.md"], "test")
    target.write_text("worker-edited", encoding="utf-8")

    ok, msg = restore(tmp_path, job_id)
    assert ok
    assert target.read_text(encoding="utf-8") == "original"
    assert "Restored 1" in msg


def test_restore_deletes_created_file(tmp_path: Path) -> None:
    # File does not exist at snapshot time.
    job_id = snapshot_backup(tmp_path, ["projects/new.md"], "test")
    created = tmp_path / "projects" / "new.md"
    created.parent.mkdir(parents=True, exist_ok=True)
    created.write_text("autonomously created", encoding="utf-8")

    ok, msg = restore(tmp_path, job_id)
    assert ok
    assert not created.exists()
    assert "removed 1 created" in msg


def test_restore_unknown_job(tmp_path: Path) -> None:
    ok, msg = restore(tmp_path, "nope")
    assert not ok
    assert "No backup" in msg


def test_prune_keeps_recent(tmp_path: Path) -> None:
    (tmp_path / "f.md").write_text("x", encoding="utf-8")
    ids = [snapshot_backup(tmp_path, ["f.md"], "test") for _ in range(25)]
    backups = tmp_path / ".olano" / "state" / "cognition" / "backups"
    remaining = {p.name for p in backups.iterdir() if p.is_dir()}
    assert len(remaining) <= 20
    # The most recent job survived.
    assert ids[-1] in remaining
