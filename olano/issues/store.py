"""SQLite store for agent-to-agent issues.

Two tables: ``issues`` (current state) and ``issue_events`` (append-only
audit trail — comments, state changes, assignments). The event log is the
source of truth the dashboard thread view renders; the ``issues`` row is the
denormalized head so listing never replays events.

Attribution is the caller's job: every mutating method takes an explicit
``actor``, and the tool layer binds it from the ``current_agent`` contextvar
— it is never accepted from model-controlled input. Backend-swap note
(Phase 2): keep this class's method surface stable; the Gitea store
implements the same shape behind the same tools.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Mirrors the checkpointer discipline: every connection gets a busy_timeout
# so concurrent agent turns queue instead of raising "database is locked".
_BUSY_TIMEOUT_MS = 30_000

VALID_STATES = ("open", "closed")
VALID_TYPES = ("task", "bug", "feature", "question", "incident")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL,
    assignee TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open',
    type TEXT NOT NULL DEFAULT 'task',
    labels TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS issue_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_issue_events_issue ON issue_events(issue_id);
CREATE INDEX IF NOT EXISTS idx_issues_state ON issues(state);
CREATE INDEX IF NOT EXISTS idx_issues_assignee ON issues(assignee);
CREATE TABLE IF NOT EXISTS issue_watermarks (
    principal TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issue_embeddings (
    issue_id INTEGER PRIMARY KEY REFERENCES issues(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    vector BLOB NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# Keyword index over title + body + the comment thread. A plain (self-storing)
# FTS5 table keyed by rowid = issue id, rebuilt whole-row on write — at this
# table's volume (thousands of rows) delete+reinsert is simpler and safer than
# external-content triggers. Created separately from _SCHEMA so a SQLite built
# without FTS5 degrades to the LIKE path instead of failing the store.
_FTS_SCHEMA = "CREATE VIRTUAL TABLE IF NOT EXISTS issues_fts USING fts5(title, body, comments)"


def _to_fts_match(query: str) -> str:
    """Safe FTS5 ``MATCH`` expression from arbitrary text.

    Delegates to the sanitizer proven in ``memory/knowledge_vault.py`` (terms
    quoted as FTS5 string literals so ``.``/``-``/``:`` never parse as
    syntax); local fallback quotes terms the same way if that import moves.
    """
    try:
        from olano.memory.knowledge_vault import _to_fts_match as _kv_match

        return _kv_match(query)
    except Exception:
        terms = [
            '"' + t.replace('"', '""') + '"' for t in query.split() if any(c.isalnum() for c in t)
        ]
        return " ".join(terms)


class IssueError(ValueError):
    """Invalid issue operation (unknown id, bad transition) — actionable text."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IssueStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fts_ok = True
        with self._lock, self._conn() as conn:
            conn.executescript(_SCHEMA)
            # Migration idiom (webhooks/db.py, scheduler/db.py): pre-`type`
            # databases gain the column; existing rows default to 'task'.
            try:
                conn.execute("ALTER TABLE issues ADD COLUMN type TEXT NOT NULL DEFAULT 'task'")
            except sqlite3.OperationalError:
                pass  # column already exists
            try:
                conn.execute(_FTS_SCHEMA)
                # Backfill rows that predate the index (or were written while
                # FTS was unavailable). Idempotent and tiny at this scale.
                missing = conn.execute(
                    "SELECT id FROM issues WHERE id NOT IN (SELECT rowid FROM issues_fts)"
                ).fetchall()
                for row in missing:
                    self._fts_reindex(conn, int(row["id"]))
            except sqlite3.OperationalError:
                self._fts_ok = False  # SQLite built without FTS5 → LIKE fallback
                logger.info("issues: FTS5 unavailable — keyword search uses LIKE fallback")

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── reads ──────────────────────────────────────────────────────────

    def get(self, issue_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        return self._issue_dict(row) if row else None

    def list_issues(
        self,
        *,
        state: str = "",
        assignee: str = "",
        author: str = "",
        label: str = "",
        issue_type: str = "",
        query: str = "",
        sort: str = "updated",
        limit: int = 20,
        ranked_ids: list[int] | None = None,
    ) -> list[dict[str, Any]]:
        """List with filters. ``query`` searches title/body/comments (FTS5
        BM25, LIKE fallback). ``sort="relevance"`` orders by search rank and
        falls back to recency without a query. ``ranked_ids`` is the seam the
        semantic layer uses: a pre-ranked id list (hybrid FTS+vector) that
        replaces the keyword match while every other filter still applies.
        """
        rank_order: list[int] | None = None
        clauses: list[str] = []
        params: list[Any] = []
        if ranked_ids is not None:
            if not ranked_ids:
                return []
            rank_order = list(ranked_ids)
            marks = ",".join("?" * len(rank_order))
            clauses.append(f"id IN ({marks})")
            params.extend(rank_order)
        elif query:
            fts_ids = self.search_ids(query, limit=max(int(limit) * 4, 50))
            if fts_ids is not None:
                if not fts_ids:
                    return []
                rank_order = fts_ids
                marks = ",".join("?" * len(fts_ids))
                clauses.append(f"id IN ({marks})")
                params.extend(fts_ids)
            else:  # FTS5 unavailable — original LIKE fallback (unranked)
                clauses.append("(title LIKE ? OR body LIKE ?)")
                like = f"%{query}%"
                params.extend([like, like])
        if state:
            clauses.append("state = ?")
            params.append(state)
        if assignee:
            clauses.append("assignee = ?")
            params.append(assignee)
        if author:
            clauses.append("author = ?")
            params.append(author)
        if issue_type:
            clauses.append("type = ?")
            params.append(issue_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = {
            "updated": "updated_at DESC",
            "created": "created_at DESC",
            "oldest": "created_at ASC",
            "id": "id ASC",
            "relevance": "updated_at DESC",  # re-ranked in Python below
        }.get(sort, "updated_at DESC")
        sql = (
            "SELECT issues.*,"
            " (SELECT COUNT(*) FROM issue_events e"
            "   WHERE e.issue_id = issues.id AND e.kind = 'comment') AS comment_count"
            f" FROM issues {where} ORDER BY {order} LIMIT ?"
        )
        with self._conn() as conn:
            rows = conn.execute(sql, (*params, max(1, min(int(limit), 200)))).fetchall()
        issues = [self._issue_dict(r) for r in rows]
        # Labels are a JSON list; filtering in Python keeps the SQL sane at
        # this volume (thousands of rows, not millions).
        if label:
            issues = [i for i in issues if label in i["labels"]]
        if sort == "relevance" and rank_order:
            position = {issue_id: i for i, issue_id in enumerate(rank_order)}
            issues.sort(key=lambda item: position.get(item["id"], len(position)))
        return issues[: max(1, min(int(limit), 200))]

    def search_ids(self, query: str, limit: int = 50) -> list[int] | None:
        """BM25-ranked issue ids for a free-text query, best match first.

        Returns ``None`` when FTS5 is unavailable (caller falls back to LIKE)
        and ``[]`` when the query has no searchable terms or no matches.
        """
        if not self._fts_ok:
            return None
        match = _to_fts_match(query)
        if not match:
            return []
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT rowid FROM issues_fts WHERE issues_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match, max(1, int(limit))),
                ).fetchall()
        except sqlite3.OperationalError:  # defensive: malformed MATCH
            return []
        return [int(r["rowid"]) for r in rows]

    def events(self, issue_id: int, limit: int = 200) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM issue_events WHERE issue_id = ? ORDER BY id ASC LIMIT ?",
                (issue_id, limit),
            ).fetchall()
        return [
            {
                "id": r["id"],
                "issue_id": r["issue_id"],
                "ts": r["ts"],
                "actor": r["actor"],
                "kind": r["kind"],
                "payload": json.loads(r["payload"] or "{}"),
            }
            for r in rows
        ]

    # ── writes (actor is bound by the tool layer, never model input) ───

    def create(
        self,
        *,
        title: str,
        body: str,
        author: str,
        assignee: str = "",
        labels: list[str] | None = None,
        issue_type: str = "task",
    ) -> dict[str, Any]:
        title = title.strip()
        if not title:
            raise IssueError("Issue title must not be empty.")
        issue_type = (issue_type or "task").strip().lower()
        if issue_type not in VALID_TYPES:
            raise IssueError(f"Unknown type {issue_type!r} — expected one of {VALID_TYPES}.")
        now = _now()
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO issues"
                " (title, body, author, assignee, type, labels, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (title, body, author, assignee, issue_type, json.dumps(labels or []), now, now),
            )
            issue_id = int(cur.lastrowid or 0)
            if assignee:
                self._add_event(conn, issue_id, author, "assign", {"assignee": assignee})
            self._fts_reindex(conn, issue_id)
        issue = self.get(issue_id)
        assert issue is not None
        return issue

    def comment(self, issue_id: int, *, actor: str, body: str) -> dict[str, Any]:
        body = body.strip()
        if not body:
            raise IssueError("Comment body must not be empty.")
        with self._lock, self._conn() as conn:
            self._require(conn, issue_id)
            self._add_event(conn, issue_id, actor, "comment", {"body": body})
            self._touch(conn, issue_id)
            self._fts_reindex(conn, issue_id)
        issue = self.get(issue_id)
        assert issue is not None
        return issue

    def set_state(
        self, issue_id: int, *, actor: str, state: str, reason: str = ""
    ) -> dict[str, Any]:
        if state not in VALID_STATES:
            raise IssueError(f"Unknown state {state!r} — expected one of {VALID_STATES}.")
        kind = "close" if state == "closed" else "reopen"
        with self._lock, self._conn() as conn:
            row = self._require(conn, issue_id)
            if row["state"] == state:
                raise IssueError(f"Issue #{issue_id} is already {state}.")
            payload: dict[str, Any] = {}
            if reason:
                payload["reason"] = reason
            self._add_event(conn, issue_id, actor, kind, payload)
            conn.execute(
                "UPDATE issues SET state = ?, closed_at = ?, updated_at = ? WHERE id = ?",
                (state, _now() if state == "closed" else None, _now(), issue_id),
            )
        issue = self.get(issue_id)
        assert issue is not None
        return issue

    def set_labels(self, issue_id: int, *, actor: str, labels: list[str]) -> dict[str, Any]:
        labels = sorted({part.strip() for part in labels if part.strip()})
        with self._lock, self._conn() as conn:
            self._require(conn, issue_id)
            self._add_event(conn, issue_id, actor, "label", {"labels": labels})
            conn.execute(
                "UPDATE issues SET labels = ?, updated_at = ? WHERE id = ?",
                (json.dumps(labels), _now(), issue_id),
            )
        issue = self.get(issue_id)
        assert issue is not None
        return issue

    def set_type(self, issue_id: int, *, actor: str, issue_type: str) -> dict[str, Any]:
        issue_type = (issue_type or "").strip().lower()
        if issue_type not in VALID_TYPES:
            raise IssueError(f"Unknown type {issue_type!r} — expected one of {VALID_TYPES}.")
        with self._lock, self._conn() as conn:
            self._require(conn, issue_id)
            self._add_event(conn, issue_id, actor, "type", {"type": issue_type})
            conn.execute(
                "UPDATE issues SET type = ?, updated_at = ? WHERE id = ?",
                (issue_type, _now(), issue_id),
            )
        issue = self.get(issue_id)
        assert issue is not None
        return issue

    def assign(self, issue_id: int, *, actor: str, assignee: str) -> dict[str, Any]:
        with self._lock, self._conn() as conn:
            self._require(conn, issue_id)
            self._add_event(conn, issue_id, actor, "assign", {"assignee": assignee})
            conn.execute(
                "UPDATE issues SET assignee = ?, updated_at = ? WHERE id = ?",
                (assignee, _now(), issue_id),
            )
        issue = self.get(issue_id)
        assert issue is not None
        return issue

    def _fts_reindex(self, conn: sqlite3.Connection, issue_id: int) -> None:
        """Rebuild one issue's FTS row (title + body + all comment bodies)."""
        if not self._fts_ok:
            return
        head = conn.execute("SELECT title, body FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if head is None:
            return
        comment_rows = conn.execute(
            "SELECT payload FROM issue_events WHERE issue_id = ? AND kind = 'comment'"
            " ORDER BY id ASC",
            (issue_id,),
        ).fetchall()
        comments = "\n".join(
            str(json.loads(r["payload"] or "{}").get("body") or "") for r in comment_rows
        )
        try:
            conn.execute("DELETE FROM issues_fts WHERE rowid = ?", (issue_id,))
            conn.execute(
                "INSERT INTO issues_fts (rowid, title, body, comments) VALUES (?, ?, ?, ?)",
                (issue_id, head["title"], head["body"], comments),
            )
        except sqlite3.OperationalError:
            self._fts_ok = False

    # ── embedding sidecar (semantic layer's storage; see issues/semantic.py) ──

    def vector_rows(self, model: str) -> list[tuple[int, bytes]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT issue_id, vector FROM issue_embeddings WHERE model = ?", (model,)
            ).fetchall()
        return [(int(r["issue_id"]), bytes(r["vector"])) for r in rows]

    def set_vector(self, issue_id: int, model: str, vector: bytes) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO issue_embeddings (issue_id, model, vector, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(issue_id) DO UPDATE SET"
                "   model = excluded.model, vector = excluded.vector,"
                "   updated_at = excluded.updated_at",
                (issue_id, model, vector, _now()),
            )

    def ids_missing_vector(self, model: str, limit: int = 500) -> list[int]:
        """Issues without a current-model embedding — the backfill worklist.
        A model switch naturally re-embeds everything, one drain at a time."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id FROM issues WHERE id NOT IN"
                " (SELECT issue_id FROM issue_embeddings WHERE model = ?)"
                " ORDER BY id ASC LIMIT ?",
                (model, max(1, int(limit))),
            ).fetchall()
        return [int(r["id"]) for r in rows]

    # ── delivery watermarks ────────────────────────────────────────────
    # Last-checked timestamp per principal, used by the per-agent issue
    # poll (scheduler) to find "new activity since I last looked". Lives
    # here — not in any feature's state file — so delivery never depends
    # on another subsystem being enabled.

    def get_watermark(self, principal: str) -> str:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT checked_at FROM issue_watermarks WHERE principal = ?", (principal,)
            ).fetchone()
        return str(row["checked_at"]) if row else ""

    def set_watermark(self, principal: str, checked_at: str = "") -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO issue_watermarks (principal, checked_at) VALUES (?, ?)"
                " ON CONFLICT(principal) DO UPDATE SET checked_at = excluded.checked_at",
                (principal, checked_at or _now()),
            )

    # ── helpers ────────────────────────────────────────────────────────

    def _require(self, conn: sqlite3.Connection, issue_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            raise IssueError(f"No issue #{issue_id}. Use issue_list to see existing issues.")
        assert isinstance(row, sqlite3.Row)  # narrowed: fetchone() is typed Any
        return row

    @staticmethod
    def _add_event(
        conn: sqlite3.Connection,
        issue_id: int,
        actor: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO issue_events (issue_id, ts, actor, kind, payload) VALUES (?, ?, ?, ?, ?)",
            (issue_id, _now(), actor, kind, json.dumps(payload)),
        )

    @staticmethod
    def _touch(conn: sqlite3.Connection, issue_id: int) -> None:
        conn.execute("UPDATE issues SET updated_at = ? WHERE id = ?", (_now(), issue_id))

    @staticmethod
    def _issue_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["labels"] = json.loads(d.get("labels") or "[]")
        return d


def get_issue_store(home: str | Path | None = None) -> IssueStore:
    """Store over ``<home>/issues.db``. Cheap to construct — no caching needed."""
    from olano.config.home import get_olano_paths

    return IssueStore(get_olano_paths(home).issues_db_path)
