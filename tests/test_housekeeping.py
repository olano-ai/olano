"""Tests for olano/runtime/housekeeping.py"""

from __future__ import annotations

import datetime
import gzip
import time
from pathlib import Path

from olano.runtime.housekeeping import (
    HousekeepingConfig,
    run_housekeeping,
)


def _old_mtime(days: float = 0, hours: float = 0) -> float:
    """Return an mtime that is `days` days + `hours` hours in the past."""
    seconds = days * 86400 + hours * 3600
    return time.time() - seconds


def _set_mtime(path: Path, mtime: float) -> None:
    import os

    os.utime(path, (mtime, mtime))


# ─────────────────────────────────────────────────────────────────
# Rule 1: empty sessions deleted after TTL
# ─────────────────────────────────────────────────────────────────


def test_empty_session_deleted_after_ttl(tmp_path: Path):
    session_dir = tmp_path / "sessions" / "myagent"
    session_dir.mkdir(parents=True)
    stale = session_dir / "old.jsonl"
    stale.write_text("")  # empty
    _set_mtime(stale, _old_mtime(hours=25))

    cfg = HousekeepingConfig(empty_session_ttl_hours=24)
    report = run_housekeeping(tmp_path, config=cfg)

    assert not stale.exists()
    assert report.empty_sessions_deleted == 1


def test_empty_session_kept_if_too_young(tmp_path: Path):
    session_dir = tmp_path / "sessions" / "myagent"
    session_dir.mkdir(parents=True)
    fresh = session_dir / "new.jsonl"
    fresh.write_text("")
    _set_mtime(fresh, _old_mtime(hours=1))

    cfg = HousekeepingConfig(empty_session_ttl_hours=24)
    report = run_housekeeping(tmp_path, config=cfg)

    assert fresh.exists()
    assert report.empty_sessions_deleted == 0


# ─────────────────────────────────────────────────────────────────
# Rule 2: non-empty old sessions compressed
# ─────────────────────────────────────────────────────────────────


def test_nonempty_old_session_compressed(tmp_path: Path):
    session_dir = tmp_path / "sessions" / "myagent"
    session_dir.mkdir(parents=True)
    old = session_dir / "old.jsonl"
    old.write_text('{"role": "user", "content": "hello"}')
    _set_mtime(old, _old_mtime(days=200))

    cfg = HousekeepingConfig(session_compress_days=180)
    report = run_housekeeping(tmp_path, config=cfg)

    assert not old.exists()
    gz = session_dir / "old.jsonl.gz"
    assert gz.exists()
    with gzip.open(gz, "rb") as f:
        content = f.read().decode()
    assert "hello" in content
    assert report.sessions_compressed == 1


def test_nonempty_recent_session_not_compressed(tmp_path: Path):
    session_dir = tmp_path / "sessions" / "myagent"
    session_dir.mkdir(parents=True)
    recent = session_dir / "recent.jsonl"
    recent.write_text('{"role": "user", "content": "hi"}')
    _set_mtime(recent, _old_mtime(days=10))

    cfg = HousekeepingConfig(session_compress_days=180)
    report = run_housekeeping(tmp_path, config=cfg)

    assert recent.exists()
    assert report.sessions_compressed == 0


# ─────────────────────────────────────────────────────────────────
# Rule 3: memories archived after 365 days
# ─────────────────────────────────────────────────────────────────


def test_old_memory_file_archived(tmp_path: Path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(parents=True)

    today = datetime.date.today()
    old_date = today - datetime.timedelta(days=366)
    old_md = memories_dir / f"{old_date.isoformat()}.md"
    old_md.write_text("# Old memory")

    cfg = HousekeepingConfig(memory_archive_days=365)
    report = run_housekeeping(tmp_path, config=cfg)

    assert not old_md.exists()
    year = old_date.year
    archived = memories_dir / "archive" / str(year) / old_md.name
    assert archived.exists()
    assert report.memories_archived == 1


def test_recent_memory_file_not_archived(tmp_path: Path):
    memories_dir = tmp_path / "memories"
    memories_dir.mkdir(parents=True)

    today = datetime.date.today()
    recent_date = today - datetime.timedelta(days=364)
    recent_md = memories_dir / f"{recent_date.isoformat()}.md"
    recent_md.write_text("# Recent memory")

    cfg = HousekeepingConfig(memory_archive_days=365)
    report = run_housekeeping(tmp_path, config=cfg)

    assert recent_md.exists()
    assert report.memories_archived == 0


# ─────────────────────────────────────────────────────────────────
# Rule 4: degraded log rotated when oversized
# ─────────────────────────────────────────────────────────────────


def test_degraded_log_rotated_when_oversized(tmp_path: Path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    degraded = logs_dir / "degraded.log"
    # Write 6 MB of data
    degraded.write_bytes(b"x" * (6 * 1024 * 1024))

    cfg = HousekeepingConfig(degraded_log_max_mb=5.0)
    report = run_housekeeping(tmp_path, config=cfg)

    assert report.degraded_log_rotated is True
    assert degraded.exists()  # new empty file created
    assert degraded.stat().st_size == 0
    rotated_files = [
        f for f in logs_dir.iterdir() if f.name.startswith("degraded.") and f.name != "degraded.log"
    ]
    assert len(rotated_files) == 1


def test_degraded_log_not_rotated_when_small(tmp_path: Path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    degraded = logs_dir / "degraded.log"
    degraded.write_bytes(b"x" * 1024)  # 1 KB

    cfg = HousekeepingConfig(degraded_log_max_mb=5.0)
    report = run_housekeeping(tmp_path, config=cfg)

    assert report.degraded_log_rotated is False


# ─────────────────────────────────────────────────────────────────
# Disabled config
# ─────────────────────────────────────────────────────────────────


def test_disabled_config_skips_all(tmp_path: Path):
    session_dir = tmp_path / "sessions" / "myagent"
    session_dir.mkdir(parents=True)
    stale = session_dir / "old.jsonl"
    stale.write_text("")
    _set_mtime(stale, _old_mtime(hours=200))

    cfg = HousekeepingConfig(enabled=False)
    report = run_housekeeping(tmp_path, config=cfg)

    assert stale.exists()
    assert report.empty_sessions_deleted == 0
    assert report.actions_taken is False


# ─────────────────────────────────────────────────────────────────
# actions_taken and __str__
# ─────────────────────────────────────────────────────────────────


def test_actions_taken_false_when_nothing_done(tmp_path: Path):
    report = run_housekeeping(tmp_path, config=HousekeepingConfig())
    assert report.actions_taken is False


def test_report_str_includes_counts(tmp_path: Path):
    session_dir = tmp_path / "sessions" / "myagent"
    session_dir.mkdir(parents=True)
    stale = session_dir / "old.jsonl"
    stale.write_text("")
    _set_mtime(stale, _old_mtime(hours=25))

    cfg = HousekeepingConfig(empty_session_ttl_hours=24)
    report = run_housekeeping(tmp_path, config=cfg)

    assert report.actions_taken is True
    text = str(report)
    assert "1 empty sessions removed" in text
