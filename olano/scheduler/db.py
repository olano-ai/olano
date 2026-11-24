"""
SQLite-backed scheduler database.

Stores tasks with optional cron schedules, one-time run times, execution history,
and float32 embedding blobs for semantic search over task prompts.
"""

from __future__ import annotations

import logging
import pickle
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)

_CREATE_TASKS = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    schedule    TEXT,           -- cron expression (NULL = one-time)
    run_at      TEXT,           -- ISO datetime for one-time tasks
    timezone    TEXT DEFAULT 'UTC',
    enabled     INTEGER DEFAULT 1,
    agent_name  TEXT,
    created_at  TEXT NOT NULL,
    last_run    TEXT,
    next_run    TEXT,
    run_count   INTEGER DEFAULT 0,
    embedding   BLOB,           -- pickle of float32 numpy array
    visibility  TEXT DEFAULT 'private',
    created_by  TEXT,
    platform    TEXT,           -- multi-user: channel the owning customer arrived on
    user_id     TEXT,           -- multi-user: RAW sender id (delivery address)
    model       TEXT,           -- provider:model this task runs on (NULL = agent default)
    reasoning_effort TEXT       -- effort level for this task's runs (NULL = agent default)
)
"""
# `model` pins the run to one model for THIS task only — a cheap tier for a
# nightly digest, a bigger one for the weekly analysis — without touching the
# agent's configured model. NULL/empty means the agent's own, which is every
# task that predates the column. The scheduler hands it to
# `AgentRunner.ainvoke(override_model=...)`; a model that cannot be built fails
# the run with a history error naming it, never a silent fallback.
# `reasoning_effort` is its sibling: the per-turn effort override the chat's
# effort chip sets, persisted per task and validated at every write against
# the ladder of the model the task runs on (scheduler/overrides.py). NULL/empty
# means the agent's own setting; a stale value is dropped by the model builder
# with a log line rather than failing the run.
# platform/user_id are NULL on every task that is not customer-owned — which is
# every task a normal (single-user) agent has, so nothing about the existing
# scheduling path changes shape. When both are set, the task belongs to ONE
# authenticated end-user of a multi-user agent: the scheduler binds this
# identity for the run (so identity-injected tools resolve that customer, not a
# phantom), and `user_id` is the raw platform address because the turn's reply
# is delivered back to it. The sanitized folder key is derived, never stored —
# `sanitize_identity(user_id)` is deterministic.

_CREATE_HISTORY = """
CREATE TABLE IF NOT EXISTS task_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    ran_at      TEXT NOT NULL,
    result      TEXT,
    error       TEXT
)
"""

# Schema init is a write transaction (CREATE TABLE + ALTERs), and SchedulerDB is
# instantiated per request/event throughout the runtime — so it runs once per
# resolved db path per process. Keyed by path so tests' fresh tmp DBs (and any
# explicit db_path) still initialize.
_INITIALIZED_PATHS: set[str] = set()
_INIT_LOCK = threading.Lock()


class SchedulerDB:
    """
    SQLite-backed task store with semantic search via numpy cosine similarity.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or get_olano_paths().scheduler_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ----------------------------------------------------------------- init

    def _init_db(self) -> None:
        key = str(self.db_path.resolve())
        with _INIT_LOCK:
            if key in _INITIALIZED_PATHS:
                return
            with self._conn() as conn:
                conn.execute(_CREATE_TASKS)
                conn.execute(_CREATE_HISTORY)
                # Migrate existing tables that predate visibility/created_by
                # (and, later, the multi-user platform/user_id) columns.
                for col, defn in [
                    ("visibility", "TEXT DEFAULT 'private'"),
                    ("created_by", "TEXT"),
                    ("platform", "TEXT"),
                    ("user_id", "TEXT"),
                    ("model", "TEXT"),
                    ("reasoning_effort", "TEXT"),
                ]:
                    try:
                        conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {defn}")
                    except sqlite3.OperationalError:
                        pass  # column already exists
                conn.commit()
            # Recorded only after a successful commit so a failed init retries.
            _INITIALIZED_PATHS.add(key)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    # ----------------------------------------------------------------- CRUD

    def create_task(
        self,
        name: str,
        prompt: str,
        schedule: str | None = None,
        run_at: str | None = None,
        timezone: str = "UTC",
        agent_name: str | None = None,
        embedding: np.ndarray | None = None,
        visibility: str = "private",
        created_by: str | None = None,
        platform: str | None = None,
        user_id: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """Create a new task and return its ID.

        ``platform`` + ``user_id`` (always both or neither) mark the task as
        owned by one end-user of a multi-user agent; see the schema comment.
        ``model`` pins the run to one ``provider:model``; empty/None keeps the
        agent's configured model. ``reasoning_effort`` pins the effort level of
        that model's runs; empty/None keeps the agent's own setting.
        """
        task_id = str(uuid.uuid4())
        created_at = datetime.now(tz=__import__("datetime").timezone.utc).isoformat()
        emb_blob = pickle.dumps(embedding) if embedding is not None else None

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO tasks (id, name, prompt, schedule, run_at, timezone,
                                   enabled, agent_name, created_at, embedding,
                                   visibility, created_by, platform, user_id, model,
                                   reasoning_effort)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    name,
                    prompt,
                    schedule,
                    run_at,
                    timezone,
                    agent_name,
                    created_at,
                    emb_blob,
                    visibility,
                    created_by,
                    platform,
                    user_id,
                    (model or "").strip() or None,
                    (reasoning_effort or "").strip().lower() or None,
                ),
            )
            conn.commit()

        logger.debug("Created task %s: %s", task_id, name)
        return task_id

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def list_tasks(
        self,
        enabled_only: bool = False,
        agent_name: str | None = None,
        caller: str | None = None,
    ) -> list[dict[str, Any]]:
        """List tasks. If caller is set, returns only tasks visible to that agent:
        tasks they created (private or shared) plus shared tasks from others."""
        query = "SELECT * FROM tasks"
        params: list[Any] = []
        conditions = []
        if enabled_only:
            conditions.append("enabled = 1")
        if agent_name:
            conditions.append("agent_name = ?")
            params.append(agent_name)
        if caller:
            conditions.append("(created_by = ? OR visibility = 'shared')")
            params.append(caller)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def update_task(self, task_id: str, **kwargs: Any) -> bool:
        allowed = {
            "name",
            "prompt",
            "schedule",
            "run_at",
            "timezone",
            "enabled",
            "agent_name",
            "last_run",
            "next_run",
            "run_count",
            "visibility",
            "model",
            "reasoning_effort",
        }
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return False
        if "model" in fields:
            # "" and None both mean "back to the agent's configured model".
            fields["model"] = (str(fields["model"] or "")).strip() or None
        if "reasoning_effort" in fields:
            # Same convention: "" and None both mean the agent's own setting.
            fields["reasoning_effort"] = (
                str(fields["reasoning_effort"] or "").strip().lower() or None
            )

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [task_id]

        with self._conn() as conn:
            cur = conn.execute(f"UPDATE tasks SET {set_clause} WHERE id = ?", values)
            conn.commit()
        return cur.rowcount > 0

    def delete_task(self, task_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------- multi-user (customer) tasks

    def list_user_tasks(
        self,
        agent_name: str,
        platform: str,
        user_id: str,
        *,
        enabled_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Tasks owned by one end-user of one multi-user agent.

        The identity filter is exact on all three keys — the same triple the
        identity-injected tools resolve from the request context — so one
        customer can never enumerate (or count against) another's jobs.
        """
        query = "SELECT * FROM tasks WHERE agent_name = ? AND platform = ? AND user_id = ?"
        params: list[Any] = [agent_name, platform, user_id]
        if enabled_only:
            query += " AND enabled = 1"
        query += " ORDER BY created_at DESC"
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def count_user_tasks(self, agent_name: str, platform: str, user_id: str) -> int:
        """Enabled tasks one end-user currently has on one agent (cap check)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE agent_name = ? AND platform = ? "
                "AND user_id = ? AND enabled = 1",
                (agent_name, platform, user_id),
            ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def delete_user_tasks(self, agent_name: str, platform: str, user_id: str) -> list[str]:
        """Delete ALL of one end-user's tasks on one agent; returns deleted ids.

        The ids come back so the caller can also drop the live APScheduler jobs
        — a forget_me that removes rows but leaves registered triggers would
        keep firing until the next restart.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM tasks WHERE agent_name = ? AND platform = ? AND user_id = ?",
                (agent_name, platform, user_id),
            ).fetchall()
            ids = [str(r[0]) for r in rows]
            if ids:
                conn.executemany("DELETE FROM tasks WHERE id = ?", [(i,) for i in ids])
                conn.commit()
        return ids

    # ----------------------------------------------------------------- history

    def add_history(
        self,
        task_id: str,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        ran_at = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO task_history (task_id, ran_at, result, error) VALUES (?, ?, ?, ?)",
                (task_id, ran_at, result, error),
            )
            conn.execute(
                "UPDATE tasks SET last_run = ?, run_count = run_count + 1 WHERE id = ?",
                (ran_at, task_id),
            )
            conn.commit()

    def get_history(self, task_id: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, task_id, ran_at, result, error FROM task_history "
                "WHERE task_id = ? ORDER BY ran_at DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return [
            {"id": r[0], "task_id": r[1], "ran_at": r[2], "result": r[3], "error": r[4]}
            for r in rows
        ]

    def history_stats(self, task_id: str) -> dict[str, Any]:
        """Run-count + last-run derived straight from task_history.

        Config-schedule jobs (synthetic ``cfg:…`` ids) have no `tasks` row, so
        their run_count/last_run can't come from the tasks table like DB tasks —
        this reads them from the history rows recorded on each fire instead."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*), MAX(ran_at) FROM task_history WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        count = int(row[0]) if row and row[0] is not None else 0
        return {"run_count": count, "last_run": row[1] if row else None}

    def history_for_day(self, day: str, limit: int = 200) -> list[dict[str, Any]]:
        """Return every scheduled-task run recorded on ``day`` (``YYYY-MM-DD``),
        joined to its task's name + owning agent, newest first.

        Powers the daily work journal's "completed scheduled tasks" narrative —
        a LEFT JOIN so a run whose task was later deleted still appears (with a
        null name) instead of vanishing from the day's record.
        """
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT h.task_id, h.ran_at, h.result, h.error, t.name, t.agent_name "
                "FROM task_history h LEFT JOIN tasks t ON h.task_id = t.id "
                "WHERE substr(h.ran_at, 1, 10) = ? ORDER BY h.ran_at DESC LIMIT ?",
                (day, limit),
            ).fetchall()
        return [
            {
                "task_id": r[0],
                "ran_at": r[1],
                "result": r[2],
                "error": r[3],
                "name": r[4],
                "agent_name": r[5],
            }
            for r in rows
        ]

    # ----------------------------------------------------------------- semantic search

    def semantic_search(
        self, query_embedding: np.ndarray, k: int = 5, caller: str | None = None
    ) -> list[dict[str, Any]]:
        """
        Return top-k tasks ranked by cosine similarity to the query embedding.
        Only considers tasks with a stored embedding.

        ``caller`` applies the same visibility rule as :meth:`list_tasks` — that
        agent's own tasks plus shared ones. Without it this ranks every agent's
        private tasks and hands back their names and prompts. The filter is in
        SQL rather than applied to the ranked output so ``k`` stays honest: a
        post-filter would silently return fewer than ``k`` visible results.
        ``caller=None`` keeps the unfiltered behaviour for callers with no agent
        identity (the dashboard, which gates separately).
        """
        sql = "SELECT id, embedding FROM tasks WHERE embedding IS NOT NULL AND enabled = 1"
        params: list[Any] = []
        if caller:
            # Character-for-character the list_tasks predicate: one visibility
            # rule, or search surfaces rows the listing hides.
            sql += " AND (created_by = ? OR visibility = 'shared')"
            params.append(caller)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()

        if not rows:
            return []

        candidates = []
        for row_id, emb_blob in rows:
            try:
                vec = pickle.loads(emb_blob)
                candidates.append((row_id, vec))
            except Exception:
                continue

        if not candidates:
            return []

        from olano.memory.embeddings import top_k_similar

        vecs = [c[1] for c in candidates]
        ranked = top_k_similar(query_embedding, vecs, k=k)

        results = []
        for idx, score in ranked:
            task_id = candidates[idx][0]
            task = self.get_task(task_id)
            if task:
                task["similarity_score"] = round(score, 4)
                results.append(task)
        return results

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _row_to_dict(row: tuple) -> dict[str, Any]:
        keys = [
            "id",
            "name",
            "prompt",
            "schedule",
            "run_at",
            "timezone",
            "enabled",
            "agent_name",
            "created_at",
            "last_run",
            "next_run",
            "run_count",
            "embedding",
            "visibility",
            "created_by",
            # Appended via ALTER on migrated tables, so column order matches
            # a fresh CREATE — these must stay last, in ALTER order.
            "platform",
            "user_id",
            "model",
            "reasoning_effort",
        ]
        d = dict(zip(keys, row, strict=False))
        d.pop("embedding", None)  # don't expose blob in public dicts
        d["enabled"] = bool(d.get("enabled", 1))
        d.setdefault("visibility", "private")
        # Always present on the wire, "" for the agent default, so every
        # reader (dashboard form, tools) can treat it as a plain string.
        d["model"] = str(d.get("model") or "")
        d["reasoning_effort"] = str(d.get("reasoning_effort") or "")
        return d
