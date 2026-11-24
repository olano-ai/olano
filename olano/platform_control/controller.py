"""Lifecycle controller behind the ``/platform`` command and REST endpoints.

Restart/shutdown of a live server is inherently mode-dependent:

- Supervised (systemd ``Restart=always``, docker ``restart: unless-stopped``):
  a graceful exit *is* a restart — the supervisor respawns the process. A
  true shutdown must go through the supervisor (``systemctl stop``); under
  docker it cannot be done from inside the container at all.
- Unsupervised (``olano server`` daemon, foreground ``olano serve``):
  restart re-execs the process in place (PID preserved, so the daemon's
  pidfile and log redirection stay valid); shutdown is a clean exit.

Every destructive action is **scheduled** with a short delay so the chat
reply that announced it can flush through the adapter before the process
goes away. A marker file records a pending restart so the freshly booted
process can send a "back online" notice to the chat that asked for it —
delivery waits for the runtime's ready signal (channels/platforms
connected), since adapters can't send until their ``connect()`` has run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from olano.platform_control.detect import (
    InstallMode,
    SupervisionMode,
    current_version,
    detect_install_mode,
    detect_supervision,
    source_commit,
    systemd_unit_name,
)

logger = logging.getLogger(__name__)

_MARKER_NAME = "platform_action.json"
# A restart marker older than this at *boot* is stale (e.g. the restart was
# triggered but the box came back much later for unrelated reasons) — don't
# announce. Judged against process start, not delivery time, so a slow
# channel connect can't turn a legitimate restart stale.
_MARKER_MAX_AGE = 600.0
# How long the "back online" announcer waits for the runtime to report that
# channels/platforms have connected before attempting delivery anyway (a
# single wedged adapter connect must not hold the notice hostage forever).
_READY_WAIT_TIMEOUT = 900.0
# Post-ready delivery retries: adapters can register connected-but-flaky
# (bridge reconnecting, Telegram network hiccup), so keep trying for a while.
_ANNOUNCE_MAX_ATTEMPTS = 30
_ANNOUNCE_RETRY_DELAY = 10.0
# How long channels get to flush the "restarting…" reply before the process
# exits / re-execs. Env-tunable for slow bridges.
_DEFAULT_ACTION_DELAY = 2.0
# How long a scheduled stop gets to finish before the exit watchdog forces the
# process out. Generous vs. stop()'s bounded phases (~10s) plus loop/thread
# teardown; env-tunable, <= 0 disables.
_DEFAULT_EXIT_WATCHDOG_TIMEOUT = 60.0


@dataclass(frozen=True)
class Notify:
    """Where to send lifecycle follow-ups ("update finished", "back online")."""

    agent: str = ""
    channel: str = ""
    target: str = ""  # chat id / recipient on that channel


_RUNTIME: Any = None
_STARTED_AT: float | None = None
_BOOT_WALLTIME: float | None = None
_ANNOUNCE_TASK: asyncio.Task[None] | None = None
_READY_EVENT: asyncio.Event | None = None


def bind_runtime(runtime: Any) -> None:
    """Attach the live RuntimeServer (called from ``RuntimeServer.start()``).

    Also schedules the post-restart "back online" announcement if the
    previous process left a restart marker behind. The announcement holds
    until :func:`notify_runtime_ready` fires (channels/platforms connected).
    """
    global _RUNTIME, _STARTED_AT, _BOOT_WALLTIME, _ANNOUNCE_TASK, _READY_EVENT
    _RUNTIME = runtime
    _STARTED_AT = time.monotonic()
    _BOOT_WALLTIME = time.time()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _READY_EVENT = asyncio.Event()
    _ANNOUNCE_TASK = loop.create_task(_announce_restart_if_marked())


def notify_runtime_ready() -> None:
    """Signal that boot finished (agents up, channels/platforms connected).

    Called from ``RuntimeServer.start()`` after the platform connect loop;
    releases the held "back online" notice. Safe to call when no runtime is
    bound and safe to call more than once.
    """
    if _READY_EVENT is not None:
        _READY_EVENT.set()


def get_runtime() -> Any:
    return _RUNTIME


def _action_delay() -> float:
    try:
        return float(os.environ.get("LIONCLAW_PLATFORM_ACTION_DELAY", ""))
    except (TypeError, ValueError):
        return _DEFAULT_ACTION_DELAY


def _marker_path() -> Path:
    from olano.config.home import get_olano_paths

    return get_olano_paths().run_dir / _MARKER_NAME


def _write_marker(action: str, notify: Notify | None) -> None:
    try:
        path = _marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "action": action,
                    "ts": time.time(),
                    "from_version": current_version(),
                    "notify": asdict(notify) if notify else None,
                }
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # pragma: no cover - marker is best-effort
        logger.debug("platform_control: could not write action marker: %s", exc)


async def send_followup(notify: Notify | None, text: str) -> bool:
    """Proactively deliver *text* to the chat recorded in *notify*.

    Routes via the platform registry (new adapters) first, then the channel
    registry (legacy channels, whose send methods take ``(recipient, text)``).
    """
    if notify is None or not (notify.agent and notify.channel and notify.target):
        return False
    try:
        from olano.platforms import get_channel_registry, get_platform_registry

        platform_reg = get_platform_registry()
        if platform_reg.has(notify.agent, notify.channel):
            adapter = platform_reg.get(notify.agent, notify.channel)
            if adapter is not None:
                result = await adapter.send(notify.target, text)
                ok = bool(getattr(result, "success", True))
                if not ok:
                    # Registered but couldn't deliver (e.g. "Not connected"
                    # before the adapter's connect()) — callers may retry.
                    logger.debug(
                        "platform_control: %s/%s send failed: %s",
                        notify.agent,
                        notify.channel,
                        getattr(result, "error", None) or "unknown error",
                    )
                return ok

        legacy: Any = get_channel_registry().get(notify.agent, notify.channel)
        if legacy is not None:
            if hasattr(legacy, "send_text"):
                await legacy.send_text(text, notify.target)
            else:
                # Legacy BaseChannel signature: send(recipient, text).
                await legacy.send(notify.target, text)
            return True
        logger.debug(
            "platform_control: no '%s' adapter registered for agent '%s' (yet)",
            notify.channel,
            notify.agent,
        )
    except Exception as exc:
        logger.warning(
            "platform_control: follow-up to %s/%s failed: %s",
            notify.agent,
            notify.channel,
            exc,
        )
    return False


async def _announce_restart_if_marked() -> None:
    path = _marker_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    if raw.get("action") != "restart":
        return
    try:
        marker_ts = float(raw.get("ts") or 0)
    except (TypeError, ValueError):
        return
    # Only consume a marker written by a PRIOR process. A marker whose ts is
    # at/after this process's boot time was written by a restart *this* process
    # just scheduled (and hasn't performed yet) — announcing it now would fire a
    # premature "back online". Leave it for the next boot to pick up.
    if _BOOT_WALLTIME is not None and marker_ts >= _BOOT_WALLTIME:
        return
    try:
        path.unlink()
    except OSError:
        pass
    # Staleness is judged against this process's boot, not delivery time: a
    # marker is stale when the box came back long after the restart was
    # scheduled, not when channels merely took a while to connect.
    boot_ref = _BOOT_WALLTIME if _BOOT_WALLTIME is not None else time.time()
    if boot_ref - marker_ts > _MARKER_MAX_AGE:
        return
    n = raw.get("notify") or {}
    notify = Notify(
        agent=str(n.get("agent") or ""),
        channel=str(n.get("channel") or ""),
        target=str(n.get("target") or ""),
    )
    if not (notify.agent and notify.channel and notify.target):
        return
    version = current_version()
    commit = source_commit()
    text = (
        f"✅ Olano restarted — v{version}" + (f" ({commit})" if commit else "") + ". Back online."
    )
    # Adapters can't deliver until their connect() has run, and that only
    # happens once every agent has booted — a post-update boot (cold imports,
    # startup upgrades, MCP discovery) routinely takes minutes. Wait for the
    # runtime's ready signal instead of guessing with a fixed sleep; fall back
    # to a deadline so one wedged adapter connect can't hold the notice
    # hostage when the others (including the target) may be fine.
    ready = _READY_EVENT
    if ready is not None and not ready.is_set():
        try:
            await asyncio.wait_for(ready.wait(), timeout=_READY_WAIT_TIMEOUT)
        except TimeoutError:
            logger.warning(
                "platform_control: runtime not ready %.0fs after boot — "
                "attempting to deliver the restart notice anyway.",
                _READY_WAIT_TIMEOUT,
            )
    for _ in range(_ANNOUNCE_MAX_ATTEMPTS):
        if await send_followup(notify, text):
            logger.info(
                "platform_control: restart notice delivered to %s/%s",
                notify.agent,
                notify.channel,
            )
            return
        await asyncio.sleep(_ANNOUNCE_RETRY_DELAY)
    logger.warning(
        "platform_control: gave up delivering the restart notice to %s/%s "
        "(target %s) after %d attempts — the channel never became sendable.",
        notify.agent,
        notify.channel,
        notify.target,
        _ANNOUNCE_MAX_ATTEMPTS,
    )


def _execv_self() -> None:
    """Replace this process with a fresh copy of itself (PID preserved)."""
    argv0 = sys.argv[0] or ""
    try:
        if argv0 and Path(argv0).name.startswith("olano") and os.access(argv0, os.X_OK):
            os.execv(argv0, [argv0, *sys.argv[1:]])
        os.execv(sys.executable, [sys.executable, "-m", "olano", *sys.argv[1:]])
    except OSError:
        logger.exception("platform_control: re-exec failed; process will exit instead")


def _exit_watchdog_timeout() -> float:
    try:
        return float(os.environ["LIONCLAW_EXIT_WATCHDOG_TIMEOUT"])
    except (KeyError, TypeError, ValueError):
        return _DEFAULT_EXIT_WATCHDOG_TIMEOUT


def _force_exit(post_action: Callable[[], None] | None) -> None:
    """Last-resort exit for a wedged teardown: re-exec if that was the plan
    (a successful ``os.execv`` never returns), else ``os._exit``. Exit code 1
    so ``Restart=on-failure`` supervisors respawn too."""
    if post_action is not None:
        try:
            post_action()
        except Exception:
            logger.exception("platform_control: watchdog post-action failed")
    os._exit(1)


def _arm_exit_watchdog(post_action: Callable[[], None] | None) -> None:
    """Guarantee a scheduled stop actually ends in a process exit.

    A chat-triggered restart on a supervised deployment only works if the
    process dies — the supervisor respawns it. Graceful teardown can wedge
    past every in-loop timeout: ``asyncio.run()``'s cleanup awaits cancelled
    tasks without a deadline, and interpreter exit joins non-daemon threads
    forever, so one misbehaving SDK task/thread leaves a zombie (unit
    "active", API port unbound, never respawned). A daemon thread survives
    all of that and forces the exit once the deadline passes; if the process
    exits cleanly first, the thread dies with it.
    """
    deadline = _exit_watchdog_timeout()
    if deadline <= 0:
        return

    def _watch() -> None:
        time.sleep(deadline)
        logger.error(
            "platform_control: still alive %.0fs after scheduled stop — "
            "forcing exit so the supervisor/re-exec can take over.",
            deadline,
        )
        _force_exit(post_action)

    threading.Thread(target=_watch, name="olano-exit-watchdog", daemon=True).start()


class PlatformController:
    """Mode-aware restart / shutdown / update orchestration."""

    def __init__(self) -> None:
        self._action_task: asyncio.Task[None] | None = None
        self._update_task: asyncio.Task[None] | None = None

    # ── introspection ─────────────────────────────────────────────────
    @property
    def supervision(self) -> SupervisionMode:
        return detect_supervision()

    @property
    def install_mode(self) -> InstallMode:
        return detect_install_mode()

    def status(self) -> dict[str, Any]:
        runtime = get_runtime()
        agents = sorted(getattr(runtime, "_runners", {}) or {}) if runtime is not None else []
        return {
            "version": current_version(),
            "commit": source_commit(),
            "supervision": self.supervision.value,
            "systemd_unit": systemd_unit_name(),
            "install_mode": self.install_mode.value,
            "uptime_seconds": (
                int(time.monotonic() - _STARTED_AT) if _STARTED_AT is not None else None
            ),
            "agents": agents,
            "update_in_progress": self.update_in_progress(),
        }

    async def dashboard_reachable(self) -> bool:
        """Probe the node dashboard's local control endpoint."""
        port = os.environ.get("LIONCLAW_DASHBOARD_PORT", "")
        if not port:
            try:
                from olano.config.home import load_home_config

                port = str(load_home_config().dashboard_port)
            except Exception:
                port = "3200"

        def _ping() -> bool:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/__control/ping", timeout=1.5
                ) as resp:
                    return bool(200 <= resp.status < 300)
            except Exception:
                return False

        return await asyncio.to_thread(_ping)

    # ── lifecycle guards ──────────────────────────────────────────────
    def can_control(self) -> bool:
        """True when a live RuntimeServer is bound (i.e. we run inside
        ``olano serve``). False in a one-shot CLI process, which has no
        server to restart."""
        return get_runtime() is not None

    def action_pending(self) -> bool:
        """A restart/shutdown has been scheduled and hasn't fired yet."""
        return self._action_task is not None and not self._action_task.done()

    def busy_reason(self) -> str | None:
        """Why a new lifecycle action can't start now (``None`` if it can)."""
        if self.update_in_progress():
            return "update"
        if self.action_pending():
            return "action"
        return None

    # ── restart ───────────────────────────────────────────────────────
    def schedule_restart(self, *, notify: Notify | None = None) -> str:
        """Schedule a full-process restart; returns the strategy used.

        ``"supervised-exit"`` — graceful exit, the supervisor respawns us.
        ``"execv"`` — graceful stop, then re-exec in place.
        ``"busy-update"`` / ``"busy-action"`` — refused: an update or another
        lifecycle action is already in flight (restarting now would orphan the
        git/uv subprocess or race a pending exit).
        """
        busy = self.busy_reason()
        if busy is not None:
            return f"busy-{busy}"
        return self._schedule_restart_now(notify=notify)

    def _schedule_restart_now(self, *, notify: Notify | None = None) -> str:
        """Restart without the busy guard — used by the post-update flow, which
        legitimately restarts *while* the update task is still finishing."""
        mode = self.supervision
        _write_marker("restart", notify)
        if mode in (SupervisionMode.SYSTEMD, SupervisionMode.DOCKER):
            self._action_task = asyncio.get_running_loop().create_task(
                self._delayed_stop(post_action=None)
            )
            return "supervised-exit"
        self._action_task = asyncio.get_running_loop().create_task(
            self._delayed_stop(post_action=_execv_self)
        )
        return "execv"

    # ── shutdown ──────────────────────────────────────────────────────
    def schedule_shutdown(self, *, notify: Notify | None = None) -> str:
        """Schedule a shutdown; returns the strategy used.

        ``"unsupported"`` — docker: exiting would just respawn the container,
        and the docker daemon can't be told to stop it from inside. Nothing
        is scheduled; the caller explains the host-side commands instead.
        ``"systemctl"`` — ask systemd to stop the unit (delayed; failures are
        reported as a follow-up message since the reply already went out).
        ``"exit"`` — clean process exit (daemon / foreground).
        ``"busy-update"`` / ``"busy-action"`` — refused, an action is in flight.
        """
        mode = self.supervision
        if mode is SupervisionMode.DOCKER:
            return "unsupported"
        busy = self.busy_reason()
        if busy is not None:
            return f"busy-{busy}"
        if mode is SupervisionMode.SYSTEMD:
            self._action_task = asyncio.get_running_loop().create_task(
                self._delayed_systemctl_stop(notify)
            )
            return "systemctl"
        self._action_task = asyncio.get_running_loop().create_task(
            self._delayed_stop(post_action=None)
        )
        return "exit"

    async def _delayed_stop(self, post_action: Any) -> None:
        await asyncio.sleep(_action_delay())
        runtime = get_runtime()
        event = getattr(runtime, "_shutdown_event", None)
        if runtime is not None:
            runtime._post_stop_action = post_action
        if event is not None:
            _arm_exit_watchdog(post_action)
            if not event.is_set():
                event.set()
            return
        # No live runtime to stop gracefully. This should be unreachable —
        # callers refuse destructive actions when ``can_control()`` is False —
        # but never blindly os._exit an unknown process from here.
        logger.warning("platform_control: no runtime bound; skipping stop")

    async def _delayed_systemctl_stop(self, notify: Notify | None) -> None:
        await asyncio.sleep(_action_delay())
        unit = systemd_unit_name() or "olano.service"
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl",
                "stop",
                "--no-block",
                unit,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            rc = proc.returncode
        except FileNotFoundError:
            rc, stderr = 127, b"systemctl not found"
        if rc != 0:
            detail = (stderr or b"").decode(errors="replace").strip()
            logger.warning("platform_control: systemctl stop %s failed (%s): %s", unit, rc, detail)
            await send_followup(
                notify,
                "⚠️ I couldn't stop the service myself"
                + (f" ({detail})" if detail else "")
                + f". Run this on the host instead: `sudo systemctl stop {unit}`",
            )
        # On success systemd delivers SIGTERM and the normal graceful
        # shutdown path takes over — nothing more to do here.

    # ── update ────────────────────────────────────────────────────────
    def update_in_progress(self) -> bool:
        return self._update_task is not None and not self._update_task.done()

    def begin_update(
        self, *, notify: Notify | None = None, force: bool = False, restart: bool = True
    ) -> bool:
        """Start the apply-update background task. False if an update or a
        pending restart/shutdown is already in flight.

        ``force`` re-syncs deps and rebuilds the dashboard unconditionally;
        ``restart`` is the default post-update behaviour (``/olano update
        --no-restart`` sets it False so the operator can restart when ready).
        """
        if self.update_in_progress() or self.action_pending():
            return False
        self._update_task = asyncio.get_running_loop().create_task(
            self._run_update(notify, force=force, restart=restart)
        )
        return True

    async def _run_update(
        self, notify: Notify | None, *, force: bool = False, restart: bool = True
    ) -> None:
        from olano.config.home import get_olano_home
        from olano.update.strategies import apply_update

        try:
            result = await asyncio.to_thread(apply_update, get_olano_home(), force=force)
        except Exception as exc:
            logger.exception("platform_control: update failed")
            await send_followup(notify, f"❌ Update failed: {exc}")
            return
        if not result.ok:
            tail = f"\n{result.log[-1200:]}" if result.log.strip() else ""
            await send_followup(notify, f"❌ Update failed: {result.summary}{tail}")
            return
        if restart and result.needs_restart:
            await send_followup(notify, f"✅ {result.summary} Restarting now…")
            # Bypass the busy guard: this update task is still "in progress",
            # but restarting is exactly the next step it owns.
            self._schedule_restart_now(notify=notify)
        else:
            hint = " (restart skipped — run `/olano restart` to load it.)" if not restart else ""
            await send_followup(notify, f"✅ {result.summary}{hint}")

    # ── dashboard rebuild ─────────────────────────────────────────────
    def begin_rebuild(self, *, notify: Notify | None = None) -> bool:
        """Start a dashboard-only rebuild background task (``/olano rebuild``).

        Shares the update slot so a rebuild and an update can't run at once.
        """
        if self.update_in_progress() or self.action_pending():
            return False
        self._update_task = asyncio.get_running_loop().create_task(self._run_rebuild(notify))
        return True

    async def _run_rebuild(self, notify: Notify | None) -> None:
        from olano.platform_control.detect import (
            detect_install_mode,
            detect_supervision,
            repo_root,
        )
        from olano.update.dashboard import describe_rebuild, rebuild_dashboard

        try:
            dash = await asyncio.to_thread(
                rebuild_dashboard, repo_root(), detect_install_mode(), detect_supervision()
            )
        except Exception as exc:
            logger.exception("platform_control: dashboard rebuild failed")
            await send_followup(notify, f"❌ Dashboard rebuild failed: {exc}")
            return
        prefix = "✅" if dash.ok else "⚠️"
        await send_followup(notify, f"{prefix} {describe_rebuild(dash)}")


_CONTROLLER: PlatformController | None = None


def get_platform_controller() -> PlatformController:
    global _CONTROLLER
    if _CONTROLLER is None:
        _CONTROLLER = PlatformController()
    return _CONTROLLER
