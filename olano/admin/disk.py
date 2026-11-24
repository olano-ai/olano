"""Disk diagnostics + the disk monitor/cleanup controls.

Report tiering mirrors :mod:`olano.admin.memory`: in-process runtime first
(live monitor state), then the daemon's API, then a local measurement — so
``olano disk`` gives a useful answer whether or not ``olano serve`` is up.

The ``on``/``off`` toggle flips **``disk_monitor.cleanup.enabled``** (whether
cleanup actions run automatically) — monitoring/alerts are governed separately
by ``disk_monitor.enabled`` in ``config.yaml`` and the
``LIONCLAW_NO_DISK_MONITOR=1`` boot kill-switch. ``run`` starts a manual
cleanup in a background thread (never blocks the caller; the runtime's single
cleanup slot prevents overlap with the monitor loop).
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

from olano.admin.diagnostics import _api_get_json, _get_runtime
from olano.admin.memory import _api_post_json
from olano.config.home import get_olano_home
from olano.config.system_settings import (
    load_disk_monitor_settings,
    set_disk_cleanup_enabled,
)
from olano.runtime import disk_monitor as dm

logger = logging.getLogger(__name__)

_MONITOR_ACTIONS = ("on", "off", "status", "run", "scan")


def _env_disabled() -> bool:
    return os.environ.get("LIONCLAW_NO_DISK_MONITOR") == "1"


def _local_report(source: str) -> dict[str, Any]:
    """Measure + settings + persisted state, no runtime required."""
    from olano.runtime import disk_breakdown, disk_scan

    settings = load_disk_monitor_settings()
    home = get_olano_home()
    out: dict[str, Any] = {
        "source": source,
        "settings": {**settings.model_dump(), "env_disabled": _env_disabled()},
        "state": dm.load_state(dm.state_path(home)),
        "actions": dm.action_availability(settings),
        "cleanup_running": False,
        "scan": disk_scan.load_snapshot(home),  # None until the first scan lands
        "scan_running": disk_scan.scan_running(),
        # Whole-disk attribution (Docker + home + swap + the system residual).
        # TTL-cached: this report is polled every few seconds by the dashboard
        # and `docker system df` sizes every local volume.
        "breakdown": disk_breakdown.cached_breakdown(path=settings.path, home=home),
    }
    try:
        out.update(dm.measure_disk(settings, datetime.now(timezone.utc)).to_dict())
    except OSError as e:
        out["error"] = f"cannot measure {settings.path}: {e}"
    return out


def disk_report() -> dict[str, Any]:
    """Disk usage + monitor state, from the best available source.

    ``source`` is ``runtime`` (in-process), ``api`` (running daemon), or
    ``local`` (no daemon — fresh measurement with the same settings the daemon
    would use).
    """
    rt = _get_runtime()
    if rt is not None:
        report = _local_report("runtime")
        mon = getattr(rt, "_disk_monitor", None)
        if mon is not None:
            report["cleanup_running"] = bool(mon.cleanup_running)
            report["monitor"] = mon.snapshot()
        return report
    res = _api_get_json("/api/diagnostics/disk", timeout=5.0)
    if isinstance(res, dict):
        res["source"] = "api"
        return res
    return _local_report("local")


def _status_payload(mon: Any) -> dict[str, Any]:
    return {
        **(mon.snapshot() if mon is not None else {}),
        "settings": {**load_disk_monitor_settings().model_dump(), "env_disabled": _env_disabled()},
    }


def _start_manual_cleanup(rt: Any) -> bool:
    """Kick a manual cleanup on a daemon thread; False = one already running."""
    mon = rt._disk_monitor
    if not mon.try_begin_cleanup():
        return False

    def _work() -> None:
        try:
            settings = load_disk_monitor_settings()
            home = get_olano_home()
            now = datetime.now(timezone.utc)
            report = dm.run_cleanup(settings=settings, home=home, trigger="manual", now=now)
            spath = dm.state_path(home)
            state = dm.load_state(spath)
            state["last_cleanup_at"] = now.isoformat()
            state["last_report"] = report.to_dict()
            try:
                snap = dm.measure_disk(settings, now)
                state["level"] = snap.level
                mon.last_snapshot = snap.to_dict()
            except OSError:
                pass
            dm.save_state(spath, state)
            mon.last_report = report.to_dict()
            logger.info(str(report))
            try:  # dashboard live update; never let SSE break the cleanup
                from olano.runtime.api import _push_event

                _push_event("disk_cleanup", {**report.to_dict(), "message": str(report)})
            except Exception:
                pass
        except Exception as e:
            logger.warning("manual disk cleanup failed: %s", e)
        finally:
            mon.end_cleanup()

    threading.Thread(target=_work, name="disk_cleanup_manual", daemon=True).start()
    return True


def apply_monitor_action(rt: Any, action: str) -> dict[str, Any]:
    """Apply ``action`` (``on``/``off``/``status``/``run``) against a live
    runtime. Used in-process by the API handler and the AgentFather tool."""
    action = (action or "status").strip().lower()
    mon = getattr(rt, "_disk_monitor", None)
    if mon is None:
        return {"ok": False, "error": "runtime has no disk monitor"}
    if action in ("on", "enable"):
        set_disk_cleanup_enabled(True)
    elif action in ("off", "disable"):
        set_disk_cleanup_enabled(False)
    elif action == "run":
        if not _start_manual_cleanup(rt):
            return {"ok": False, "error": "cleanup already running", **_status_payload(mon)}
        return {"ok": True, "running": True, **_status_payload(mon)}
    elif action == "scan":
        from olano.runtime import disk_breakdown, disk_scan

        # "Scan now" is the button a human presses when they want current
        # numbers, so drop the memoised whole-disk attribution too — otherwise
        # the Docker figures stay up to five minutes stale next to a scan the
        # user just watched finish.
        disk_breakdown.invalidate_cache()

        def _on_done(result: dict[str, Any]) -> None:
            try:  # dashboard live update; never let SSE break the scan
                from olano.runtime.api import _push_event

                _push_event("disk_scan", result)
            except Exception:
                pass

        settings = load_disk_monitor_settings()
        home = get_olano_home()
        started = disk_scan.spawn_scan(home, settings.scan, on_done=_on_done)
        if not started:
            return {"ok": False, "error": "scan already running", **_status_payload(mon)}
        # Stamp the daily schedule so the next monitor tick doesn't queue a
        # second walk right behind this manual one.
        spath = dm.state_path(home)
        state = dm.load_state(spath)
        state["last_scan_at"] = datetime.now(timezone.utc).isoformat()
        dm.save_state(spath, state)
        return {"ok": True, "scanning": True, **_status_payload(mon)}
    elif action != "status":
        return {"ok": False, "error": f"unknown action: {action}", **_status_payload(mon)}
    return {"ok": True, **_status_payload(mon)}


def set_disk_monitor(action: str = "status") -> dict[str, Any]:
    """Toggle automatic cleanup / read state / start a manual cleanup.

    In-process this drives the live runtime directly; from a separate CLI
    process it POSTs to the running daemon. With no daemon at all, ``on``/
    ``off`` still work (they persist to ``config.yaml``, picked up at next
    boot) and ``status`` reads local state; ``run`` needs the daemon —
    ``olano disk --clean`` falls back to running the cleanup itself.
    """
    action = (action or "status").strip().lower()
    rt = _get_runtime()
    if rt is not None:
        return apply_monitor_action(rt, action)
    res = _api_post_json("/api/diagnostics/disk/monitor", {"action": action})
    if isinstance(res, dict):
        return res
    if action in ("on", "enable", "off", "disable"):
        set_disk_cleanup_enabled(action in ("on", "enable"))
        return {"ok": True, "daemon": False, **_status_payload(None)}
    if action == "status":
        return {"ok": True, "daemon": False, **_status_payload(None)}
    return {"ok": False, "error": "no live runtime and daemon API unreachable"}


def render_disk_report_text(report: dict[str, Any]) -> str:
    """Human-readable block for the CLI / chat."""
    lines: list[str] = []
    settings = report.get("settings") or {}
    cleanup = settings.get("cleanup") or {}
    level = str(report.get("level") or "?").upper()
    if report.get("percent") is not None:
        lines.append(
            f"Disk {report.get('path', settings.get('path', '/'))}: "
            f"{report.get('percent')}% used · {report.get('free_human', '?')} free "
            f"of {report.get('total_human', '?')}   [{level}]"
        )
    elif report.get("error"):
        lines.append(f"Disk: {report['error']}")
    lines.append(
        f"  thresholds: warn {settings.get('warn_percent', '?')}% · "
        f"auto-clean {settings.get('cleanup_percent', '?')}% · "
        f"check every {settings.get('check_interval_minutes', '?')} min"
    )
    monitoring = "off"
    if settings.get("env_disabled"):
        monitoring = "off (LIONCLAW_NO_DISK_MONITOR=1)"
    elif settings.get("enabled"):
        monitoring = "on"
    auto = "on" if cleanup.get("enabled") else "off"
    if report.get("cleanup_running"):
        auto += " · CLEANUP RUNNING NOW"
    lines.append(f"  monitoring: {monitoring} · automatic cleanup: {auto}")
    actions = report.get("actions") or []
    if actions:
        parts = []
        for a in actions:
            mark = "✓" if a.get("selected") else "—"
            note = "" if a.get("safe") else " (aggressive)"
            if not a.get("available", True):
                note += f" [{a.get('reason')}]"
            parts.append(f"{a.get('name')} {mark}{note}")
        lines.append("  actions: " + " · ".join(parts))
    from olano.runtime import disk_breakdown

    lines.extend(disk_breakdown.render_text(report.get("breakdown")))
    scan = report.get("scan") or {}
    entries = scan.get("entries") or []
    if entries:
        top = [e for e in entries if e.get("depth") == 1][:8]
        lines.append(
            f"  largest under {scan.get('root', '~/.olano')} "
            f"(scanned {scan.get('scanned_at', '?')}"
            + (", truncated" if scan.get("truncated") else "")
            + "):"
        )
        for e in top:
            lines.append(f"    - {e.get('path')}: {dm.format_bytes(int(e.get('bytes') or 0))}")
    if report.get("scan_running"):
        lines.append("  directory scan: RUNNING NOW")
    state = report.get("state") or {}
    last = state.get("last_report") or report.get("monitor", {}).get("last_report") or None
    if last:
        lines.append(
            f"  last cleanup: {last.get('started_at', '?')} ({last.get('trigger', '?')}) "
            f"freed {last.get('freed_human', '?')} "
            f"({last.get('percent_before', '?')}% → {last.get('percent_after', '?')}%)"
        )
        for r in last.get("results") or []:
            detail = r.get("reported") or r.get("reason") or r.get("freed_human")
            lines.append(f"    - {r.get('name')}: {r.get('status')} · {detail}")
    elif state.get("last_cleanup_at"):
        lines.append(f"  last cleanup: {state['last_cleanup_at']}")
    else:
        lines.append("  last cleanup: never")
    src = report.get("source")
    if src and src != "runtime":
        lines.append(f"  source: {src}")
    return "\n".join(lines)
