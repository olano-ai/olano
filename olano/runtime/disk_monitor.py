"""Disk-space monitor + automatic cleanup engine.

The runtime server runs :func:`disk_monitor_tick` on a background loop (see
``RuntimeServer._disk_monitor_loop``): it measures ``shutil.disk_usage`` on the
configured path, alerts on threshold transitions (runtime log + dashboard SSE),
and — when usage crosses ``cleanup_percent`` — runs the enabled cleanup actions
from the fixed :data:`ACTION_REGISTRY` allow-list. Config can only *select*
registry actions, never supply commands (``argv`` tuples are code, and the one
templated value, the journald vacuum size, is regex-validated at settings load).

Stdlib only — no psutil (same convention as :mod:`olano.admin.memory`).
Everything is injected (``now``, ``usage_fn``, ``run_fn``, …) so tests drive the
tick directly and never run the loop.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from olano.config.system_settings import (
    DISK_CLEANUP_SAFE_ACTIONS,
    DiskMonitorSettings,
)

logger = logging.getLogger(__name__)

# Re-alert while stuck in warn/critical (transitions always alert immediately).
REALERT_MINUTES = 360
STATE_VERSION = 1

_LEVEL_RANK = {"ok": 0, "warn": 1, "critical": 2}

# Tool output lines worth surfacing verbatim (docker/journalctl print their own
# reclaim totals; the free-space delta we measure can miss cross-partition wins).
_REPORTED_RE = re.compile(r"(?i)(total reclaimed space|vacuuming done|freed)[^\n]*")
_PERMISSION_RE = re.compile(r"(?i)permission denied|access denied|not permitted|need to be root")


def format_bytes(n: int | float) -> str:
    """Human size — ``11.6 GB`` style, for alerts/CLI/dashboard strings."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < 1024 or unit == "GB":
            break
        value /= 1024
    else:  # pragma: no cover - unreachable
        unit = "GB"
    if unit == "GB" and abs(value) >= 1024:
        return f"{value / 1024:.1f} TB"
    return f"{value:.0f} {unit}" if unit in ("B", "KB") else f"{value:.1f} {unit}"


# ── action registry (the fixed allow-list) ───────────────────────────────────


def _run_olano_caches(home: Path) -> str:
    """Internal action: olano's own housekeeping + media caches."""
    # Deliberately reuses the boot-time housekeeping rules (sessions, memories,
    # degraded log) and wires in the platform media-cache cleaners.
    from olano.platforms.base import cleanup_document_cache, cleanup_image_cache
    from olano.runtime.housekeeping import run_housekeeping

    report = run_housekeeping(home)
    images = cleanup_image_cache(max_age_hours=24)
    documents = cleanup_document_cache(max_age_hours=24)
    return (
        f"{report.empty_sessions_deleted} empty sessions removed, "
        f"{report.sessions_compressed} sessions compressed, "
        f"{report.memories_archived} memories archived, "
        f"{images} cached images + {documents} cached documents deleted"
    )


@dataclass(frozen=True)
class ActionSpec:
    """One allow-listed cleanup action. ``argv`` may contain the single
    ``{vacuum_size}`` placeholder; nothing else is ever templated."""

    name: str
    description: str
    kind: str  # "internal" | "command"
    safe: bool  # safe = only deletes caches/old logs; unsafe actions are opt-in
    binary: str | None = None
    argv: tuple[str, ...] = ()
    timeout_seconds: int = 300
    sudo_fallback: bool = False  # retry once as ``sudo -n <argv>`` on failure
    skip_in_container: bool = False  # host-level action; meaningless inside docker
    needs_docker_daemon: bool = False  # requires a reachable docker daemon (socket/DOCKER_HOST)
    run_internal: Callable[[Path], str] | None = None


# ── superseded-image reclaim ─────────────────────────────────────────────────
#
# "Keep the last good image and nothing else." Every update pulls a fresh
# backend + dashboard + sandbox image; `compose up -d` never removes the ones
# it replaced, and re-tagging olano-sandbox:latest only ORPHANS the previous
# sandbox image. So each update used to leak a full image set (multiple GB) —
# on a 25 GB disk, two or three updates is the whole disk.
#
# `docker image prune` (the pre-existing safe action) only catches DANGLING
# images, which misses every superseded copy that still carries a tag, and
# `docker system prune -af` is the opposite problem: it also deletes
# olano-sandbox:latest whenever no sandbox container happens to be running,
# silently disabling agent code-exec until the next update re-tags it. Hence
# this action, whose rule is neither "untagged" nor "unused" but:
#
#   keep what a container actually uses, plus the local names the runtime
#   resolves by hand; drop every other image in repos WE publish.
#
# Nothing outside `OLANO_IMAGE_PREFIXES` is ever touched — postgres, caddy and
# anything the customer pulled themselves are not ours to reclaim.
OLANO_IMAGE_PREFIXES: tuple[str, ...] = (
    "ghcr.io/olano-ai/",
    "olano-backend",
    "olano-dashboard",
    "olano-sandbox",
    "olano-browser",
    # Pre-rename image, still present on boxes provisioned before it.
    "ghcr.io/olano-ai/lionclaw-sandbox",
    "lionclaw-sandbox",
)

# Tags the runtime resolves by NAME rather than through a running container, so
# "unused right now" must not mean "safe to delete": the exec sandbox is
# spawned on demand (backend_config._default_sandbox_image), and between agent
# sessions no container references it at all. The live browser image is the
# same: started per agent on demand, idle between sessions, and a re-download
# on the next browse would cost a minute and ~1 GB of traffic.
PROTECTED_IMAGE_TAGS: frozenset[str] = frozenset(
    {"olano-sandbox:latest", "lionclaw-sandbox:latest", "olano-browser:latest"}
)


def is_olano_image(repository: str) -> bool:
    return any(repository.startswith(prefix) for prefix in OLANO_IMAGE_PREFIXES)


def select_stale_olano_images(
    images: list[dict[str, str]],
    in_use_image_ids: set[str],
    *,
    protected_tags: frozenset[str] = PROTECTED_IMAGE_TAGS,
) -> list[str]:
    """Image references (``repo:tag``) safe to drop. Pure — unit-tested.

    ``images`` is ``[{"id", "repository", "tag"}, …]`` as read from
    ``docker images``; ``in_use_image_ids`` are the full image IDs every
    container (running **or** stopped) was created from.

    Removal is by REFERENCE, never by id and never with ``--force``: untagging
    is all this action does, and an image left with no tags is collected by the
    dangling prune that follows. That way an image which turns out to be shared
    or in use simply survives instead of taking a container down with it.
    """
    stale: list[str] = []
    for image in images:
        repository, tag = image.get("repository", ""), image.get("tag", "")
        if not repository or repository == "<none>" or tag == "<none>":
            continue  # dangling — the prune step owns these
        if not is_olano_image(repository):
            continue
        if f"{repository}:{tag}" in protected_tags:
            continue
        if image.get("id", "") in in_use_image_ids:
            continue
        stale.append(f"{repository}:{tag}")
    return stale


def _docker(argv: list[str], run_fn: Callable[..., subprocess.CompletedProcess[str]]) -> str:
    proc = run_fn(argv, capture_output=True, text=True, timeout=120)
    return proc.stdout if proc.returncode == 0 else ""


def _run_olano_stale_images(
    home: Path,
    *,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    which_fn: Callable[[str], str | None] = shutil.which,
    docker_fn: Callable[[], bool] | None = None,
) -> str:
    """Internal action: keep the last good Olano image, drop the superseded ones.

    ``docker_fn`` defaults to :func:`docker_daemon_reachable`, resolved at call
    time because that helper is defined further down this module (a default
    argument would be evaluated at import).
    """
    docker_fn = docker_fn or docker_daemon_reachable
    if which_fn("docker") is None:
        return "docker not installed"
    if not docker_fn():
        return "docker daemon not reachable"

    images = []
    for line in _docker(
        ["docker", "images", "--no-trunc", "--format", "{{.ID}}\t{{.Repository}}\t{{.Tag}}"],
        run_fn,
    ).splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            images.append({"id": parts[0], "repository": parts[1], "tag": parts[2]})

    container_ids = _docker(["docker", "ps", "-a", "--format", "{{.ID}}"], run_fn).split()
    in_use: set[str] = set()
    if container_ids:
        in_use = set(
            _docker(["docker", "inspect", "--format", "{{.Image}}", *container_ids], run_fn).split()
        )

    # Refuse to act when nothing on this box runs one of our images: either the
    # stack is down or docker is answering oddly, and in that state "unused"
    # cannot distinguish the image the box needs to boot from a superseded one.
    if not any(
        image["id"] in in_use for image in images if is_olano_image(image.get("repository", ""))
    ):
        return "no running Olano container to compare against — nothing removed"

    stale = select_stale_olano_images(images, in_use)
    removed = 0
    for reference in stale:
        proc = run_fn(["docker", "rmi", reference], capture_output=True, text=True, timeout=300)
        if proc.returncode == 0:
            removed += 1

    # Untagged leftovers (including the previous olano-sandbox:latest, orphaned
    # by the re-tag) plus the build cache, which is pure cache on any box that
    # ever built images locally.
    pruned = _docker(["docker", "image", "prune", "-f"], run_fn)
    builder = _docker(["docker", "builder", "prune", "-f"], run_fn)
    reclaimed = " / ".join(
        m.group(0) for m in (_REPORTED_RE.search(pruned), _REPORTED_RE.search(builder)) if m
    )
    return (
        f"{removed} superseded Olano image(s) removed"
        + (f" of {len(stale)} candidates" if removed != len(stale) else "")
        + (f"; {reclaimed}" if reclaimed else "")
    )


def _run_revisions_gc(home: Path) -> str:
    """Internal action: repack the home revision-history repo."""
    from olano.revisions.engine import RevisionsEngine

    engine = RevisionsEngine(home)
    if not (engine.repo / "HEAD").exists() or not engine.available():
        return "no revisions repo"
    before = engine.repo_size_bytes()
    engine.gc()
    return f"revisions.git repacked ({before - engine.repo_size_bytes()} bytes freed)"


ACTION_REGISTRY: dict[str, ActionSpec] = {
    spec.name: spec
    for spec in (
        ActionSpec(
            # Renamed from "lionclaw_caches"; system_settings normalizes the
            # legacy name in stored configs to this one.
            name="olano_caches",
            description="Olano housekeeping: stale sessions, old memories, media caches",
            kind="internal",
            safe=True,
            run_internal=_run_olano_caches,
        ),
        ActionSpec(
            name="revisions_gc",
            description=(
                "git gc on <home>/revisions.git — repacks home revision history; "
                "never deletes commits"
            ),
            kind="internal",
            safe=True,
            run_internal=_run_revisions_gc,
        ),
        ActionSpec(
            name="olano_stale_images",
            description=(
                "Keeps the last good Olano image and drops the superseded ones — "
                "every update leaves a full previous image set behind (compose "
                "never removes what it replaced). Only images in repos Olano "
                "publishes are ever touched, never one a container uses, and "
                "never the sandbox image the runtime spawns on demand"
            ),
            kind="internal",
            safe=True,
            run_internal=_run_olano_stale_images,
        ),
        ActionSpec(
            name="docker_image_prune",
            description=(
                "docker image prune -f — removes dangling (untagged) images only; "
                "recovers the old copies left behind by image updates, never "
                "touches tagged images or containers"
            ),
            kind="command",
            safe=True,
            binary="docker",
            argv=("docker", "image", "prune", "-f"),
            timeout_seconds=600,
            needs_docker_daemon=True,
        ),
        ActionSpec(
            name="docker_prune",
            description=(
                "docker system prune -af — removes ALL unused images, stopped "
                "containers, networks and build cache. Registry images re-pull on "
                "next use, but a locally-tagged sandbox image with no container is "
                "gone until the next update re-tags it"
            ),
            kind="command",
            safe=False,
            binary="docker",
            argv=("docker", "system", "prune", "-af"),
            timeout_seconds=900,
            needs_docker_daemon=True,
        ),
        ActionSpec(
            name="npm_cache",
            description="npm cache clean --force — clears the npm download cache",
            kind="command",
            safe=True,
            binary="npm",
            argv=("npm", "cache", "clean", "--force"),
            timeout_seconds=300,
        ),
        ActionSpec(
            name="journald_vacuum",
            description="journalctl --vacuum-size — trims old systemd journal logs",
            kind="command",
            safe=True,
            binary="journalctl",
            argv=("journalctl", "--vacuum-size={vacuum_size}"),
            timeout_seconds=120,
            sudo_fallback=True,
        ),
    )
}

DEFAULT_ACTIONS: tuple[str, ...] = tuple(ACTION_REGISTRY)

# config/system_settings.py mirrors these names so config validation never has
# to import this (runtime) module; keep the two in sync (asserted in tests).
assert set(DISK_CLEANUP_SAFE_ACTIONS) == {s.name for s in ACTION_REGISTRY.values() if s.safe}


# ── results / snapshots ──────────────────────────────────────────────────────


@dataclass
class ActionResult:
    name: str
    status: str  # "ok" | "skipped" | "failed"
    reason: str | None = None
    freed_bytes: int = 0  # free-space delta on the monitored path (>= 0)
    reported: str | None = None  # the tool's own summary line, when it prints one
    used_sudo: bool = False
    duration_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
            "freed_bytes": self.freed_bytes,
            "freed_human": format_bytes(self.freed_bytes),
            "reported": self.reported,
            "used_sudo": self.used_sudo,
            "duration_seconds": round(self.duration_seconds, 2),
        }


@dataclass
class CleanupReport:
    started_at: str  # ISO UTC
    trigger: str  # "threshold" | "force_interval" | "manual"
    path: str
    percent_before: float
    percent_after: float
    freed_bytes_total: int
    results: list[ActionResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "trigger": self.trigger,
            "path": self.path,
            "percent_before": round(self.percent_before, 1),
            "percent_after": round(self.percent_after, 1),
            "freed_bytes_total": self.freed_bytes_total,
            "freed_human": format_bytes(self.freed_bytes_total),
            "results": [r.to_dict() for r in self.results],
        }

    def __str__(self) -> str:
        ran = [r for r in self.results if r.status == "ok"]
        skipped = [r for r in self.results if r.status == "skipped"]
        failed = [r for r in self.results if r.status == "failed"]
        parts = [
            f"✓ Disk cleanup ({self.trigger}): freed {format_bytes(self.freed_bytes_total)}",
            f"{self.percent_before:.1f}% → {self.percent_after:.1f}% used on {self.path}",
            f"{len(ran)} action(s) ran",
        ]
        if skipped:
            parts.append(f"{len(skipped)} skipped")
        if failed:
            parts.append(f"{len(failed)} FAILED ({', '.join(r.name for r in failed)})")
        return ", ".join(parts) + "."


@dataclass
class DiskSnapshot:
    path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent: float
    level: str  # "ok" | "warn" | "critical"
    checked_at: str  # ISO UTC

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "free_human": format_bytes(self.free_bytes),
            "total_human": format_bytes(self.total_bytes),
            "percent": round(self.percent, 1),
            "level": self.level,
            "checked_at": self.checked_at,
        }


def measure_disk(
    settings: DiskMonitorSettings,
    now: datetime,
    usage_fn: Callable[[str], Any] = shutil.disk_usage,
) -> DiskSnapshot:
    """One measurement of the configured path. May raise ``OSError``.

    ``percent`` is ``used / (used + free)`` — shutil's ``free`` is the space
    available to non-root, so this matches what ``df`` prints (root-reserved
    blocks count as used). Do not "fix" it to ``used / total``.
    """
    usage = usage_fn(settings.path)
    used = int(usage.used)
    free = int(usage.free)
    percent = (used / (used + free) * 100.0) if (used + free) > 0 else 0.0
    if percent >= settings.cleanup_percent:
        level = "critical"
    elif percent >= settings.warn_percent:
        level = "warn"
    else:
        level = "ok"
    return DiskSnapshot(
        path=settings.path,
        total_bytes=int(usage.total),
        used_bytes=used,
        free_bytes=free,
        percent=percent,
        level=level,
        checked_at=now.isoformat(),
    )


def docker_daemon_reachable() -> bool:
    """Can this process talk to a docker daemon?

    True when the standard socket exists (bind-mounted into the runtime
    container on the all-in-one deployment — that's what lets the prune actions
    work *inside* docker) or ``DOCKER_HOST`` points elsewhere. Deliberately not
    a ``docker info`` round-trip: this runs on dashboard/config read paths.
    """
    import os

    return Path("/var/run/docker.sock").exists() or bool(os.environ.get("DOCKER_HOST"))


def action_availability(
    settings: DiskMonitorSettings,
    *,
    which_fn: Callable[[str], str | None] = shutil.which,
    in_container_fn: Callable[[], bool] | None = None,
    docker_fn: Callable[[], bool] = docker_daemon_reachable,
) -> list[dict[str, Any]]:
    """Per-action availability for the dashboard/CLI/API — name, description,
    safe/aggressive flag, whether it's selected in config, and whether it can
    run on this host (binary present, daemon reachable, not container-blocked)."""
    in_container = _in_container(in_container_fn)
    rows: list[dict[str, Any]] = []
    for spec in ACTION_REGISTRY.values():
        available, reason = True, None
        if spec.kind == "command":
            if spec.skip_in_container and in_container:
                available, reason = False, "running in container"
            elif spec.binary and which_fn(spec.binary) is None:
                available, reason = False, f"{spec.binary} not installed"
            elif spec.needs_docker_daemon and not docker_fn():
                available, reason = False, "docker daemon not reachable"
        rows.append(
            {
                "name": spec.name,
                "description": spec.description,
                "safe": spec.safe,
                "selected": spec.name in settings.cleanup.actions,
                "available": available,
                "reason": reason,
            }
        )
    return rows


# ── persisted state (<home>/run/disk-monitor-state.json) ─────────────────────


def state_path(home: Path) -> Path:
    return home / "run" / "disk-monitor-state.json"


def default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "level": "ok",
        "last_alert_at": None,
        "last_cleanup_at": None,
        "last_forced_cleanup_at": None,
        "last_scan_at": None,
        "last_report": None,
    }


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                state = default_state()
                state.update(raw)
                return state
        except Exception:
            pass
    return default_state()


def save_state(path: Path, state: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        # A full disk must never crash the monitor that exists to fix it.
        logger.debug("disk monitor: could not persist state: %s", e)


def _minutes_since(now: datetime, iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds() / 60.0


def _in_container(in_container_fn: Callable[[], bool] | None) -> bool:
    if in_container_fn is None:
        from olano.security.sharing import is_running_in_container

        in_container_fn = is_running_in_container
    try:
        return bool(in_container_fn())
    except Exception:  # pragma: no cover - defensive
        return False


# ── executing cleanup actions ────────────────────────────────────────────────


def _extract_reported(*streams: str | None) -> str | None:
    for stream in streams:
        if not stream:
            continue
        match = _REPORTED_RE.search(stream)
        if match:
            return match.group(0).strip()
    return None


def _execute_command(
    spec: ActionSpec,
    settings: DiskMonitorSettings,
    which_fn: Callable[[str], str | None],
    run_fn: Callable[..., subprocess.CompletedProcess[str]],
    in_container: bool,
    docker_fn: Callable[[], bool] = docker_daemon_reachable,
) -> ActionResult:
    if spec.skip_in_container and in_container:
        return ActionResult(spec.name, "skipped", reason="running in container")
    if spec.binary and which_fn(spec.binary) is None:
        return ActionResult(spec.name, "skipped", reason=f"{spec.binary} not installed")
    if spec.needs_docker_daemon and not docker_fn():
        return ActionResult(spec.name, "skipped", reason="docker daemon not reachable")

    argv = [part.format(vacuum_size=settings.cleanup.journald_vacuum_size) for part in spec.argv]
    result = ActionResult(spec.name, "failed")
    try:
        proc = run_fn(argv, capture_output=True, text=True, timeout=spec.timeout_seconds)
        if proc.returncode != 0 and spec.sudo_fallback and which_fn("sudo") is not None:
            # e.g. journalctl vacuum needs root; sudo -n fails fast (no prompt)
            # when passwordless sudo isn't granted.
            proc = run_fn(
                ["sudo", "-n", *argv],
                capture_output=True,
                text=True,
                timeout=spec.timeout_seconds,
            )
            result.used_sudo = True
        result.reported = _extract_reported(proc.stdout, proc.stderr)
        if proc.returncode == 0:
            result.status = "ok"
        else:
            stderr_tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
            if _PERMISSION_RE.search(proc.stderr or ""):
                result.reason = "permission denied"
            else:
                result.reason = f"exit {proc.returncode}: {stderr_tail[0][:200]}"
    except subprocess.TimeoutExpired:
        result.reason = f"timeout after {spec.timeout_seconds}s"
    except FileNotFoundError:
        result.status, result.reason = "skipped", f"{spec.binary or argv[0]} not installed"
    except OSError as e:
        result.reason = str(e)[:200]
    return result


def run_cleanup(
    *,
    settings: DiskMonitorSettings,
    home: Path,
    trigger: str,
    now: datetime | None = None,
    usage_fn: Callable[[str], Any] = shutil.disk_usage,
    which_fn: Callable[[str], str | None] = shutil.which,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    in_container_fn: Callable[[], bool] | None = None,
    docker_fn: Callable[[], bool] = docker_daemon_reachable,
) -> CleanupReport:
    """Run the configured cleanup actions in registry order.

    Per-action try/except: one failure never stops the rest (housekeeping's
    per-rule convention). ``freed_bytes`` per action is the free-space delta on
    the monitored path — approximate under concurrent writes, and ≈0 when the
    tool frees space on another partition (docker's data-root elsewhere); the
    ``reported`` field keeps the tool's own number for those cases.
    """
    now = now or datetime.now(timezone.utc)
    in_container = _in_container(in_container_fn)

    def _percent() -> tuple[float, int]:
        try:
            usage = usage_fn(settings.path)
            used, free = int(usage.used), int(usage.free)
            pct = (used / (used + free) * 100.0) if (used + free) > 0 else 0.0
            return pct, free
        except OSError:
            return 0.0, 0

    percent_before, free_before = _percent()
    report = CleanupReport(
        started_at=now.isoformat(),
        trigger=trigger,
        path=settings.path,
        percent_before=percent_before,
        percent_after=percent_before,
        freed_bytes_total=0,
    )

    for name in settings.cleanup.actions:
        spec = ACTION_REGISTRY.get(name)
        if spec is None:  # settings validator prevents this; belt and braces
            continue
        started = time.monotonic()
        _, free_at_start = _percent()
        try:
            if spec.kind == "internal" and spec.run_internal is not None:
                summary = spec.run_internal(home)
                result = ActionResult(spec.name, "ok", reported=summary)
            else:
                result = _execute_command(spec, settings, which_fn, run_fn, in_container, docker_fn)
        except Exception as e:
            result = ActionResult(spec.name, "failed", reason=str(e)[:200])
        result.duration_seconds = time.monotonic() - started
        _, free_after_action = _percent()
        result.freed_bytes = max(0, free_after_action - free_at_start)
        report.results.append(result)
        if result.status == "failed":
            logger.warning("disk cleanup action %s failed: %s", result.name, result.reason)
        else:
            logger.info(
                "disk cleanup action %s: %s (%s)",
                result.name,
                result.status,
                result.reason or result.reported or format_bytes(result.freed_bytes),
            )

    report.percent_after, free_after = _percent()
    report.freed_bytes_total = max(0, free_after - free_before)
    # A cleanup is the one thing that deliberately moves these numbers, so the
    # memoised whole-disk attribution must not survive it — a user who presses
    # "Clean now" should see the images shrink, not a five-minute-old figure.
    try:
        from olano.runtime.disk_breakdown import invalidate_cache

        invalidate_cache()
    except Exception:  # pragma: no cover - cache hygiene must never fail a cleanup
        logger.debug("disk cleanup: could not invalidate the breakdown cache", exc_info=True)
    return report


# ── the tick ─────────────────────────────────────────────────────────────────


def disk_monitor_tick(
    *,
    settings: DiskMonitorSettings,
    state: dict[str, Any],
    now: datetime,
    home: Path,
    usage_fn: Callable[[str], Any] = shutil.disk_usage,
    which_fn: Callable[[str], str | None] = shutil.which,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    in_container_fn: Callable[[], bool] | None = None,
    docker_fn: Callable[[], bool] = docker_daemon_reachable,
    alert_fn: Callable[[str, dict[str, Any]], None] | None = None,
    cleanup_fn: Callable[..., CleanupReport | None] | None = None,
    scan_fn: Callable[[], bool] | None = None,
) -> tuple[dict[str, Any], DiskSnapshot | None, CleanupReport | None]:
    """One monitor cycle: measure → alert on transitions → maybe clean up →
    maybe start the daily directory-size scan.

    Pure-ish and fully injectable; returns ``(new_state, snapshot, report)`` —
    the caller persists the state. ``cleanup_fn`` exists so the server can wrap
    :func:`run_cleanup` with its concurrency gate (manual runs vs the loop); a
    ``None`` return from it means "someone else is cleaning" and leaves the
    cooldown stamps untouched. ``scan_fn`` starts a background scan and returns
    whether it actually started (``disk_scan.spawn_scan``); ``last_scan_at`` is
    only stamped on a real start, so a busy slot retries next tick.
    """
    state = dict(state)
    alert = alert_fn or (lambda _kind, _payload: None)
    do_cleanup = cleanup_fn or (
        lambda **kw: run_cleanup(
            usage_fn=usage_fn,
            which_fn=which_fn,
            run_fn=run_fn,
            in_container_fn=in_container_fn,
            docker_fn=docker_fn,
            **kw,
        )
    )

    try:
        snapshot = measure_disk(settings, now, usage_fn)
    except OSError as e:
        logger.debug("disk monitor: cannot measure %s: %s", settings.path, e)
        return state, None, None

    previous = str(state.get("level") or "ok")
    level = snapshot.level
    cleanup_cfg = settings.cleanup

    def _payload(message: str) -> dict[str, Any]:
        return {
            "level": level,
            "percent": round(snapshot.percent, 1),
            "free_bytes": snapshot.free_bytes,
            "free_human": format_bytes(snapshot.free_bytes),
            "total_bytes": snapshot.total_bytes,
            "path": snapshot.path,
            "message": message,
        }

    def _pressure_message() -> str:
        base = (
            f"Disk usage on {snapshot.path} at {snapshot.percent:.1f}% "
            f"({format_bytes(snapshot.free_bytes)} free)"
        )
        if level == "critical":
            if cleanup_cfg.enabled:
                return f"{base} — running automatic cleanup."
            return (
                f"{base} — automatic cleanup is disabled; enable it in "
                "Config → Disk monitor or run `olano disk --clean`."
            )
        return f"{base} — warn threshold is {settings.warn_percent}%."

    # ── alerts on transitions, re-alerts while stuck, one "resolved" note ──
    if _LEVEL_RANK[level] > _LEVEL_RANK.get(previous, 0):
        alert(level, _payload(_pressure_message()))
        state["last_alert_at"] = now.isoformat()
    elif level in ("warn", "critical"):
        since_alert = _minutes_since(now, state.get("last_alert_at"))
        if since_alert is None or since_alert >= REALERT_MINUTES:
            alert(level, _payload(_pressure_message()))
            state["last_alert_at"] = now.isoformat()
    elif previous in ("warn", "critical"):
        alert(
            "ok",
            _payload(f"Disk pressure resolved: {snapshot.percent:.1f}% used on {snapshot.path}."),
        )
        state["last_alert_at"] = None

    # ── cleanup decision ──
    report: CleanupReport | None = None
    if cleanup_cfg.enabled and cleanup_cfg.actions:
        since_cleanup = _minutes_since(now, state.get("last_cleanup_at"))
        cooldown_ok = since_cleanup is None or since_cleanup >= cleanup_cfg.cooldown_minutes

        threshold_due = level == "critical" and cooldown_ok

        force_due = False
        if cleanup_cfg.force_interval_days > 0:
            since_forced = _minutes_since(now, state.get("last_forced_cleanup_at"))
            if since_forced is None:
                # Start the clock on first observation — never surprise-clean
                # right when the setting (or the server) first comes up.
                state["last_forced_cleanup_at"] = now.isoformat()
            elif since_forced >= cleanup_cfg.force_interval_days * 24 * 60 and cooldown_ok:
                force_due = True

        if threshold_due or force_due:
            trigger = "threshold" if threshold_due else "force_interval"
            report = do_cleanup(settings=settings, home=home, trigger=trigger, now=now)
            if report is not None:
                state["last_cleanup_at"] = now.isoformat()
                if force_due:
                    state["last_forced_cleanup_at"] = now.isoformat()
                state["last_report"] = report.to_dict()
                logger.info(str(report))
                alert(
                    "cleanup",
                    {
                        "trigger": report.trigger,
                        "freed_bytes_total": report.freed_bytes_total,
                        "freed_human": format_bytes(report.freed_bytes_total),
                        "percent_before": round(report.percent_before, 1),
                        "percent_after": round(report.percent_after, 1),
                        "path": report.path,
                        "results": [r.to_dict() for r in report.results],
                        "message": str(report),
                    },
                )
                # A successful cleanup can resolve the pressure within the same
                # tick — recompute so the next tick doesn't fire a stale alert.
                try:
                    snapshot = measure_disk(settings, now, usage_fn)
                    level = snapshot.level
                    if level == "ok":
                        state["last_alert_at"] = None
                except OSError:  # pragma: no cover - defensive
                    pass

    # ── daily directory-size scan (see olano.runtime.disk_scan) ──
    scan_cfg = getattr(settings, "scan", None)
    if scan_fn is not None and scan_cfg is not None and scan_cfg.enabled:
        since_scan = _minutes_since(now, state.get("last_scan_at"))
        if since_scan is None or since_scan >= scan_cfg.interval_hours * 60:
            try:
                if scan_fn():
                    state["last_scan_at"] = now.isoformat()
            except Exception as e:  # pragma: no cover - scan must never break the tick
                logger.debug("disk monitor: could not start scan: %s", e)

    state["level"] = level
    state["version"] = STATE_VERSION
    return state, snapshot, report


# ── the runtime-held monitor object (mirrors admin.memory.MemoryMonitor) ─────


class DiskMonitor:
    """Live disk-monitor state held on the RuntimeServer.

    Carries the env kill-switch, the last snapshot/report for cheap reads (the
    dashboard card and ``/api/status``-adjacent surfaces must never trigger a
    filesystem syscall), and the cleanup concurrency gate shared by the
    background loop and the manual "clean now" endpoint.
    """

    def __init__(self, *, env_disabled: bool = False) -> None:
        self.env_disabled = bool(env_disabled)
        self.last_snapshot: dict[str, Any] | None = None
        self.last_report: dict[str, Any] | None = None
        self.cleanup_running = False
        self._gate = threading.Lock()

    def try_begin_cleanup(self) -> bool:
        """Atomically claim the single cleanup slot; False = already running."""
        with self._gate:
            if self.cleanup_running:
                return False
            self.cleanup_running = True
            return True

    def end_cleanup(self) -> None:
        with self._gate:
            self.cleanup_running = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "env_disabled": self.env_disabled,
            "cleanup_running": self.cleanup_running,
            "last_snapshot": self.last_snapshot,
            "last_report": self.last_report,
        }


def monitor_from_env() -> DiskMonitor:
    """Build the initial monitor; ``LIONCLAW_NO_DISK_MONITOR=1`` kills the loop
    for the whole process lifetime (config toggles handle everything else)."""
    import os

    return DiskMonitor(env_disabled=os.environ.get("LIONCLAW_NO_DISK_MONITOR") == "1")
