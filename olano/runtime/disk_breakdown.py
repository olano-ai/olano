"""Whole-disk breakdown — one level above the olano-home directory scan.

:mod:`olano.runtime.disk_scan` answers "where are the bytes *inside*
``~/.olano``". On a managed deployment that home is a small minority of the
disk, so the dashboard could show 61% used (14 GB) next to a "largest
directories" list totalling 759 MB, with no way to ask where the other 13 GB
went. The missing 13 GB is not mysterious — it is Docker images, the swap file
and the operating system — but nothing on the box said so, and the runtime
container cannot walk the host filesystem to find out.

This module attributes the *whole* filesystem from the three things the runtime
CAN measure from inside its container:

- **Docker** — ``docker system df`` over the bind-mounted socket (images,
  containers, local volumes, build cache, each with its reclaimable share).
  This is the big one: a managed box holds the backend, sandbox and dashboard
  images plus Postgres.
- **The olano home** — the latest :mod:`disk_scan` snapshot.
- **Swap files** — ``/proc/swaps`` (host-wide inside a container; a swap
  *partition* is not on this filesystem and is excluded).

Everything left over is reported as one honest residual group: the OS, the
Docker engine, system logs, the deployment checkout — host files the runtime
has no window onto. Naming it is the point; an unlabelled 13 GB reads as a bug,
a labelled 5 GB reads as an operating system.

Two traps this code exists to avoid:

- **``st_dev`` cannot decide "same disk" here.** Inside the runtime container
  ``/`` is an overlayfs and the home is a bind mount, so their device numbers
  differ while the bytes land on one host filesystem. Sameness is therefore
  decided by comparing ``statvfs`` totals (:func:`_same_volume`), which
  overlay2 reports from the underlying filesystem.
- **``docker system df`` is not free.** It sizes every local volume, so it is
  cached behind a TTL (:func:`cached_breakdown`) instead of running on each
  dashboard poll.

Stdlib only, everything injectable — same convention as
:mod:`olano.runtime.disk_monitor`.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BREAKDOWN_VERSION = 1

# docker system df sizes are re-measured at most this often — the disk moves in
# minutes, not seconds, and the dashboard polls the disk report every 5s.
DEFAULT_TTL_SECONDS = 300.0

# Two statvfs calls a few microseconds apart can disagree by a few blocks on a
# live filesystem; anything under this is still "the same volume".
_FREE_TOLERANCE_BYTES = 64 * 1024 * 1024

_DOCKER_GROUPS: tuple[tuple[str, str, str], ...] = (
    (
        "docker_images",
        "Docker images",
        "The runtime, agent-sandbox, dashboard and database images this "
        "deployment runs. Pulled fresh on every update; superseded copies are "
        "reclaimable.",
    ),
    (
        "docker_containers",
        "Docker containers",
        "Writable layers and log files of the running containers, including "
        "the per-agent execution sandboxes.",
    ),
    (
        "docker_volumes",
        "Docker volumes",
        "Named volumes — the Postgres database (threads, agent memory) and "
        "the web server's TLS certificates.",
    ),
    (
        "docker_build_cache",
        "Docker build cache",
        "Intermediate layers left by locally built images. Empty on a managed "
        "deployment, which pulls pre-built images instead of building them.",
    ),
)

RESIDUAL_KEY = "system"
RESIDUAL_LABEL = "System & other host files"
RESIDUAL_DETAIL = (
    "Everything outside Docker and the Olano home: the operating system and "
    "kernel, the Docker engine itself, system logs, package caches and the "
    "deployment checkout. The runtime runs in a container and cannot read "
    "these paths, so this is measured as what is left of the used space."
)


# ── docker system df ─────────────────────────────────────────────────────────

_SIZE_UNITS = {"B": 1, "KB": 10**3, "MB": 10**6, "GB": 10**9, "TB": 10**12, "PB": 10**15}


def parse_docker_size(text: str) -> int | None:
    """``"3.16GB"`` → bytes; ``"3.1GB (73%)"`` keeps the size part. None = unparseable.

    docker prints go-units human sizes (decimal, three significant figures), so
    the result is docker's own rounding — good to ~0.5% on a multi-GB figure,
    which is well inside what a disk breakdown needs.
    """
    head = str(text).strip().split(" (")[0].strip()
    if head in ("0", "0B"):
        return 0
    # Split the trailing unit off the number without a regex: the grammar is
    # "<float><unit>" with optional spaces, and unit is one of _SIZE_UNITS.
    idx = len(head)
    while idx > 0 and (head[idx - 1].isalpha()):
        idx -= 1
    number, unit = head[:idx].strip(), head[idx:].strip().upper()
    if not number or unit not in _SIZE_UNITS:
        return None
    try:
        return int(float(number) * _SIZE_UNITS[unit])
    except ValueError:
        return None


def docker_available(
    which_fn: Callable[[str], str | None] = shutil.which,
    docker_fn: Callable[[], bool] | None = None,
) -> tuple[bool, str | None]:
    """``(usable, reason_when_not)`` for the docker CLI + daemon socket."""
    from olano.runtime.disk_monitor import docker_daemon_reachable

    if which_fn("docker") is None:
        return False, "docker CLI not installed in the runtime container"
    if not (docker_fn or docker_daemon_reachable)():
        return False, "docker socket not mounted into the runtime container"
    return True, None


def collect_docker_df(
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    which_fn: Callable[[str], str | None] = shutil.which,
    docker_fn: Callable[[], bool] | None = None,
) -> dict[str, Any] | None:
    """``docker system df`` totals, or None when docker isn't usable here.

    Uses ``--format {{json .}}`` (one JSON object per line, one per resource
    type) — supported far longer back than ``--format json``.
    """
    usable, _reason = docker_available(which_fn, docker_fn)
    if not usable:
        return None
    try:
        proc = run_fn(
            ["docker", "system", "df", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.debug("disk breakdown: docker system df failed: %s", e)
        return None
    if proc.returncode != 0:
        logger.debug("disk breakdown: docker system df exit %s", proc.returncode)
        return None

    out: dict[str, Any] = {}
    key_map = {
        "Images": "images",
        "Containers": "containers",
        "Local Volumes": "volumes",
        "Build Cache": "build_cache",
    }
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        key = key_map.get(str(row.get("Type", "")))
        if key is None:
            continue
        out[key] = {
            "count": row.get("TotalCount"),
            "active": row.get("Active"),
            "size_bytes": parse_docker_size(row.get("Size", "")),
            "reclaimable_bytes": parse_docker_size(row.get("Reclaimable", "")),
        }
    return out or None


# ── swap ─────────────────────────────────────────────────────────────────────


def read_swap_files(proc_swaps: str | Path = "/proc/swaps") -> list[dict[str, Any]]:
    """File-backed swap entries as ``[{"path", "bytes"}, …]``.

    ``/proc/swaps`` is host-wide even inside a container, which is exactly what
    we want: the provisioner's ``/swapfile`` occupies real space on the disk the
    monitor measures. Swap *partitions* live outside any filesystem and are
    skipped — counting them would inflate the attributed total.
    """
    try:
        raw = Path(proc_swaps).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines()[1:]:  # first line is the column header
        parts = line.split()
        if len(parts) < 3 or parts[1] != "file":
            continue
        try:
            size_kib = int(parts[2])
        except ValueError:
            continue
        entries.append({"path": parts[0], "bytes": size_kib * 1024})
    return entries


# ── filesystem identity ──────────────────────────────────────────────────────


def _same_volume(
    a: str | Path,
    b: str | Path,
    usage_fn: Callable[[str], Any] = shutil.disk_usage,
) -> bool:
    """Do two paths land on the same underlying filesystem?

    Deliberately NOT an ``st_dev`` comparison: inside the runtime container
    ``/`` is an overlayfs and the olano home is a bind mount, so their device
    numbers differ even though every byte written to either one comes off the
    same host disk. overlay2 answers ``statvfs`` from the filesystem holding
    its upper directory, so equal totals (and near-equal free space) is the
    check that survives containerisation.
    """
    try:
        ua, ub = usage_fn(str(a)), usage_fn(str(b))
    except OSError:
        return False
    return (
        int(ua.total) == int(ub.total) and abs(int(ua.free) - int(ub.free)) <= _FREE_TOLERANCE_BYTES
    )


# ── the breakdown ────────────────────────────────────────────────────────────


@dataclass
class BreakdownGroup:
    key: str
    label: str
    detail: str
    bytes: int | None = None
    reclaimable_bytes: int | None = None
    note: str | None = None  # why bytes is None, or a caveat worth showing

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "detail": self.detail,
            "bytes": self.bytes,
            "reclaimable_bytes": self.reclaimable_bytes,
            "note": self.note,
        }


@dataclass
class DiskBreakdown:
    path: str
    generated_at: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    groups: list[BreakdownGroup] = field(default_factory=list)
    complete: bool = True  # every category measured; False = residual absorbs more
    warnings: list[str] = field(default_factory=list)

    @property
    def attributed_bytes(self) -> int:
        return sum(g.bytes or 0 for g in self.groups if g.key != RESIDUAL_KEY)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": BREAKDOWN_VERSION,
            "path": self.path,
            "generated_at": self.generated_at,
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "attributed_bytes": self.attributed_bytes,
            "groups": [g.to_dict() for g in self.groups],
            "complete": self.complete,
            "warnings": self.warnings,
        }


def build_breakdown(
    *,
    path: str,
    home: Path,
    now: datetime | None = None,
    usage_fn: Callable[[str], Any] = shutil.disk_usage,
    docker_df_fn: Callable[[], dict[str, Any] | None] = collect_docker_df,
    docker_probe_fn: Callable[[], tuple[bool, str | None]] = docker_available,
    swap_fn: Callable[[], list[dict[str, Any]]] = read_swap_files,
    scan_loader: Callable[[Path], dict[str, Any] | None] | None = None,
) -> DiskBreakdown:
    """Attribute the used space on ``path`` to Docker / the home / swap / the rest.

    Raises ``OSError`` only if ``path`` itself cannot be measured; every
    individual category degrades to "not measured" with a note rather than
    failing the whole report.
    """
    if scan_loader is None:
        from olano.runtime.disk_scan import load_snapshot

        scan_loader = load_snapshot

    stamp = (now or datetime.now(timezone.utc)).isoformat()
    usage = usage_fn(path)
    used, free = int(usage.used), int(usage.free)
    report = DiskBreakdown(
        path=path,
        generated_at=stamp,
        total_bytes=int(usage.total),
        used_bytes=used,
        free_bytes=free,
    )

    # ── Docker ──
    docker_ok, docker_reason = docker_probe_fn()
    df = docker_df_fn() if docker_ok else None
    if df is None and docker_ok:
        docker_reason = "docker system df did not answer"
    for key, label, detail in _DOCKER_GROUPS:
        row = (df or {}).get(key.removeprefix("docker_")) or {}
        size = row.get("size_bytes") if isinstance(row, dict) else None
        group = BreakdownGroup(
            key=key,
            label=label,
            detail=detail,
            bytes=int(size) if isinstance(size, int) else None,
            reclaimable_bytes=(
                int(row["reclaimable_bytes"])
                if isinstance(row.get("reclaimable_bytes"), int)
                else None
            ),
        )
        if group.bytes is None:
            group.note = docker_reason or "not reported by docker"
        count = row.get("count") if isinstance(row, dict) else None
        if isinstance(count, int | str) and str(count).strip() not in ("", "0"):
            group.detail = f"{group.detail} ({count} present)"
        report.groups.append(group)
    if df is None:
        report.complete = False
        report.warnings.append(
            "Docker usage could not be measured"
            + (f" ({docker_reason})" if docker_reason else "")
            + " — it is counted in the system total instead."
        )

    # ── olano home ──
    home_group = BreakdownGroup(
        key="olano_home",
        label="Olano home",
        detail=(
            "Agent workspaces, conversation transcripts, memory, skills, the "
            "credentials vault and the agent sandbox tool layer. Broken down "
            "directory by directory below."
        ),
    )
    snapshot = scan_loader(home) or None
    if snapshot is None:
        home_group.note = "no directory scan yet — press Scan now"
        report.complete = False
    elif not _same_volume(home, path, usage_fn):
        home_group.note = f"stored on a different filesystem than {path}"
    else:
        home_group.bytes = int(snapshot.get("total_bytes") or 0)
        if snapshot.get("truncated"):
            home_group.note = "last scan hit its time budget — this is a floor"
    report.groups.append(home_group)

    # ── swap ──
    swap_entries = swap_fn()
    if swap_entries:
        total_swap = sum(int(e["bytes"]) for e in swap_entries)
        names = ", ".join(str(e["path"]) for e in swap_entries)
        report.groups.append(
            BreakdownGroup(
                key="swap",
                label="Swap file",
                detail=(
                    f"Virtual memory backed by a file on this disk ({names}). "
                    "It keeps a small machine from running out of RAM, and it "
                    "occupies its full size whether or not it is in use."
                ),
                bytes=total_swap,
            )
        )

    # ── the residual ──
    residual = used - report.attributed_bytes
    residual_group = BreakdownGroup(
        key=RESIDUAL_KEY, label=RESIDUAL_LABEL, detail=RESIDUAL_DETAIL, bytes=max(0, residual)
    )
    if residual < 0:
        # Categories summed past the filesystem's used space: something we
        # attributed lives on a different disk (a Docker data-root moved to an
        # attached volume is the realistic cause). Say so rather than render a
        # negative bar.
        residual_group.bytes = 0
        residual_group.note = (
            "measured categories exceed the used space on this filesystem — "
            "some of them are stored on another disk"
        )
        report.complete = False
        report.warnings.append(
            "Docker or the Olano home appears to live on a different filesystem "
            f"than {path}; the totals below overlap."
        )
    report.groups.append(residual_group)
    return report


# ── TTL cache (docker system df sizes every local volume — not a poll path) ──

_CACHE_LOCK = threading.Lock()
# (path, home) is part of the key: `disk_monitor.path` is editable from Config
# and re-read every tick, so a memo keyed on time alone would keep serving the
# old filesystem's numbers for up to a TTL after the change.
_CACHE: tuple[tuple[str, str], float, dict[str, Any]] | None = None


def invalidate_cache() -> None:
    """Drop the memoised breakdown — call it when a human asks for fresh numbers."""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


def cached_breakdown(
    *,
    path: str,
    home: Path,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    now_fn: Callable[[], float] = time.monotonic,
    build_fn: Callable[..., DiskBreakdown] = build_breakdown,
) -> dict[str, Any] | None:
    """The breakdown, recomputed at most once per ``ttl_seconds``.

    None when the filesystem itself cannot be measured — the caller already
    surfaces that failure through the ordinary disk report.
    """
    global _CACHE
    key = (str(path), str(home))
    stamp = now_fn()
    with _CACHE_LOCK:
        if _CACHE is not None and _CACHE[0] == key and (stamp - _CACHE[1]) < max(0.0, ttl_seconds):
            return _CACHE[2]
    try:
        result = build_fn(path=path, home=home).to_dict()
    except OSError as e:
        logger.debug("disk breakdown: cannot measure %s: %s", path, e)
        return None
    except Exception:  # pragma: no cover - a diagnostic must never break its caller
        logger.debug("disk breakdown failed", exc_info=True)
        return None
    with _CACHE_LOCK:
        _CACHE = (key, stamp, result)
    return result


def render_text(breakdown: dict[str, Any] | None, indent: str = "  ") -> list[str]:
    """CLI/chat lines for a breakdown dict — empty list when there is none."""
    if not breakdown:
        return []
    from olano.runtime.disk_monitor import format_bytes

    lines = [
        f"{indent}where the space went ({format_bytes(breakdown.get('used_bytes') or 0)} used):"
    ]
    groups = sorted(
        breakdown.get("groups") or [],
        key=lambda g: (g.get("key") == RESIDUAL_KEY, -(g.get("bytes") or 0)),
    )
    for group in groups:
        size = group.get("bytes")
        shown = format_bytes(size) if isinstance(size, int) else "not measured"
        extra = ""
        reclaimable = group.get("reclaimable_bytes")
        if isinstance(reclaimable, int) and reclaimable > 0:
            extra = f" ({format_bytes(reclaimable)} reclaimable)"
        elif group.get("note"):
            extra = f" ({group['note']})"
        if size == 0 and not extra:
            continue  # empty categories are noise in a text report
        lines.append(f"{indent}  - {group.get('label')}: {shown}{extra}")
    free = breakdown.get("free_bytes")
    if isinstance(free, int):
        lines.append(f"{indent}  - free: {format_bytes(free)}")
    for warning in breakdown.get("warnings") or []:
        lines.append(f"{indent}  ! {warning}")
    return lines
