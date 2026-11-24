"""Persistent, owner-scoped A2A task store with retention.

a2a-sdk's ``InMemoryTaskStore`` is a dictionary that never evicts: every inbound
call leaves its ``Task`` (status, message history, artifact) in process memory
for the life of the runtime, and a caller's ``tasks/get`` after a restart fails
for every id it holds. This store keeps the same records in SQLite under
``<home>/a2a/tasks.db`` — scoped by owner exactly as the SDK store is (the
owner is the verified token's user name, via the SDK's ``OwnerResolver``, so one
caller can never read another's tasks) — and deletes rows older than
``external_access.a2a_task_retention_days`` on a throttled sweep.

Only the A2A envelope lives here. The conversation itself is in the agent's
thread (checkpointer) and transcript, and the Agent to Agent view keeps its own
activity log, so retention bounds memory and disk without losing the record of
what was said.

Stdlib ``sqlite3`` behind ``asyncio.to_thread``, deliberately: it is loop-agnostic
(the server is rebuilt on settings changes and tests run one loop per test),
every operation is a single short statement, and it matches how the rest of the
home stores its rows (issues, checkpoints).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from a2a.server.owner_resolver import OwnerResolver, resolve_user_scope
from a2a.server.tasks.task_store import TaskStore
from a2a.types import a2a_pb2
from a2a.types.a2a_pb2 import Task
from a2a.utils.constants import DEFAULT_LIST_TASKS_PAGE_SIZE
from a2a.utils.errors import InvalidParamsError
from a2a.utils.task import decode_page_token, encode_page_token

if TYPE_CHECKING:
    from a2a.server.context import ServerCallContext

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 7
SWEEP_INTERVAL_SECONDS = 600.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    owner      TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    context_id TEXT NOT NULL DEFAULT '',
    state      INTEGER NOT NULL DEFAULT 0,
    status_ts  TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    payload    BLOB NOT NULL,
    PRIMARY KEY (owner, task_id)
);
CREATE INDEX IF NOT EXISTS tasks_updated_at ON tasks (updated_at);
"""

_UPSERT = """
INSERT INTO tasks (owner, task_id, context_id, state, status_ts, updated_at, payload)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(owner, task_id) DO UPDATE SET
    context_id = excluded.context_id,
    state      = excluded.state,
    status_ts  = excluded.status_ts,
    updated_at = excluded.updated_at,
    payload    = excluded.payload
"""


def default_task_store_path() -> Path:
    from olano.config.home import get_olano_paths

    return get_olano_paths().root / "a2a" / "tasks.db"


def _status_fields(task: Task) -> tuple[int, str]:
    """``(state, status timestamp as RFC 3339 or '')`` — the columns ``list`` sorts by."""
    if not task.HasField("status"):
        return 0, ""
    ts = task.status.timestamp.ToJsonString() if task.status.HasField("timestamp") else ""
    return int(task.status.state), ts


class SqliteTaskStore(TaskStore):
    """``TaskStore`` over one SQLite file, owner-scoped, with age-based retention.

    ``retention_days`` may be a callable so the live setting is read on every
    sweep — the operator changes it in Config → External access and the next
    sweep honours it without a restart.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        retention_days: int | Callable[[], int] = DEFAULT_RETENTION_DAYS,
        owner_resolver: OwnerResolver = resolve_user_scope,
        sweep_interval_seconds: float = SWEEP_INTERVAL_SECONDS,
    ) -> None:
        self.path = Path(path) if path is not None else default_task_store_path()
        self._retention = retention_days
        self._owner_resolver = owner_resolver
        self._sweep_interval = sweep_interval_seconds
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._last_sweep = 0.0  # monotonic; 0 ⇒ the first write sweeps

    # ── connection ─────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        """Open lazily; the caller holds ``self._lock``."""
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self.path), timeout=30, check_same_thread=False, isolation_level=None
            )
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.executescript(_SCHEMA)
            try:
                self.path.chmod(0o600)
            except OSError:  # pragma: no cover - exotic filesystems
                pass
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def retention_days(self) -> int:
        value = self._retention() if callable(self._retention) else self._retention
        try:
            return max(1, int(value))
        except (TypeError, ValueError):  # pragma: no cover - settings are clamped upstream
            return DEFAULT_RETENTION_DAYS

    # ── sync primitives (run in a worker thread) ────────────────────────────
    def _save_sync(self, owner: str, task: Task) -> None:
        state, status_ts = _status_fields(task)
        with self._lock:
            self._connect().execute(
                _UPSERT,
                (
                    owner,
                    task.id,
                    task.context_id,
                    state,
                    status_ts,
                    time.time(),
                    task.SerializeToString(),
                ),
            )
            self._maybe_sweep_locked()

    def _get_sync(self, owner: str, task_id: str) -> Task | None:
        with self._lock:
            row = (
                self._connect()
                .execute(
                    "SELECT payload FROM tasks WHERE owner = ? AND task_id = ?", (owner, task_id)
                )
                .fetchone()
            )
        if row is None:
            return None
        task: Task = Task.FromString(bytes(row[0]))
        return task

    def _owner_tasks_sync(self, owner: str) -> list[Task]:
        with self._lock:
            rows = (
                self._connect()
                .execute("SELECT payload FROM tasks WHERE owner = ?", (owner,))
                .fetchall()
            )
        return [Task.FromString(bytes(r[0])) for r in rows]

    def _delete_sync(self, owner: str, task_id: str) -> bool:
        with self._lock:
            cur = self._connect().execute(
                "DELETE FROM tasks WHERE owner = ? AND task_id = ?", (owner, task_id)
            )
            return (cur.rowcount or 0) > 0

    def _count_sync(self) -> int:
        with self._lock:
            row = self._connect().execute("SELECT COUNT(*) FROM tasks").fetchone()
        return int(row[0]) if row else 0

    def _sweep_locked(self, now: float | None = None) -> int:
        cutoff = (time.time() if now is None else now) - self.retention_days() * 86400
        cur = self._connect().execute("DELETE FROM tasks WHERE updated_at < ?", (cutoff,))
        self._last_sweep = time.monotonic()
        return int(cur.rowcount or 0)

    def _maybe_sweep_locked(self) -> None:
        if time.monotonic() - self._last_sweep < self._sweep_interval:
            return
        removed = self._sweep_locked()
        if removed:
            logger.info(
                "A2A task store: swept %d task(s) older than %d day(s)",
                removed,
                self.retention_days(),
            )

    def _sweep_sync(self, now: float | None = None) -> int:
        with self._lock:
            return self._sweep_locked(now)

    # ── TaskStore protocol ─────────────────────────────────────────────────
    async def save(self, task: Task, context: ServerCallContext) -> None:
        await asyncio.to_thread(self._save_sync, self._owner_resolver(context), task)

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        return await asyncio.to_thread(self._get_sync, self._owner_resolver(context), task_id)

    async def list(
        self, params: a2a_pb2.ListTasksRequest, context: ServerCallContext
    ) -> a2a_pb2.ListTasksResponse:
        tasks = await asyncio.to_thread(self._owner_tasks_sync, self._owner_resolver(context))
        return _paginate(_filter_and_sort(tasks, params), params)

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        owner = self._owner_resolver(context)
        if not await asyncio.to_thread(self._delete_sync, owner, task_id):
            logger.warning("A2A task store: delete of unknown task %s for owner %s", task_id, owner)

    # ── maintenance ────────────────────────────────────────────────────────
    async def sweep(self, now: float | None = None) -> int:
        """Delete every task older than the retention window; returns the count."""
        return await asyncio.to_thread(self._sweep_sync, now)

    async def count(self) -> int:
        return await asyncio.to_thread(self._count_sync)


# ── list semantics, kept identical to the SDK's in-memory store ────────────────


def _sort_key(task: Task) -> tuple[bool, str, str]:
    has_ts = task.HasField("status") and task.status.HasField("timestamp")
    return (has_ts, task.status.timestamp.ToJsonString() if has_ts else "", task.id)


def _filter_and_sort(tasks: list[Task], params: a2a_pb2.ListTasksRequest) -> list[Task]:
    if params.context_id:
        tasks = [t for t in tasks if t.context_id == params.context_id]
    if params.status:
        tasks = [t for t in tasks if t.status.state == params.status]
    if params.HasField("status_timestamp_after"):
        after = params.status_timestamp_after.ToJsonString()
        tasks = [
            t
            for t in tasks
            if t.HasField("status")
            and t.status.HasField("timestamp")
            and t.status.timestamp.ToJsonString() >= after
        ]
    # Newest first; ids break timestamp ties so paging is stable.
    return sorted(tasks, key=_sort_key, reverse=True)


def _paginate(tasks: list[Task], params: a2a_pb2.ListTasksRequest) -> a2a_pb2.ListTasksResponse:
    total = len(tasks)
    start = 0
    if params.page_token:
        wanted = decode_page_token(params.page_token)
        for i, task in enumerate(tasks):
            if task.id == wanted:
                start = i
                break
        else:
            raise InvalidParamsError(f"Invalid page token: {params.page_token}")
    page_size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
    end = start + page_size
    next_token: Any = encode_page_token(tasks[end].id) if end < total else None
    return a2a_pb2.ListTasksResponse(
        next_page_token=next_token,
        tasks=tasks[start:end],
        total_size=total,
        page_size=page_size,
    )


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "SqliteTaskStore",
    "default_task_store_path",
]
