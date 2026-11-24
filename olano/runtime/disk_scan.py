"""Directory-size scan of the olano home — throttled, depth-bounded, daily.

The threshold monitor (:mod:`olano.runtime.disk_monitor`) says *how full*
the disk is; this module says *where the bytes are*. One walk of
``~/.olano`` aggregates file sizes into depth-limited buckets — two levels
below the home root, plus two levels below each ``agents/<name>`` so per-agent
growth is attributable — and persists the result to
``<home>/run/disk-scan.json`` for the dashboard (Overview DiskCard, Files
view), ``olano disk``, ``/api/diagnostics/disk`` and the cloud disk
reporter.

The walk must visit every file to produce true sizes (a depth-capped traversal
would undercount), so cost is controlled instead of scope: the scan runs on a
niced daemon thread (one at a time process-wide), sleeps between batches of
entries, and stops at a hard time budget (``truncated: true`` marks a partial
result). Scheduling lives in :func:`disk_monitor.disk_monitor_tick` — at most
one scan per ``disk_monitor.scan.interval_hours``, stamped in the monitor
state file so restarts don't re-trigger it.

Stdlib only; everything injectable for tests (same convention as
:mod:`olano.runtime.disk_monitor`).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCAN_VERSION = 1

# Bucket paths use "/" regardless of platform so the API/UI never see os.sep.
_SEP = "/"


def snapshot_path(home: Path) -> Path:
    return home / "run" / "disk-scan.json"


@dataclass
class DiskScanResult:
    root: str
    scanned_at: str  # ISO UTC (scan start)
    duration_seconds: float
    total_bytes: int
    total_files: int
    truncated: bool  # time budget hit — sizes are a floor, not the truth
    entries_capped: bool  # smallest buckets dropped to honour max_entries
    entries: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SCAN_VERSION,
            "root": self.root,
            "scanned_at": self.scanned_at,
            "duration_seconds": round(self.duration_seconds, 2),
            "total_bytes": self.total_bytes,
            "total_files": self.total_files,
            "truncated": self.truncated,
            "entries_capped": self.entries_capped,
            "entries": self.entries,
        }


def _bucket_cap(parts: tuple[str, ...], depth: int, agent_depth: int) -> int:
    """How many leading path components form this directory's deepest bucket.

    Regular tree: ``depth`` levels below the root. Under ``agents/<name>`` the
    cap extends by ``agent_depth`` so each agent's subtree gets its own
    breakdown (``agents/<name>/<d1>/<d2>`` at the defaults).
    """
    if parts and parts[0] == "agents":
        return max(depth, 2 + agent_depth)
    return depth


def scan_directory_sizes(
    root: Path,
    *,
    depth: int = 2,
    agent_depth: int = 2,
    time_budget_seconds: float = 600.0,
    throttle_every: int = 2000,
    throttle_ms: int = 20,
    max_entries: int = 400,
    now: datetime | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> DiskScanResult:
    """One full walk of ``root``, aggregated into depth-limited buckets.

    Sizes are ``lstat`` apparent sizes; symlinks are never followed (the link
    itself is counted, its target is not) and hardlinked files count once.
    Unreadable entries are skipped — a scan must never fail on permissions.
    """
    started = now or datetime.now(timezone.utc)
    t0 = time.monotonic()
    deadline = t0 + max(1.0, time_budget_seconds)
    pause = max(0, throttle_ms) / 1000.0

    buckets: dict[tuple[str, ...], list[int]] = {}  # parts -> [bytes, files]
    total_bytes = 0
    total_files = 0
    truncated = False
    processed = 0
    seen_hardlinks: set[tuple[int, int]] = set()

    # Iterative scandir (cartography/walker.py convention): DirEntry avoids a
    # path join per file, and the explicit stack makes the time-budget bailout
    # a clean break instead of os.walk generator surgery.
    stack: list[tuple[str, tuple[str, ...]]] = [(str(root), ())]
    while stack:
        if time.monotonic() > deadline:
            truncated = True
            break
        dirpath, parts = stack.pop()
        cap = _bucket_cap(parts, depth, agent_depth)
        prefixes = [parts[:i] for i in range(1, min(len(parts), cap) + 1)]
        try:
            it = os.scandir(dirpath)
        except OSError:
            continue
        with it:
            for entry in it:
                processed += 1
                if pause and throttle_every > 0 and processed % throttle_every == 0:
                    sleep_fn(pause)
                    if time.monotonic() > deadline:
                        truncated = True
                        break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((entry.path, (*parts, entry.name)))
                        continue
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_nlink > 1:
                    key = (st.st_dev, st.st_ino)
                    if key in seen_hardlinks:
                        continue
                    seen_hardlinks.add(key)
                size = int(st.st_size)
                total_bytes += size
                total_files += 1
                for prefix in prefixes:
                    slot = buckets.get(prefix)
                    if slot is None:
                        buckets[prefix] = [size, 1]
                    else:
                        slot[0] += size
                        slot[1] += 1
        if truncated:
            break

    ordered = sorted(buckets.items(), key=lambda kv: kv[1][0], reverse=True)
    entries_capped = len(ordered) > max_entries
    entries = [
        {"path": _SEP.join(parts), "depth": len(parts), "bytes": slot[0], "files": slot[1]}
        for parts, slot in ordered[:max_entries]
    ]
    return DiskScanResult(
        root=str(root),
        scanned_at=started.isoformat(),
        duration_seconds=time.monotonic() - t0,
        total_bytes=total_bytes,
        total_files=total_files,
        truncated=truncated,
        entries_capped=entries_capped,
        entries=entries,
    )


# ── snapshot persistence ─────────────────────────────────────────────────────


def save_snapshot(home: Path, result: DiskScanResult) -> None:
    path = snapshot_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        # Same posture as the monitor state: a full disk must never crash the
        # machinery that exists to diagnose it.
        logger.debug("disk scan: could not persist snapshot: %s", e)


def load_snapshot(home: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(snapshot_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != SCAN_VERSION:
        return None
    return raw


# Hot-path cache for /api/files (per-directory listings arrive in bursts; the
# snapshot only changes when a scan lands, so key on the file's mtime).
_CACHE_LOCK = threading.Lock()
_CACHE: tuple[str, int, dict[str, Any] | None, dict[str, dict[str, Any]]] | None = None


def cached_snapshot(home: Path) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    """``(snapshot, {bucket_path: entry})`` — reread only when the file changes."""
    global _CACHE
    path = snapshot_path(home)
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return None, {}
    with _CACHE_LOCK:
        if _CACHE is not None and _CACHE[0] == str(path) and _CACHE[1] == mtime_ns:
            return _CACHE[2], _CACHE[3]
    snap = load_snapshot(home)
    index = {e["path"]: e for e in (snap or {}).get("entries", []) if isinstance(e, dict)}
    with _CACHE_LOCK:
        _CACHE = (str(path), mtime_ns, snap, index)
    return snap, index


def dir_size_lookup(home: Path, abs_dir: Path) -> dict[str, Any] | None:
    """Scan entry for an absolute directory path, or None when it's outside
    the scanned tree / deeper than the bucket cap / not scanned yet."""
    _, index = cached_snapshot(home)
    if not index:
        return None
    try:
        rel = abs_dir.resolve().relative_to(home.resolve())
    except (ValueError, OSError):
        return None
    return index.get(_SEP.join(rel.parts))


# ── background execution (one scan at a time, off every request path) ────────

_SPAWN_LOCK = threading.Lock()
_INFLIGHT = False


def scan_running() -> bool:
    return _INFLIGHT


def spawn_scan(
    home: Path,
    settings: Any,
    *,
    on_done: Callable[[dict[str, Any]], None] | None = None,
) -> bool:
    """Run a scan on a niced daemon thread; ``False`` = one already running.

    ``settings`` is a ``DiskScanSettings`` (duck-typed so admin/CLI callers can
    pass a plain namespace). The snapshot is persisted before ``on_done`` fires
    so consumers can re-read it immediately.
    """
    global _INFLIGHT
    with _SPAWN_LOCK:
        if _INFLIGHT:
            return False
        _INFLIGHT = True

    def _run() -> None:
        global _INFLIGHT
        try:
            try:
                os.nice(10)  # on Linux setpriority applies to the calling thread
            except (AttributeError, OSError):  # pragma: no cover - platform dependent
                pass
            result = scan_directory_sizes(
                home,
                depth=int(getattr(settings, "depth", 2)),
                agent_depth=int(getattr(settings, "agent_depth", 2)),
                time_budget_seconds=float(getattr(settings, "time_budget_seconds", 600)),
                throttle_ms=int(getattr(settings, "throttle_ms", 20)),
                max_entries=int(getattr(settings, "max_entries", 400)),
            )
            save_snapshot(home, result)
            logger.info(
                "disk scan: %s files, %s under %s in %.1fs%s",
                result.total_files,
                _human(result.total_bytes),
                result.root,
                result.duration_seconds,
                " (truncated at time budget)" if result.truncated else "",
            )
            if on_done is not None:
                try:
                    on_done(result.to_dict())
                except Exception:  # pragma: no cover - callback must not kill the thread
                    logger.debug("disk scan on_done callback failed", exc_info=True)
        except Exception as e:  # a background scan must never crash the runtime
            logger.warning("disk scan failed: %s", e)
        finally:
            with _SPAWN_LOCK:
                _INFLIGHT = False

    threading.Thread(target=_run, name="disk-scan", daemon=True).start()
    return True


def _human(n: int) -> str:
    from olano.runtime.disk_monitor import format_bytes

    return format_bytes(n)
