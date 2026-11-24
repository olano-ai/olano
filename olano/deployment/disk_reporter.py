"""Daily disk report to the cloud control plane.

Ships a compact "where the bytes are" summary to ``POST {cloud}/api/v1/health``
in the ``metrics.disk`` field — the same authenticated channel the
credential-rotation reporter uses — so cloud.olano.ai can show a per-directory
breakdown under the Deployments → Machine disk meter without any host-side
agent:

- filesystem totals for the monitored path (``shutil.disk_usage``),
- the latest directory-size scan snapshot (top buckets only — see
  :mod:`olano.runtime.disk_scan`),
- ``docker system df`` totals via the bind-mounted socket when available
  (images/containers/volumes/build-cache size + reclaimable) — this is the
  "are old update images piling up?" number,
- the whole-disk breakdown (:mod:`olano.runtime.disk_breakdown`) — Docker,
  the olano home, swap and the labelled system residual, adding up to the used
  space, so the console can answer "what is the other 13 GB?" without SSH.

Posture mirrors :mod:`olano.deployment.credential_rotation`: best-effort,
never raises into the runtime, jittered interval, at most one report per
``REPORT_EVERY_SECONDS`` (stamped in ``<home>/deployment/disk_report_state.json``
so restarts don't re-send).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from olano.deployment.sources import CloudConfig, _atomic_write, cloud_settings_config

# The docker-df collection lives with the rest of the disk-attribution code
# (:mod:`olano.runtime.disk_breakdown`) so the dashboard and this reporter can
# never disagree about what "images: 5.9 GB" means. Re-exported under the names
# this module has always used.
from olano.runtime.disk_breakdown import collect_docker_df, parse_docker_size

__all__ = [
    "DiskReporter",
    "build_disk_metrics",
    "collect_docker_df",
    "parse_docker_size",
    "report_disk_metrics",
    "reporting_enabled",
]

logger = logging.getLogger(__name__)

STATE_FILE = "disk_report_state.json"
HEALTH_PATH = "/api/v1/health"
POST_TIMEOUT_SECONDS = 15.0

REPORT_EVERY_SECONDS = 24 * 60 * 60  # one report per day
CHECK_INTERVAL_SECONDS = 60 * 60  # due-ness re-checked hourly
MIN_INTERVAL_SECONDS = 60
INTERVAL_ENV = "LIONCLAW_DISK_REPORT_INTERVAL"  # overrides REPORT_EVERY_SECONDS
DISABLE_ENV = "LIONCLAW_DISK_REPORT"

# Keep the cloud payload small: top-level + one sublevel is what the console
# renders; the full snapshot stays on the box.
MAX_SCAN_ENTRIES = 40
MAX_SCAN_DEPTH = 2


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path(home: str | Path | None = None) -> Path:
    from olano.config.home import get_olano_paths

    d = get_olano_paths(home).deployment_dir
    d.mkdir(parents=True, exist_ok=True)
    return d / STATE_FILE


def load_state(home: str | Path | None = None) -> dict[str, Any]:
    try:
        raw = json.loads(_state_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_state(state: dict[str, Any], home: str | Path | None = None) -> None:
    _atomic_write(_state_path(home), json.dumps(state, indent=2) + "\n")


def build_disk_metrics(home: str | Path | None = None) -> dict[str, Any]:
    """The ``metrics.disk`` payload. Cheap: one statvfs, one small JSON read,
    one ``docker system df`` (skipped when docker isn't reachable)."""
    from olano.config.home import get_olano_home
    from olano.config.system_settings import load_disk_monitor_settings
    from olano.runtime import disk_breakdown, disk_scan
    from olano.runtime import disk_monitor as dm

    home_path = get_olano_home() if home is None else Path(home)
    settings = load_disk_monitor_settings()
    payload: dict[str, Any] = {"generated_at": _now_iso()}

    try:
        snap = dm.measure_disk(settings, datetime.now(timezone.utc))
        payload["fs"] = {
            "path": snap.path,
            "total_bytes": snap.total_bytes,
            "used_bytes": snap.used_bytes,
            "free_bytes": snap.free_bytes,
            "percent": round(snap.percent, 1),
            "level": snap.level,
        }
    except OSError as e:
        payload["fs"] = {"error": str(e)[:200]}

    scan = disk_scan.load_snapshot(home_path)
    if scan:
        entries = [
            e
            for e in scan.get("entries", [])
            if isinstance(e, dict) and int(e.get("depth") or 0) <= MAX_SCAN_DEPTH
        ][:MAX_SCAN_ENTRIES]
        payload["scan"] = {
            "root": scan.get("root"),
            "scanned_at": scan.get("scanned_at"),
            "duration_seconds": scan.get("duration_seconds"),
            "total_bytes": scan.get("total_bytes"),
            "total_files": scan.get("total_files"),
            "truncated": bool(scan.get("truncated")),
            "entries": entries,
        }

    docker = collect_docker_df()
    if docker is not None:
        payload["docker"] = docker

    # The attributed view of the same numbers (Docker + home + swap + the
    # labelled system residual). Cached behind its own TTL, so the daily report
    # normally reuses whatever the dashboard already computed rather than
    # re-running `docker system df`.
    breakdown = disk_breakdown.cached_breakdown(path=settings.path, home=home_path)
    if breakdown is not None:
        payload["breakdown"] = breakdown
    return payload


def report_disk_metrics(
    home: str | Path | None = None,
    *,
    config: CloudConfig | None = None,
    transport: httpx.BaseTransport | None = None,
) -> bool:
    """Build + POST one disk report; stamp the state file on success.

    Never raises. False = not sent (no config, network error, HTTP error) —
    the next due tick simply retries.
    """
    try:
        cfg = config if config is not None else cloud_settings_config()
        if cfg is None:
            return False
        payload: dict[str, Any] = {"metrics": {"disk": build_disk_metrics(home)}}
        # Ride along with the daily report so a box whose boot-time report was
        # lost (control plane down, network blip) refills the field without
        # needing a restart. Omitted entirely when unknown — reporting a
        # placeholder is the bug `deployment/version.py` exists to prevent.
        from olano.deployment.version import build_sha as _build_sha
        from olano.deployment.version import runtime_version as _runtime_version

        version = _runtime_version()
        if version:
            payload["runtime_version"] = version
        # The canonical commit identity (deployments.reported_commit) rides
        # along too — same daily self-heal, same omit-when-unknown rule.
        sha = _build_sha()
        if sha:
            payload["build_sha"] = sha
        # And the release name (deployments.reported_version), same rules.
        from olano.deployment.version import package_version as _package_version

        pkg = _package_version()
        if pkg:
            payload["package_version"] = pkg
        client_kwargs: dict[str, Any] = {"timeout": POST_TIMEOUT_SECONDS}
        if transport is not None:
            client_kwargs["transport"] = transport
        try:
            with httpx.Client(**client_kwargs) as client:
                resp = client.post(
                    f"{cfg.url}{HEALTH_PATH}",
                    headers={"Authorization": f"Bearer {cfg.token}"},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            logger.debug("disk-reporter: network error: %s", exc)
            return False
        if not (200 <= resp.status_code < 300):
            logger.debug("disk-reporter: HTTP %s", resp.status_code)
            return False
        state = load_state(home)
        state["last_reported_at"] = _now_iso()
        _write_state(state, home)
        logger.info("disk-reporter: reported disk metrics to the control plane")
        return True
    except Exception:  # pragma: no cover - reporting must never break the runtime
        logger.exception("disk-reporter: pass failed")
        return False


def _seconds_since(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


def report_due(home: str | Path | None = None) -> bool:
    since = _seconds_since(load_state(home).get("last_reported_at"))
    return since is None or since >= _report_every()


def _report_every() -> float:
    raw = os.environ.get(INTERVAL_ENV, "").strip()
    seconds = float(raw) if raw.replace(".", "", 1).isdigit() else REPORT_EVERY_SECONDS
    return max(seconds, MIN_INTERVAL_SECONDS)


def reporting_enabled() -> bool:
    """True when a deployment token exists and the kill-switch is not set."""
    if os.environ.get(DISABLE_ENV, "").strip().lower() in ("0", "false", "no", "off"):
        return False
    return cloud_settings_config() is not None


class DiskReporter:
    """Hourly due-ness check, one report per day (jittered).

    Lifecycle mirrors :class:`olano.deployment.credential_rotation.RotationReporter`:
    ``start()``/``stop()``, one tracked asyncio task, never crashes the runtime.
    The first check runs shortly after start so a fresh deployment shows up in
    the console the same day, not tomorrow.
    """

    def __init__(self, report: Callable[[], bool] | None = None) -> None:
        # Injectable for tests; the default builds + posts the real payload.
        self._report = report if report is not None else report_disk_metrics
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="disk_reporter")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @staticmethod
    def _interval() -> float:
        check = min(CHECK_INTERVAL_SECONDS, _report_every())
        # Jitter so a fleet doesn't thundering-herd the health endpoint.
        return max(MIN_INTERVAL_SECONDS, check) * random.uniform(0.9, 1.1)

    def _tick(self) -> None:
        if reporting_enabled() and report_due():
            self._report()

    async def _loop(self) -> None:
        # Late first pass: give boot (and a possible first disk scan) a moment.
        await asyncio.sleep(120 * random.uniform(0.9, 1.1))
        while True:
            try:
                await asyncio.to_thread(self._tick)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.exception("disk-reporter: tick failed")
            await asyncio.sleep(self._interval())
