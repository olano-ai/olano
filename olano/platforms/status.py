"""Machine-local lock primitives for platform adapters.

Adapters use ``acquire_scoped_lock`` to prevent two olano processes
from fighting over the same external identity (e.g. the same Telegram
bot token, the same Slack team's socket-mode connection). This matters
when users run multiple ``LIONCLAW_HOME`` directories on one machine.

Ported from hermes-agent/gateway/status.py, stripped down to just the
lock APIs the adapters import.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

_LOCKS_DIRNAME = "platform-locks"
_IS_WINDOWS = sys.platform == "win32"


def _get_lock_dir() -> Path:
    """Return the machine-local directory for token-scoped platform locks."""
    override = os.getenv("LIONCLAW_PLATFORM_LOCK_DIR")
    if override:
        return Path(override)
    state_home = Path(os.getenv("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "olano" / _LOCKS_DIRNAME


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scope_hash(identity: str) -> str:
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _get_scope_lock_path(scope: str, identity: str) -> Path:
    return _get_lock_dir() / f"{scope}-{_scope_hash(identity)}.lock"


def _get_process_start_time(pid: int) -> int | None:
    """Return the kernel start time for a process when available (Linux only)."""
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        return int(stat_path.read_text().split()[21])
    except (FileNotFoundError, IndexError, PermissionError, ValueError, OSError):
        return None


def _build_pid_record() -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "start_time": _get_process_start_time(os.getpid()),
    }


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_json_file(path: Path, record: dict[str, Any]) -> None:
    path.write_text(json.dumps(record), encoding="utf-8")


def terminate_pid(pid: int, *, force: bool = False) -> None:
    """Terminate a PID with platform-appropriate force semantics."""
    if force and _IS_WINDOWS:
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            os.kill(pid, signal.SIGTERM)
            return
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            raise OSError(details or f"taskkill failed for PID {pid}")
        return

    sig = signal.SIGTERM if not force else getattr(signal, "SIGKILL", signal.SIGTERM)
    os.kill(pid, sig)


def acquire_scoped_lock(
    scope: str,
    identity: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Acquire a machine-local lock keyed by ``scope`` + ``identity``.

    Used to prevent two local adapters from using the same external
    identity at once (same Telegram bot token across different
    ``LIONCLAW_HOME`` directories, for instance).

    Returns ``(True, prior_record_or_None)`` on success, or
    ``(False, holding_record)`` if another live process already holds it.
    """
    lock_path = _get_scope_lock_path(scope, identity)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        **_build_pid_record(),
        "scope": scope,
        "identity_hash": _scope_hash(identity),
        "metadata": metadata or {},
        "updated_at": _utc_now_iso(),
    }

    existing = _read_json_file(lock_path)
    if existing is None and lock_path.exists():
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
    if existing:
        try:
            existing_pid = int(existing["pid"])
        except (KeyError, TypeError, ValueError):
            existing_pid = None

        if existing_pid == os.getpid() and existing.get("start_time") == record.get("start_time"):
            _write_json_file(lock_path, record)
            return True, existing

        stale = existing_pid is None
        if not stale and existing_pid is not None:
            try:
                os.kill(existing_pid, 0)
            except (ProcessLookupError, PermissionError):
                stale = True
            else:
                current_start = _get_process_start_time(existing_pid)
                if (
                    existing.get("start_time") is not None
                    and current_start is not None
                    and current_start != existing.get("start_time")
                ):
                    stale = True
                if not stale:
                    try:
                        proc_status = Path(f"/proc/{existing_pid}/status")
                        if proc_status.exists():
                            for line in proc_status.read_text().splitlines():
                                if line.startswith("State:"):
                                    state = line.split()[1]
                                    if state in ("T", "t"):
                                        stale = True
                                    break
                    except (OSError, PermissionError):
                        pass
        if stale:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            return False, existing

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False, _read_json_file(lock_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
    except Exception:
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return True, None


def release_scoped_lock(scope: str, identity: str) -> None:
    """Release a scoped lock when owned by the current process."""
    lock_path = _get_scope_lock_path(scope, identity)
    existing = _read_json_file(lock_path)
    if not existing:
        return
    if existing.get("pid") != os.getpid():
        return
    if existing.get("start_time") != _get_process_start_time(os.getpid()):
        return
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


def release_all_scoped_locks() -> int:
    """Remove every scoped lock file (used during ``--replace`` takeover)."""
    lock_dir = _get_lock_dir()
    removed = 0
    if lock_dir.exists():
        for lock_file in lock_dir.glob("*.lock"):
            try:
                lock_file.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
    return removed


def write_runtime_status(data: dict[str, Any]) -> None:  # pragma: no cover - stub
    """No-op placeholder; the full implementation belongs to the gateway daemon."""


def read_runtime_status() -> dict[str, Any] | None:  # pragma: no cover - stub
    return None
