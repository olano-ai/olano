"""Per-customer delivery preferences: subscriber mute + operator block.

One tiny JSON file per customer folder (``customers/<platform>-<id>/.preferences.json``)
carries two independent flags:

- ``muted`` — set/cleared by the CUSTOMER through the identity-injected
  ``mute_my_notifications`` / ``resume_my_notifications`` tools (zero
  arguments, resolved from the authenticated request context like every other
  per-customer tool). Muted pauses every scheduled delivery to them — Type-1
  fan-out runs (turn and action mode) and their own Type-2 scheduled tasks —
  while live chat keeps working, so "resume" is always one message away.
  This is the platform-policy "user says stop and it actually stops" switch.
- ``blocked`` — set/cleared by the OPERATOR only (CLI/dashboard/API paths via
  :func:`set_blocked`); it is deliberately NOT a model tool, so no
  conversation can block or unblock anyone. Blocked stops scheduled work for
  that customer entirely.

Both checks are read where scheduled work fans out (``scheduler/multi_user``)
and again at the delivery boundary (``deliver_text``), so a future caller of
either path inherits the guarantee. ``forget_me`` erases the whole folder,
preferences included — a forgotten customer starts from a clean slate.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PREFS_FILENAME = ".preferences.json"


def read_prefs(folder: Path) -> dict:
    """Best-effort read of a customer folder's preferences ({} when absent/corrupt)."""
    target = Path(folder) / PREFS_FILENAME
    try:
        if target.is_file():
            data = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        logger.debug("Unreadable %s in %s", PREFS_FILENAME, folder, exc_info=True)
    return {}


def write_prefs(folder: Path, updates: dict) -> bool:
    """Merge *updates* into the folder's preferences file. Returns success."""
    folder = Path(folder)
    prefs = read_prefs(folder)
    prefs.update(updates)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / PREFS_FILENAME
        target.write_text(json.dumps(prefs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return True
    except OSError:
        logger.warning("Could not write %s in %s", PREFS_FILENAME, folder, exc_info=True)
        return False


def is_muted(folder: Path) -> bool:
    return bool(read_prefs(folder).get("muted"))


def is_blocked(folder: Path) -> bool:
    return bool(read_prefs(folder).get("blocked"))


def delivery_suppressed(folder: Path) -> str | None:
    """Reason scheduled work/delivery must be skipped for this folder, or None."""
    prefs = read_prefs(folder)
    if prefs.get("blocked"):
        return "blocked"
    if prefs.get("muted"):
        return "muted"
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _customer_folder(agent_name: str, customer_key: str) -> Path | None:
    """The folder a customer key names, or None when it names no customer.

    A key is one folder name directly under the agent's ``customers/`` root.
    Anything else (a path separator, ``..``, a hidden name) is refused before
    it reaches the filesystem: the key arrives from the dashboard API and the
    CLI, and ``customers_base / "../.."`` would otherwise write a
    preferences file outside the customer store.
    """
    from olano.memory.customer_memory import customers_base

    key = str(customer_key or "")
    if not key or key.startswith(".") or "/" in key or "\\" in key or "\x00" in key:
        return None
    folder = customers_base(agent_name) / key
    if not folder.is_dir() or folder.is_symlink():
        return None
    return folder


def _last_active(folder: Path) -> float | None:
    """Newest mtime among the folder and its memory files, best-effort."""
    newest: float | None = None
    for candidate in (folder, folder / "MEMORY.md", folder / "memories"):
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    return newest


def list_customers(agent_name: str) -> list[dict]:
    """Every end user a multi-user agent has a folder for, with their flags.

    One row per ``customers/<platform>-<id>/`` folder, sorted by key:
    ``key`` (the folder name, what :func:`set_blocked` takes), ``platform``
    and ``user_id`` (from the identity sidecar, else the folder name),
    ``blocked`` / ``muted`` and their timestamps, and ``last_active`` (epoch
    seconds, the newest write to the folder or its memory files). Read-only;
    an agent with no folder returns ``[]``.
    """
    from olano.memory.customer_memory import customers_base, load_customer_identity

    base = customers_base(agent_name)
    if not base.is_dir():
        return []
    rows: list[dict] = []
    for folder in sorted(base.iterdir(), key=lambda p: p.name):
        if folder.name.startswith(".") or folder.is_symlink() or not folder.is_dir():
            continue
        prefs = read_prefs(folder)
        identity = load_customer_identity(folder)
        rows.append(
            {
                "key": folder.name,
                "platform": identity[0] if identity else "",
                "user_id": identity[1] if identity else folder.name,
                "blocked": bool(prefs.get("blocked")),
                "muted": bool(prefs.get("muted")),
                "blocked_at": prefs.get("blocked_at") if prefs.get("blocked") else None,
                "muted_at": prefs.get("muted_at") if prefs.get("muted") else None,
                "last_active": _last_active(folder),
            }
        )
    return rows


def set_blocked(agent_name: str, customer_key: str, blocked: bool) -> bool:
    """Operator-side block/unblock of one customer folder by its key.

    Never exposed as a model tool. Returns False when the folder does not
    exist (blocking someone who never contacted the agent is a no-op, and
    silently creating a folder for them would invent a phantom customer) and
    when the key is not a plain folder name (see :func:`_customer_folder`).
    """
    folder = _customer_folder(agent_name, customer_key)
    if folder is None:
        return False
    updates: dict = {"blocked": bool(blocked)}
    updates["blocked_at" if blocked else "unblocked_at"] = _now()
    ok = write_prefs(folder, updates)
    if ok:
        logger.info(
            "Customer %s [agent=%s] %s by operator",
            customer_key,
            agent_name,
            "blocked" if blocked else "unblocked",
        )
    return ok


def make_customer_pref_tools(agent_name: str) -> list:
    """Identity-injected mute/resume tools for the CURRENT customer.

    Same contract as the per-customer memory tools: zero identity arguments,
    everything resolved from the authenticated request context, so there is
    nothing for a prompt injection to re-point. The blocked flag is read-only
    from here — no conversation can grant or lift a block.
    """
    from langchain_core.tools import tool

    from olano.memory.customer_memory import (
        active_customer_id,
        customers_base,
        record_customer_identity,
    )

    base = customers_base(agent_name)

    def _folder_for_active_customer() -> tuple[Path | None, str]:
        cid = active_customer_id()
        if not cid:
            return None, (
                "No identified customer in this conversation, so there are no "
                "notification settings to change."
            )
        folder = base / cid
        folder.mkdir(parents=True, exist_ok=True)
        record_customer_identity(folder)
        return folder, cid

    @tool
    def mute_my_notifications() -> str:
        """Pause ALL scheduled messages to the CURRENT customer (digests, alerts, their scheduled tasks).

        Use when they ask to stop, pause, unsubscribe from, or mute the
        agent's messages. Applies only to this one customer, takes effect
        immediately, and is fully reversible — live chat keeps working, and
        `resume_my_notifications` turns everything back on.
        """
        folder, err = _folder_for_active_customer()
        if folder is None:
            return err
        if not write_prefs(folder, {"muted": True, "muted_at": _now()}):
            return "Could not update notification settings — please try again."
        return (
            "Scheduled messages are now paused for this customer. They can say "
            "the word any time and resume_my_notifications turns them back on."
        )

    @tool
    def resume_my_notifications() -> str:
        """Turn scheduled messages back on for the CURRENT customer after a mute.

        The counterpart of `mute_my_notifications`; applies only to this one
        customer and takes effect from the next scheduled run.
        """
        folder, err = _folder_for_active_customer()
        if folder is None:
            return err
        if not write_prefs(folder, {"muted": False, "resumed_at": _now()}):
            return "Could not update notification settings — please try again."
        return "Scheduled messages are back on for this customer."

    return [mute_my_notifications, resume_my_notifications]
