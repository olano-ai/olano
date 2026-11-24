"""Operator notifications — the things that need a human, in one place.

The runtime already knows when something needs attention: a connector that
couldn't start because its credential is missing, a vault that is still locked
after boot, a disk crossing its threshold, an agent that keeps erroring. Until
now each of those went to the log, where nobody looks, so the only visible
symptom was a bot that silently stopped replying.

This module is the store behind the dashboard's bell and the Overview
Notifications section. It is deliberately small:

* **In-process, persisted to ``<home>/run/notifications.json``** so a restart
  doesn't wipe the list — these outlive the process that raised them.
* **Deduplicated by ``dedupe_key``** — a boot loop raising the same missing
  credential every 30s must show one row with an updated timestamp, not 200.
* **Capped** at :data:`MAX_NOTIFICATIONS`, oldest read ones dropped first.
* **Best-effort SSE** — a ``notifications_changed`` frame is pushed when the
  dashboard is running, and skipped entirely when it is not (CLI, tests). A
  notification must never fail the thing that raised it.

Producers call :func:`notify`. Everything else (list/read/dismiss) is the
dashboard's REST surface in ``runtime/api.py``.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

Level = Literal["info", "success", "warn", "error"]

# Ordered worst-first: the bell's tone is the worst unread level it holds.
_LEVEL_RANK: dict[str, int] = {"error": 3, "warn": 2, "success": 1, "info": 0}

MAX_NOTIFICATIONS = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Notification:
    """One thing the operator may need to act on."""

    id: str
    kind: str
    level: Level
    title: str
    body: str = ""
    agent: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    read_at: str | None = None
    dedupe_key: str | None = None
    # Optional {"label": ..., "href": ...} — a dashboard route to fix it.
    action: dict[str, str] | None = None
    # How many times a deduplicated producer re-raised this.
    count: int = 1

    @property
    def read(self) -> bool:
        return self.read_at is not None

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "read": self.read}


# A producer that learns to attach a link only helps rows raised *after* it
# does: notifications outlive the process that raised them, and an operator's
# bell can already hold weeks of them with ``action: None`` on disk. Where the
# link is recoverable from the row itself, rebuild it on load — otherwise the
# fix reads as broken to exactly the person who asked for it.
_ISSUE_ASSIGNED_KEY = re.compile(r"^issue-(\d+)-assign-")


def _restored_action(row: Notification) -> dict[str, str] | None:
    """Rebuild the ``action`` of a pre-link row, or ``None`` if it can't be."""
    if row.kind != "issue_assigned":
        return None
    match = _ISSUE_ASSIGNED_KEY.match(row.dedupe_key or "")
    if match is None:
        return None
    from olano.issues.delivery import issue_action

    return issue_action(match.group(1))


class _Store:
    """Thread-safe notification list with write-through persistence.

    Producers run on the event loop, in worker threads (disk monitor) and in
    background tasks alike, so every mutation takes the lock. Persistence is
    best-effort: a read-only or full disk degrades to in-memory, it never raises
    into the caller.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: list[Notification] = []
        self._loaded = False

    # ── persistence ──────────────────────────────────────────────────────────

    def _path(self) -> Path:
        from olano.config.home import get_olano_paths

        return get_olano_paths().run_dir / "notifications.json"

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            path = self._path()
            if not path.exists():
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("Could not read the notification store", exc_info=True)
            return
        items: list[Notification] = []
        for row in raw if isinstance(raw, list) else []:
            if not isinstance(row, dict):
                continue
            row.pop("read", None)  # derived property, not a field
            try:
                item = Notification(**row)
            except TypeError:
                continue  # a row from a newer/older shape — skip it, don't crash
            if item.action is None:
                item.action = _restored_action(item)
            items.append(item)
        self._items = items

    def _persist(self) -> None:
        try:
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps([asdict(n) for n in self._items], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        except Exception:
            logger.debug("Could not persist the notification store", exc_info=True)

    # ── mutations ────────────────────────────────────────────────────────────

    def add(self, notification: Notification) -> Notification:
        with self._lock:
            self._load()
            existing = None
            if notification.dedupe_key:
                existing = next(
                    (n for n in self._items if n.dedupe_key == notification.dedupe_key), None
                )
            if existing is not None:
                # Same problem, still unfixed: refresh it in place and pull it
                # back to unread so a recurrence is not silently swallowed by an
                # earlier dismissal.
                existing.level = notification.level
                existing.title = notification.title
                existing.body = notification.body
                existing.action = notification.action
                existing.updated_at = notification.updated_at
                existing.read_at = None
                existing.count += 1
                result = existing
            else:
                self._items.insert(0, notification)
                result = notification
            self._trim()
            self._persist()
        return result

    def _trim(self) -> None:
        """Drop the oldest read rows once over the cap; unread always survives."""
        if len(self._items) <= MAX_NOTIFICATIONS:
            return
        keep_unread = [n for n in self._items if not n.read]
        read = [n for n in self._items if n.read]
        room = max(0, MAX_NOTIFICATIONS - len(keep_unread))
        kept_read = set(id(n) for n in read[:room])
        self._items = [n for n in self._items if not n.read or id(n) in kept_read]

    def mark_read(self, ids: list[str] | None = None) -> int:
        with self._lock:
            self._load()
            targets = [n for n in self._items if (ids is None or n.id in ids) and not n.read]
            stamp = _now()
            for n in targets:
                n.read_at = stamp
            if targets:
                self._persist()
        return len(targets)

    def dismiss(self, notification_id: str) -> bool:
        with self._lock:
            self._load()
            before = len(self._items)
            self._items = [n for n in self._items if n.id != notification_id]
            changed = len(self._items) != before
            if changed:
                self._persist()
        return changed

    def clear(self, *, read_only: bool = True) -> int:
        with self._lock:
            self._load()
            before = len(self._items)
            self._items = [n for n in self._items if not n.read] if read_only else []
            removed = before - len(self._items)
            if removed:
                self._persist()
        return removed

    def resolve(self, dedupe_key: str) -> bool:
        """Drop a notification whose underlying problem is fixed.

        Producers of recoverable conditions (a connector that later boots fine)
        call this so the bell reflects the world, not the history.
        """
        with self._lock:
            self._load()
            before = len(self._items)
            self._items = [n for n in self._items if n.dedupe_key != dedupe_key]
            changed = len(self._items) != before
            if changed:
                self._persist()
        return changed

    def resolve_where(self, predicate: Callable[[Notification], bool]) -> int:
        """Drop every notification *predicate* matches; returns how many."""
        with self._lock:
            self._load()
            before = len(self._items)
            self._items = [n for n in self._items if not predicate(n)]
            removed = before - len(self._items)
            if removed:
                self._persist()
        return removed

    # ── reads ────────────────────────────────────────────────────────────────

    def list(self, *, include_read: bool = True, limit: int = 100) -> list[Notification]:
        with self._lock:
            self._load()
            items = self._items if include_read else [n for n in self._items if not n.read]
            return list(items[:limit])

    def summary(self) -> dict[str, Any]:
        with self._lock:
            self._load()
            unread = [n for n in self._items if not n.read]
            worst = max((_LEVEL_RANK.get(n.level, 0) for n in unread), default=-1)
            level = next((k for k, v in _LEVEL_RANK.items() if v == worst), None)
            return {
                "unread": len(unread),
                "total": len(self._items),
                "level": level,
            }


_store = _Store()


def _broadcast() -> None:
    """Push a ``notifications_changed`` frame, if a dashboard is serving.

    Imports nothing: ``runtime.api`` is only touched when it is already loaded,
    so a CLI process never drags FastAPI in just to raise a notification.
    """
    api = sys.modules.get("olano.runtime.api")
    if api is None:
        return
    try:
        api._push_event("notifications_changed", _store.summary())
    except Exception:  # pragma: no cover - SSE is best-effort by design
        logger.debug("Could not broadcast a notifications_changed frame", exc_info=True)


def notify(
    *,
    kind: str,
    title: str,
    level: Level = "info",
    body: str = "",
    agent: str | None = None,
    dedupe_key: str | None = None,
    action: dict[str, str] | None = None,
) -> Notification:
    """Raise an operator notification. Never raises.

    ``dedupe_key`` collapses repeats of the same underlying problem into one
    row — pass a stable key (``f"channel_credential_missing:{agent}:{channel}"``)
    for anything a retry loop can re-raise.
    """
    notification = Notification(
        id=uuid.uuid4().hex[:12],
        kind=kind,
        level=level,
        title=title,
        body=body,
        agent=agent,
        dedupe_key=dedupe_key,
        action=action,
    )
    stored = _store.add(notification)
    logger.debug("notification[%s] %s: %s", level, kind, title)
    _broadcast()
    # Only a new row reaches admins' phones: a dedupe refresh is the same
    # problem recurring, and a retry loop would otherwise page every 30s.
    if stored is notification:
        try:
            from olano.push import events as push_events

            push_events.operator_alert(notification=stored)
        except Exception:
            logger.debug("notification push failed", exc_info=True)
    return stored


def resolve(dedupe_key: str) -> bool:
    """Clear a notification whose condition no longer holds."""
    changed = _store.resolve(dedupe_key)
    if changed:
        _broadcast()
    return changed


def resolve_where(predicate: Callable[[Notification], bool]) -> int:
    """Clear every notification *predicate* matches, for producers whose
    condition is a set (one agent's servers) rather than one key."""
    removed = _store.resolve_where(predicate)
    if removed:
        _broadcast()
    return removed


def forget_agent(agent: str) -> int:
    """Clear every notification about *agent*: it was deleted, so nothing on
    the bell about it can still be fixed. Never raises."""
    try:
        return resolve_where(lambda n: n.agent == agent)
    except Exception:  # pragma: no cover - a delete must not fail over the bell
        logger.debug("Could not clear notifications for '%s'", agent, exc_info=True)
        return 0


def list_notifications(*, include_read: bool = True, limit: int = 100) -> list[dict[str, Any]]:
    return [n.to_dict() for n in _store.list(include_read=include_read, limit=limit)]


def mark_read(ids: list[str] | None = None) -> int:
    count = _store.mark_read(ids)
    if count:
        _broadcast()
    return count


def dismiss(notification_id: str) -> bool:
    changed = _store.dismiss(notification_id)
    if changed:
        _broadcast()
    return changed


def clear(*, read_only: bool = True) -> int:
    removed = _store.clear(read_only=read_only)
    if removed:
        _broadcast()
    return removed


def summary() -> dict[str, Any]:
    """``{"unread": n, "total": n, "level": worst-unread-level|None}``."""
    return _store.summary()


def reset_for_tests() -> None:
    """Drop every notification and re-read from disk on next use."""
    with _store._lock:
        _store._items = []
        _store._loaded = False
