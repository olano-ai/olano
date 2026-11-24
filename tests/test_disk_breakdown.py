"""Whole-disk attribution: docker df parsing, swap, same-volume detection,
the residual, and the TTL cache.

No docker and no real filesystem measurement anywhere — ``usage_fn`` /
``docker_df_fn`` / ``swap_fn`` fakes carry everything (same convention as
test_disk_monitor.py).
"""

from __future__ import annotations

import json
import subprocess
from collections import namedtuple
from pathlib import Path

from olano.runtime import disk_breakdown as db

Usage = namedtuple("Usage", "total used free")

GB = 1024**3


def _usage_map(mapping: dict[str, Usage]):
    """usage_fn over a {path: Usage} table; unknown paths raise like statvfs."""

    def usage_fn(path: str) -> Usage:
        try:
            return mapping[str(path)]
        except KeyError as e:  # pragma: no cover - a test asking for the wrong path
            raise OSError(f"no such fake path: {path}") from e

    return usage_fn


def _df(images="5.9GB", containers="80MB", volumes="120MB", cache="0B", reclaim="2.1GB (35%)"):
    return {
        "images": {
            "count": 4,
            "size_bytes": db.parse_docker_size(images),
            "reclaimable_bytes": db.parse_docker_size(reclaim),
        },
        "containers": {
            "count": 4,
            "size_bytes": db.parse_docker_size(containers),
            "reclaimable_bytes": 0,
        },
        "volumes": {
            "count": 3,
            "size_bytes": db.parse_docker_size(volumes),
            "reclaimable_bytes": 0,
        },
        "build_cache": {
            "count": 0,
            "size_bytes": db.parse_docker_size(cache),
            "reclaimable_bytes": 0,
        },
    }


# ── docker size parsing ──────────────────────────────────────────────────────


def test_parse_docker_size_handles_gounits():
    assert db.parse_docker_size("0B") == 0
    assert db.parse_docker_size("0") == 0
    assert db.parse_docker_size("356.8MB") == 356_800_000
    assert db.parse_docker_size("3.16GB") == 3_160_000_000
    assert db.parse_docker_size("3.1GB (73%)") == 3_100_000_000
    assert db.parse_docker_size("1.5kB") == 1500
    assert db.parse_docker_size("2TB") == 2 * 10**12
    assert db.parse_docker_size("N/A") is None
    assert db.parse_docker_size("") is None
    assert db.parse_docker_size("12") is None  # a bare number carries no unit


def test_collect_docker_df_reads_json_lines(monkeypatch):
    monkeypatch.setattr("olano.runtime.disk_monitor.docker_daemon_reachable", lambda: True)
    rows = [
        {
            "Type": "Images",
            "TotalCount": 4,
            "Active": 3,
            "Size": "5.9GB",
            "Reclaimable": "2.1GB (35%)",
        },
        {
            "Type": "Local Volumes",
            "TotalCount": 3,
            "Active": 3,
            "Size": "120MB",
            "Reclaimable": "0B (0%)",
        },
        {"Type": "Something Else", "TotalCount": 1, "Size": "9GB", "Reclaimable": "0B"},
    ]

    def run_fn(argv, **_kw):
        assert argv == ["docker", "system", "df", "--format", "{{json .}}"]
        return subprocess.CompletedProcess(
            argv, 0, stdout="\n".join(json.dumps(r) for r in rows), stderr=""
        )

    out = db.collect_docker_df(run_fn=run_fn, which_fn=lambda _b: "/usr/bin/docker")
    assert out is not None
    assert out["images"]["size_bytes"] == 5_900_000_000
    assert out["images"]["reclaimable_bytes"] == 2_100_000_000
    assert out["volumes"]["count"] == 3
    assert "something_else" not in out  # unknown resource types are ignored


def test_docker_available_reports_why_not():
    ok, reason = db.docker_available(which_fn=lambda _b: None)
    assert not ok and reason is not None and "docker CLI" in reason
    ok, reason = db.docker_available(which_fn=lambda _b: "/usr/bin/docker", docker_fn=lambda: False)
    assert not ok and reason is not None and "socket" in reason
    ok, reason = db.docker_available(which_fn=lambda _b: "/usr/bin/docker", docker_fn=lambda: True)
    assert ok and reason is None


# ── swap ─────────────────────────────────────────────────────────────────────


def test_read_swap_files_skips_partitions_and_header(tmp_path):
    swaps = tmp_path / "swaps"
    swaps.write_text(
        "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
        "/swapfile                               file            2097148 12345           -2\n"
        "/dev/vdb1                               partition       4194300 0               -3\n",
        encoding="utf-8",
    )
    entries = db.read_swap_files(swaps)
    assert entries == [{"path": "/swapfile", "bytes": 2097148 * 1024}]


def test_read_swap_files_missing_file_is_empty(tmp_path):
    assert db.read_swap_files(tmp_path / "nope") == []


# ── same-volume detection (the overlayfs trap) ───────────────────────────────


def test_same_volume_uses_statvfs_not_st_dev(tmp_path):
    """Inside the runtime container ``/`` is an overlayfs and the home is a bind
    mount: different ``st_dev``, one physical filesystem. Totals must decide."""
    usage_fn = _usage_map(
        {
            "/": Usage(25 * GB, 14 * GB, 9 * GB),
            "/home/olano/.lionclaw": Usage(25 * GB, 14 * GB, 9 * GB),
            "/mnt/other": Usage(50 * GB, 1 * GB, 48 * GB),
        }
    )
    assert db._same_volume("/home/olano/.lionclaw", "/", usage_fn)
    assert not db._same_volume("/mnt/other", "/", usage_fn)
    assert not db._same_volume("/nonexistent", "/", usage_fn)


def test_same_volume_tolerates_a_small_free_space_drift():
    usage_fn = _usage_map(
        {"/": Usage(25 * GB, 14 * GB, 9 * GB), "/home": Usage(25 * GB, 14 * GB, 9 * GB - 4096)}
    )
    assert db._same_volume("/home", "/", usage_fn)


# ── the breakdown ────────────────────────────────────────────────────────────


def _build(**kw):
    defaults = dict(
        path="/",
        home=Path("/home/olano/.lionclaw"),
        usage_fn=_usage_map(
            {
                "/": Usage(25 * GB, 14 * GB, 9 * GB),
                "/home/olano/.lionclaw": Usage(25 * GB, 14 * GB, 9 * GB),
            }
        ),
        docker_df_fn=lambda: _df(),
        docker_probe_fn=lambda: (True, None),
        swap_fn=lambda: [{"path": "/swapfile", "bytes": 2 * GB}],
        scan_loader=lambda _home: {"total_bytes": 759 * 1024 * 1024, "truncated": False},
    )
    defaults.update(kw)
    return db.build_breakdown(**defaults)


def test_breakdown_attributes_docker_home_swap_and_a_named_residual():
    report = _build()
    groups = {g.key: g for g in report.groups}

    assert groups["docker_images"].bytes == 5_900_000_000
    assert groups["docker_images"].reclaimable_bytes == 2_100_000_000
    assert "4 present" in groups["docker_images"].detail
    assert groups["olano_home"].bytes == 759 * 1024 * 1024
    assert groups["swap"].bytes == 2 * GB
    assert "/swapfile" in groups["swap"].detail

    # The residual is what is left of the used space — the whole point: a
    # number the operator can read as "the operating system", not a mystery.
    assert groups[db.RESIDUAL_KEY].bytes == report.used_bytes - report.attributed_bytes
    assert sum(g.bytes or 0 for g in report.groups) == report.used_bytes
    assert report.complete is True
    assert report.warnings == []


def test_breakdown_without_docker_marks_groups_unmeasured():
    report = _build(
        docker_probe_fn=lambda: (False, "docker socket not mounted"), docker_df_fn=lambda: None
    )
    groups = {g.key: g for g in report.groups}
    assert groups["docker_images"].bytes is None
    assert groups["docker_images"].note == "docker socket not mounted"
    assert report.complete is False
    assert any("Docker usage could not be measured" in w for w in report.warnings)
    # Everything docker would have held falls into the residual rather than
    # silently vanishing from the total.
    assert groups[db.RESIDUAL_KEY].bytes == report.used_bytes - (759 * 1024 * 1024) - 2 * GB


def test_breakdown_without_a_scan_leaves_the_home_unmeasured():
    report = _build(scan_loader=lambda _home: None)
    home = next(g for g in report.groups if g.key == "olano_home")
    assert home.bytes is None
    assert home.note is not None and "Scan now" in home.note
    assert report.complete is False


def test_breakdown_flags_a_truncated_scan_as_a_floor():
    report = _build(scan_loader=lambda _home: {"total_bytes": 12345, "truncated": True})
    home = next(g for g in report.groups if g.key == "olano_home")
    assert home.bytes == 12345
    assert home.note is not None and "floor" in home.note


def test_breakdown_excludes_a_home_on_another_filesystem():
    usage_fn = _usage_map(
        {
            "/": Usage(25 * GB, 14 * GB, 9 * GB),
            "/mnt/data/.lionclaw": Usage(100 * GB, 1 * GB, 98 * GB),
        }
    )
    report = _build(home=Path("/mnt/data/.lionclaw"), usage_fn=usage_fn)
    home = next(g for g in report.groups if g.key == "olano_home")
    assert home.bytes is None
    assert home.note is not None and "different filesystem" in home.note


def test_breakdown_clamps_an_impossible_residual_and_says_so():
    """Categories bigger than the filesystem mean one of them lives elsewhere —
    render a warning, never a negative bar."""
    report = _build(docker_df_fn=lambda: _df(images="200GB"))
    residual = next(g for g in report.groups if g.key == db.RESIDUAL_KEY)
    assert residual.bytes == 0
    assert residual.note is not None and "another disk" in residual.note
    assert report.complete is False
    assert any("different filesystem" in w for w in report.warnings)


def test_breakdown_dict_is_json_serialisable_and_complete():
    payload = _build().to_dict()
    assert json.loads(json.dumps(payload))["version"] == db.BREAKDOWN_VERSION
    assert {g["key"] for g in payload["groups"]} >= {
        "docker_images",
        "docker_containers",
        "docker_volumes",
        "docker_build_cache",
        "olano_home",
        "swap",
        db.RESIDUAL_KEY,
    }


# ── cache ────────────────────────────────────────────────────────────────────


def test_cached_breakdown_respects_the_ttl_and_invalidation():
    db.invalidate_cache()
    calls = {"n": 0}
    clock = {"t": 1000.0}

    def build_fn(**_kw):
        calls["n"] += 1
        return _build()

    def call():
        return db.cached_breakdown(
            path="/",
            home=Path("/x"),
            ttl_seconds=300.0,
            now_fn=lambda: clock["t"],
            build_fn=build_fn,
        )

    assert call() is not None
    call()
    assert calls["n"] == 1, "second call inside the TTL must reuse the memo"

    clock["t"] += 301
    call()
    assert calls["n"] == 2, "past the TTL it recomputes"

    db.invalidate_cache()
    call()
    assert calls["n"] == 3, "invalidate_cache forces a rebuild"
    db.invalidate_cache()


def test_cached_breakdown_is_keyed_on_the_path_and_home():
    """`disk_monitor.path` is editable in Config and re-read every tick — a memo
    keyed on time alone would serve the old filesystem's numbers after a change."""
    db.invalidate_cache()
    seen: list[tuple[str, str]] = []

    def build_fn(*, path, home, **_kw):
        seen.append((str(path), str(home)))
        return _build()

    call = lambda p, h: db.cached_breakdown(  # noqa: E731
        path=p, home=Path(h), now_fn=lambda: 1000.0, build_fn=build_fn
    )
    call("/", "/home/olano/.lionclaw")
    call("/", "/home/olano/.lionclaw")
    assert seen == [("/", "/home/olano/.lionclaw")]
    call("/mnt/data", "/home/olano/.lionclaw")
    assert len(seen) == 2, "a different monitored path must not reuse the memo"
    db.invalidate_cache()


def test_cached_breakdown_returns_none_when_the_path_cannot_be_measured():
    db.invalidate_cache()

    def build_fn(**_kw):
        raise OSError("no such path")

    assert db.cached_breakdown(path="/nope", home=Path("/x"), build_fn=build_fn) is None
    db.invalidate_cache()


# ── text rendering ───────────────────────────────────────────────────────────


def test_render_text_orders_by_size_and_keeps_the_residual_last():
    lines = db.render_text(_build().to_dict())
    assert lines and "where the space went" in lines[0]
    labels = [line.split(":")[0].strip("- ").strip() for line in lines[1:]]
    assert labels[0] == "Docker images"
    assert labels[-1] == "free"
    assert "System & other host files" in labels
    assert any("reclaimable" in line for line in lines)


def test_render_text_of_nothing_is_nothing():
    assert db.render_text(None) == []
    assert db.render_text({}) == []
