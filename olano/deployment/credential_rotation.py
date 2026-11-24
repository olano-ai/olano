"""Bootstrap-credential rotation: local record + report to the cloud control plane.

Managed-cloud instances are provisioned with *temporary* credentials generated
by the control plane — the instance-admin sign-in password and the vault
passphrase — which cloud.olano.ai stores in plaintext so its console can offer
"copy temporary password" and the ready-email can include them. The moment the
owner replaces either one on the box (dashboard password change, vault rekey),
that control-plane copy is a dead credential: this module records the rotation
locally and reports it via ``POST {cloud}/api/v1/health`` (boolean fields
``admin_password_rotated`` / ``vault_password_rotated``) so the cloud purges
its plaintext copy and stops offering it.

Only the *fact* of rotation ever leaves the box — never the new secret. The
local state file is the durable source of truth: a report that cannot be
delivered (control plane unreachable, token missing) stays pending and is
retried — immediately after the rotating request via :func:`schedule_report`,
and periodically by :class:`RotationReporter` — so the signal converges after
outages instead of being lost. Posture mirrors
:mod:`olano.deployment.usage_reporter`: best-effort, never raises into the
runtime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from olano.deployment.sources import CloudConfig, _atomic_write, cloud_settings_config

logger = logging.getLogger(__name__)

STATE_FILE = "credential_rotations.json"
HEALTH_PATH = "/api/v1/health"
POST_TIMEOUT_SECONDS = 10.0

DEFAULT_INTERVAL_SECONDS = 15 * 60
MIN_INTERVAL_SECONDS = 60
INTERVAL_ENV = "LIONCLAW_ROTATION_REPORT_INTERVAL"
DISABLE_ENV = "LIONCLAW_ROTATION_REPORT"

# The sign-in the deployment was provisioned with (cloud_init bakes it into the
# runtime env). Only THIS account's password rotation invalidates the control
# plane's temporary admin password.
ADMIN_EMAIL_ENV = "LIONCLAW_ADMIN_EMAIL"

# Report flag → (rotated_at key, reported_at key) in the state file.
_FLAG_KEYS = {
    "admin_password_rotated": ("admin_password_rotated_at", "admin_password_reported_at"),
    "vault_password_rotated": ("vault_password_rotated_at", "vault_password_reported_at"),
}


def _now_iso() -> str:
    # Microsecond resolution: the ack in report_pending_rotations compares
    # rotated_at stamps to detect a rotation that landed mid-flight — two
    # rotations in the same second must not read as one.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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


def record_admin_password_rotation(home: str | Path | None = None) -> None:
    """Stamp "the deployment admin's sign-in password was replaced" (pending report)."""
    state = load_state(home)
    state["admin_password_rotated_at"] = _now_iso()
    state["admin_password_reported_at"] = None
    _write_state(state, home)


def record_vault_password_rotation(home: str | Path | None = None) -> None:
    """Stamp "the vault was rekeyed off its provisioned passphrase" (pending report)."""
    state = load_state(home)
    state["vault_password_rotated_at"] = _now_iso()
    state["vault_password_reported_at"] = None
    _write_state(state, home)


def admin_password_rotated(home: str | Path | None = None) -> bool:
    """Has the deployment admin's sign-in password been replaced?

    Recorded fact, not a live check: the box stamps this whenever the password
    is changed, and whenever an owner creates the first account themselves (the
    provisioned temporary password was never usable then, so there is nothing
    left to rotate). The onboarding wizard reads it to decide whether it still
    has a temporary password to walk the owner off.

    Independent of whether the cloud has acknowledged the report.
    """
    return bool(load_state(home).get("admin_password_rotated_at"))


def vault_password_rotated(home: str | Path | None = None) -> bool:
    """Has the vault been rekeyed off its provisioned passphrase?

    Same recorded-fact semantics as :func:`admin_password_rotated`. The
    onboarding wizard reads it to decide whether its profile step still has a
    vault half to show; a box whose owner rekeyed from Config → Vault before the
    tour ran is not asked twice.
    """
    return bool(load_state(home).get("vault_password_rotated_at"))


def pending_flags(state: dict[str, Any]) -> dict[str, bool]:
    """Which rotations are recorded but not yet acknowledged by the cloud."""
    return {
        flag: bool(state.get(rotated_key)) and not state.get(reported_key)
        for flag, (rotated_key, reported_key) in _FLAG_KEYS.items()
    }


def has_pending(home: str | Path | None = None) -> bool:
    return any(pending_flags(load_state(home)).values())


def is_deployment_admin_email(email: str | None, *, is_admin: bool = False) -> bool:
    """Does a password change for this account kill the temporary admin password?

    The control plane provisions exactly one sign-in (``LIONCLAW_ADMIN_EMAIL``)
    and keeps its temporary password — only that account's rotation matters.
    When the env is absent (manual installs, or a managed box that lost its
    env), any admin account counts: over-reporting merely purges the cloud copy
    early — strictly less plaintext exposure, never a lockout.
    """
    if not email:
        return False
    configured = os.environ.get(ADMIN_EMAIL_ENV, "").strip().lower()
    if configured:
        return email.strip().lower() == configured
    return bool(is_admin)


@dataclass
class ReportResult:
    """Outcome of one :func:`report_pending_rotations` pass (for logs/tests)."""

    reported: bool = False  # the cloud acknowledged the pending flags
    pending: bool = False  # something remains unreported (no config / error)
    error: str = ""


def report_pending_rotations(
    home: str | Path | None = None,
    *,
    config: CloudConfig | None = None,
    transport: httpx.BaseTransport | None = None,
) -> ReportResult:
    """Report pending rotation flags to the cloud health endpoint. Never raises.

    ``config``/``transport`` are injectable for tests; by default the cloud
    channel config (env/vault deployment token) and a real HTTP transport are
    used. Acknowledged flags are stamped ``*_reported_at`` so they are never
    re-sent — unless a NEW rotation lands mid-flight, which stays pending.
    """
    result = ReportResult()
    try:
        state = load_state(home)
        flags = pending_flags(state)
        payload = {flag: True for flag, on in flags.items() if on}
        if not payload:
            return result
        cfg = config if config is not None else cloud_settings_config()
        if cfg is None:
            result.pending = True
            return result

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
            result.pending = True
            result.error = f"network: {exc}"
            return result
        if resp.status_code in (401, 403):
            result.pending = True
            result.error = f"unauthorized (HTTP {resp.status_code})"
            logger.warning(
                "rotation-reporter: deployment token rejected (HTTP %d) — "
                "will retry; check the deployment token",
                resp.status_code,
            )
            return result
        if not (200 <= resp.status_code < 300):
            result.pending = True
            result.error = f"HTTP {resp.status_code}"
            return result

        # Acknowledge exactly what was sent: re-load and only stamp flags whose
        # rotated_at is unchanged, so a rotation recorded while the POST was in
        # flight is re-reported on the next pass.
        now = _now_iso()
        current = load_state(home)
        for flag in payload:
            rotated_key, reported_key = _FLAG_KEYS[flag]
            if current.get(rotated_key) == state.get(rotated_key):
                current[reported_key] = now
        _write_state(current, home)
        result.reported = True
        logger.info("rotation-reporter: reported %s", ", ".join(sorted(payload)))
    except Exception as exc:  # pragma: no cover - reporting must never break the runtime
        logger.exception("rotation-reporter: pass failed")
        result.pending = True
        result.error = f"{type(exc).__name__}: {exc}"
    return result


# Fire-and-forget tasks need a strong reference until done (else GC can drop a
# running task mid-flight).
_BG_TASKS: set[asyncio.Task[Any]] = set()


def schedule_report(home: str | Path | None = None) -> None:
    """Kick a best-effort report attempt without blocking the caller.

    Used by the rotating endpoints right after they stamp the state file, so
    the console updates within seconds instead of a reporter tick. Any failure
    leaves the flags pending for :class:`RotationReporter`.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        report_pending_rotations(home)  # sync context — attempt inline (never raises)
        return
    task = loop.create_task(asyncio.to_thread(report_pending_rotations, home))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


def reporting_enabled() -> bool:
    """True when a deployment token exists and the kill-switch is not set."""
    if os.environ.get(DISABLE_ENV, "").strip().lower() in ("0", "false", "no", "off"):
        return False
    return cloud_settings_config() is not None


class RotationReporter:
    """Periodically retries pending rotation reports.

    Lifecycle mirrors :class:`olano.deployment.usage_reporter.UsageReporter`
    (``start()``/``stop()``, one tracked asyncio task, never crashes the
    runtime). The first pass runs at start, so a rotation that happened while
    the control plane (or this process) was down converges on boot. An idle
    tick reads one small JSON file and sends nothing.
    """

    def __init__(self, report: Callable[[], ReportResult] | None = None) -> None:
        # Injectable for tests; the default reports the real state file.
        self._report = report if report is not None else report_pending_rotations
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="credential_rotation_reporter")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @staticmethod
    def _interval() -> float:
        raw = os.environ.get(INTERVAL_ENV, "").strip()
        seconds = float(raw) if raw.replace(".", "", 1).isdigit() else DEFAULT_INTERVAL_SECONDS
        seconds = max(seconds, MIN_INTERVAL_SECONDS)
        # Jitter so a fleet doesn't thundering-herd the health endpoint.
        return seconds * random.uniform(0.9, 1.1)

    def _tick(self) -> None:
        if reporting_enabled() and has_pending():
            self._report()

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self._tick)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.exception("rotation-reporter: tick failed")
            await asyncio.sleep(self._interval())
