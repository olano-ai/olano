"""SQLite-backed delivery log for outbound webhooks.

One row per delivery attempt — keyed by ``(agent_name, webhook_id)`` so the
dashboard can render per-webhook history and so success/failure counts survive
process restarts. A small retention cap keeps the table bounded.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)

# Keep at most this many rows per (agent, webhook); pruned after each insert.
_MAX_ROWS_PER_HOOK = 200

_CREATE_DELIVERIES = """
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id            TEXT PRIMARY KEY,
    agent_name    TEXT NOT NULL,
    webhook_id    TEXT NOT NULL,
    webhook_name  TEXT,
    event_type    TEXT NOT NULL,
    url           TEXT NOT NULL,
    ts            TEXT NOT NULL,
    ok            INTEGER NOT NULL,
    status_code   INTEGER,
    duration_ms   REAL,
    attempts      INTEGER DEFAULT 1,
    is_test       INTEGER DEFAULT 0,
    direction     TEXT DEFAULT 'outbound',
    error         TEXT,
    request_body  TEXT,
    response_body TEXT
)
"""

_CREATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_hook
ON webhook_deliveries (agent_name, webhook_id, ts)
"""


class WebhookDB:
    """Append-only delivery log with per-webhook history and stats."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or get_olano_paths().webhooks_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(_CREATE_DELIVERIES)
            conn.execute(_CREATE_INDEX)
            # Migrate tables created before the inbound direction column.
            try:
                conn.execute(
                    "ALTER TABLE webhook_deliveries ADD COLUMN direction TEXT DEFAULT 'outbound'"
                )
            except sqlite3.OperationalError:
                pass  # column already exists
            conn.commit()

    # ------------------------------------------------------------------ write

    def record(
        self,
        *,
        agent_name: str,
        webhook_id: str,
        webhook_name: str,
        event_type: str,
        url: str,
        ok: bool,
        status_code: int | None = None,
        duration_ms: float | None = None,
        attempts: int = 1,
        is_test: bool = False,
        direction: str = "outbound",
        error: str | None = None,
        request_body: str | None = None,
        response_body: str | None = None,
    ) -> str:
        """Persist one delivery attempt; returns its row id."""
        row_id = uuid.uuid4().hex
        ts = datetime.now(timezone.utc).isoformat()
        # Cap stored bodies so a chatty receiver can't bloat the DB.
        req = (request_body or "")[:8000] or None
        resp = (response_body or "")[:8000] or None
        try:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT INTO webhook_deliveries
                        (id, agent_name, webhook_id, webhook_name, event_type, url,
                         ts, ok, status_code, duration_ms, attempts, is_test, direction,
                         error, request_body, response_body)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_id,
                        agent_name,
                        webhook_id,
                        webhook_name,
                        event_type,
                        url,
                        ts,
                        int(ok),
                        status_code,
                        duration_ms,
                        attempts,
                        int(is_test),
                        direction,
                        error,
                        req,
                        resp,
                    ),
                )
                self._prune(conn, agent_name, webhook_id)
                conn.commit()
        except sqlite3.Error as exc:  # logging must never break delivery
            logger.warning("WebhookDB.record failed: %s", exc)
        return row_id

    def _prune(self, conn: sqlite3.Connection, agent_name: str, webhook_id: str) -> None:
        conn.execute(
            """
            DELETE FROM webhook_deliveries
            WHERE agent_name = ? AND webhook_id = ? AND id NOT IN (
                SELECT id FROM webhook_deliveries
                WHERE agent_name = ? AND webhook_id = ?
                ORDER BY ts DESC LIMIT ?
            )
            """,
            (agent_name, webhook_id, agent_name, webhook_id, _MAX_ROWS_PER_HOOK),
        )

    # ------------------------------------------------------------------- read

    def history(self, agent_name: str, webhook_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM webhook_deliveries
                WHERE agent_name = ? AND webhook_id = ?
                ORDER BY ts DESC LIMIT ?
                """,
                (agent_name, webhook_id, max(1, min(limit, _MAX_ROWS_PER_HOOK))),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def stats(self, agent_name: str, webhook_id: str) -> dict[str, Any]:
        """Aggregate counts + last delivery for a webhook (excludes tests)."""
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS ok_count,
                    MAX(ts) AS last_ts
                FROM webhook_deliveries
                WHERE agent_name = ? AND webhook_id = ? AND is_test = 0
                """,
                (agent_name, webhook_id),
            ).fetchone()
            last = conn.execute(
                """
                SELECT ok, status_code, ts FROM webhook_deliveries
                WHERE agent_name = ? AND webhook_id = ? AND is_test = 0
                ORDER BY ts DESC LIMIT 1
                """,
                (agent_name, webhook_id),
            ).fetchone()
        total = row["total"] or 0
        ok_count = row["ok_count"] or 0
        return {
            "total": total,
            "ok": ok_count,
            "failed": total - ok_count,
            "last_ts": row["last_ts"],
            "last_ok": bool(last["ok"]) if last else None,
            "last_status": last["status_code"] if last else None,
        }

    def purge_webhook(self, agent_name: str, webhook_id: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM webhook_deliveries WHERE agent_name = ? AND webhook_id = ?",
                (agent_name, webhook_id),
            )
            conn.commit()
        return cur.rowcount

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["ok"] = bool(d.get("ok"))
        d["is_test"] = bool(d.get("is_test"))
        return d


_DEFAULT_DB: WebhookDB | None = None


def get_webhook_db() -> WebhookDB:
    """Process-wide singleton over the default DB path."""
    global _DEFAULT_DB
    if _DEFAULT_DB is None:
        _DEFAULT_DB = WebhookDB()
    return _DEFAULT_DB
