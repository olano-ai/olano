"""Directory-size scan: bucket aggregation, depth caps, snapshot round-trip.

The walk is driven directly (never via the monitor loop); the only real
filesystem used is pytest's tmp_path. The truncation test is the single one
that consumes wall-clock (~1s) — everything else is instant.
"""

from __future__ import annotations

import json
import os
import time

from olano.runtime import disk_scan


def _write(path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def make_home(tmp_path):
    """A miniature ~/.olano: root files, two-level dirs, an agents subtree.

    Uses a fresh subdirectory — conftest plants a ``olano-home`` inside
    ``tmp_path`` itself, which would pollute the buckets.
    """
    home = tmp_path / "scan-root"
    _write(home / "config.yaml", 10)
    _write(home / "sessions" / "merli" / "t1.jsonl", 100)
    _write(home / "sessions" / "merli" / "t2.jsonl", 50)
    _write(home / "workspace" / "projects" / "app" / "big.bin", 4000)
    _write(home / "agents" / "merli" / "workspace" / "data" / "deep" / "blob.bin", 700)
    _write(home / "agents" / "merli" / "agent.json5", 25)
    return home


def by_path(result):
    return {e["path"]: e for e in result.entries}


def test_buckets_roll_up_to_the_depth_cap(tmp_path):
    result = disk_scan.scan_directory_sizes(make_home(tmp_path), throttle_ms=0)
    b = by_path(result)

    # depth 1 + 2 below the root; deeper levels fold into their ancestors.
    assert b["sessions"]["bytes"] == 150 and b["sessions"]["files"] == 2
    assert b["sessions/merli"]["bytes"] == 150
    assert b["workspace"]["bytes"] == 4000
    assert b["workspace/projects"]["bytes"] == 4000
    assert "workspace/projects/app" not in b  # beyond depth 2

    # agents/<name> gets agent_depth extra levels.
    assert b["agents"]["bytes"] == 725
    assert b["agents/merli"]["bytes"] == 725
    assert b["agents/merli/workspace"]["bytes"] == 700
    assert b["agents/merli/workspace/data"]["bytes"] == 700
    assert "agents/merli/workspace/data/deep" not in b  # beyond 2 + agent_depth

    # Root files count in totals but form no bucket.
    assert result.total_bytes == 10 + 150 + 4000 + 725
    assert result.total_files == 6
    assert result.truncated is False and result.entries_capped is False
    # Largest first — the ordering the dashboard renders verbatim.
    assert result.entries[0]["path"] in ("workspace", "workspace/projects")


def test_symlinks_are_not_followed_and_hardlinks_count_once(tmp_path):
    home = make_home(tmp_path)
    (home / "loop").symlink_to(home)  # would recurse forever if followed
    os.link(home / "workspace" / "projects" / "app" / "big.bin", home / "sessions" / "hard.bin")
    result = disk_scan.scan_directory_sizes(home, throttle_ms=0)
    # 6 regular files + 1 symlink entry; the second hardlink name is deduped
    # (which parent gets the bytes depends on scan order — only totals are
    # order-independent, so that's what we assert).
    assert result.total_files == 7
    regular = 10 + 150 + 4000 + 725
    assert regular <= result.total_bytes <= regular + 200  # + the symlink's own lstat size
    assert result.total_bytes < regular + 4000  # the 4000-byte file never counts twice


def test_time_budget_truncates_instead_of_hanging(tmp_path):
    home = make_home(tmp_path)
    result = disk_scan.scan_directory_sizes(
        home,
        time_budget_seconds=1,
        throttle_every=1,
        throttle_ms=600,
        sleep_fn=time.sleep,
    )
    assert result.truncated is True
    assert result.duration_seconds < 5


def test_max_entries_keeps_the_largest_buckets(tmp_path):
    home = tmp_path / "scan-root"
    for i in range(6):
        _write(home / f"dir{i}" / "f.bin", (i + 1) * 10)
    result = disk_scan.scan_directory_sizes(home, throttle_ms=0, max_entries=3)
    assert result.entries_capped is True
    assert [e["path"] for e in result.entries] == ["dir5", "dir4", "dir3"]


def test_snapshot_round_trip_and_lookup(tmp_path):
    home = make_home(tmp_path)
    result = disk_scan.scan_directory_sizes(home, throttle_ms=0)
    disk_scan.save_snapshot(home, result)

    snap = disk_scan.load_snapshot(home)
    assert snap is not None and snap["total_bytes"] == result.total_bytes

    hit = disk_scan.dir_size_lookup(home, home / "workspace")
    assert hit is not None and hit["bytes"] == 4000
    assert disk_scan.dir_size_lookup(home, home / "workspace" / "projects" / "app") is None
    assert disk_scan.dir_size_lookup(home, tmp_path.parent) is None  # outside the tree


def test_snapshot_rejects_wrong_version_and_garbage(tmp_path):
    path = disk_scan.snapshot_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert disk_scan.load_snapshot(tmp_path) is None
    path.write_text(json.dumps({"version": 999, "entries": []}), encoding="utf-8")
    assert disk_scan.load_snapshot(tmp_path) is None


def test_cached_snapshot_rereads_only_on_mtime_change(tmp_path):
    home = make_home(tmp_path)
    disk_scan.save_snapshot(home, disk_scan.scan_directory_sizes(home, throttle_ms=0))
    snap1, index1 = disk_scan.cached_snapshot(home)
    snap2, index2 = disk_scan.cached_snapshot(home)
    assert snap1 is snap2 and index1 is index2  # served from the memo

    _write(home / "sessions" / "merli" / "t3.jsonl", 999)
    result = disk_scan.scan_directory_sizes(home, throttle_ms=0)
    disk_scan.save_snapshot(home, result)
    os.utime(disk_scan.snapshot_path(home))  # ensure the mtime moves
    snap3, _ = disk_scan.cached_snapshot(home)
    assert snap3 is not None and snap3["total_bytes"] == result.total_bytes


def test_spawn_scan_runs_once_and_persists(tmp_path):
    home = make_home(tmp_path)
    done: list[dict] = []
    started = disk_scan.spawn_scan(
        home,
        settings=type("S", (), {"depth": 2, "agent_depth": 2})(),
        on_done=done.append,
    )
    assert started is True
    for _ in range(100):
        if done:
            break
        time.sleep(0.05)
    assert done and done[0]["total_bytes"] > 0
    assert disk_scan.load_snapshot(home) is not None
    assert disk_scan.scan_running() is False
