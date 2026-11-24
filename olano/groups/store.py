"""SQLite store for multi-bot group threads.

Four tables: ``groups`` (the thread head), ``group_members`` (the roster, in
speaking order), ``group_messages`` (the append-only transcript) and
``group_shares`` (the team accounts the creator let in). Every message row
carries **who produced it**, ``author`` plus ``author_kind``
(``human`` / ``agent`` / ``system``) — because in a thread with six
participants attribution is the only thing that makes the transcript readable,
and it is what the addressed agent is shown on its turn.

**Humans are never roster members.** The roster is agents, and its 2–6 bound
is a cost guard on booted agents; a person reaches a group by creating it, by
being an admin, or by being *shared* into it (``group_shares``: one row per
user id, added by the creator or an admin). Sharing is the document model, not
the membership model: a shared person reads and posts, and leaves on their
own, but does not rename, re-roster, re-share or delete. The route layer is
where those rules are enforced; this module only stores the rows and reports
them (``shared_with``) next to the humans the transcript has actually heard
from (``humans``), so a reader can list who is in the room without a second
query per person.

Attribution is never inferred here: the caller passes the actor, the same
discipline ``olano/issues/store.py`` follows. The route layer binds a human
from the session cookie and an agent from the roster; a model never chooses
its own author string.

Storage lives at ``<home>/groups.db``, separate from ``issues.db`` and the
checkpointer's ``threads.db``: this is a *record of the conversation*, not the
resumable model context (which stays in the checkpointer, keyed by the
group-scoped thread id the route layer derives).
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

# Same discipline as the checkpointer and the issue store: every connection
# gets a busy_timeout so a concurrent agent turn queues instead of raising
# "database is locked".
_BUSY_TIMEOUT_MS = 30_000

#: Roster bounds the feature is designed around. A group needs at least two
#: agents to be a group at all, and each additional member is a booted agent
#: with its own thread state, so the ceiling is a real cost guard rather than a
#: cosmetic limit. ``groups.min_members`` / ``groups.max_members`` in
#: ``config.yaml`` may narrow this, never widen it (they clamp to this range).
MIN_MEMBERS = 2
MAX_MEMBERS = 6

AUTHOR_KINDS = ("human", "agent", "system")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    agent_name TEXT NOT NULL,
    position INTEGER NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (group_id, agent_name)
);
CREATE TABLE IF NOT EXISTS group_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    author TEXT NOT NULL,
    author_kind TEXT NOT NULL,
    addressee TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    reply_to TEXT NOT NULL DEFAULT '',
    segments TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS group_shares (
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    added_by TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_group_members_group ON group_members(group_id, position);
CREATE INDEX IF NOT EXISTS idx_group_messages_group ON group_messages(group_id, id);
CREATE INDEX IF NOT EXISTS idx_group_shares_user ON group_shares(user_id, group_id);
"""


def _message_row(row: Any) -> dict[str, Any]:
    """One ``group_messages`` row as the API hands it out.

    The single place ``segments`` is decoded: stored as a JSON string so the
    column stays additive, but a client (and every test) should never see the
    blob. Unparseable text degrades to no segments rather than raising — this
    runs inside a request, and a transcript that 500s because one row's JSON is
    malformed is a worse failure than one that renders the old way.

    Deliberately NOT used by ``unseen_for``: that feeds ``render_transcript``
    into a prompt, where the spans would be noise on top of the body they
    already spell out.
    """
    out = dict(row)
    raw = out.pop("segments", "") or ""
    if raw:
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                out["segments"] = decoded
        except (json.JSONDecodeError, TypeError):
            logger.debug("group message %s has unparseable segments", out.get("id"))
    return out


class GroupError(ValueError):
    """Invalid group operation (unknown id, bad roster) — actionable text."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_members(members: Any) -> list[str]:
    """Normalize a roster: strings, stripped, de-duplicated, order preserved.

    Order is load-bearing — the first member is the default addressee for a
    human message that names nobody — so de-duplication must keep the *first*
    occurrence rather than whatever a ``set`` happens to yield.
    """
    out: list[str] = []
    for raw in members or []:
        name = str(raw).strip()
        if name and name not in out:
            out.append(name)
    return out


class GroupStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._conn() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Bring a database created by an older release up to ``_SCHEMA``.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table exactly as it
        was, so a column added later has to be applied by hand. Additive only,
        and every added column carries a default the old rows can wear.
        """
        have = {row["name"] for row in conn.execute("PRAGMA table_info(group_messages)")}
        if "reply_to" not in have:
            conn.execute("ALTER TABLE group_messages ADD COLUMN reply_to TEXT NOT NULL DEFAULT ''")
        if "segments" not in have:
            conn.execute("ALTER TABLE group_messages ADD COLUMN segments TEXT NOT NULL DEFAULT ''")

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

    # ── groups ─────────────────────────────────────────────────────────

    def create(
        self,
        *,
        name: str,
        members: list[str],
        created_by: str,
        min_members: int = MIN_MEMBERS,
        max_members: int = MAX_MEMBERS,
    ) -> dict[str, Any]:
        """Create a group. ``members`` order is the speaking order.

        ``created_by`` is the *stable* id of the human who created it (a user
        id, not a display name): it is what visibility is decided on later, so
        it must survive the human renaming themselves.
        """
        title = str(name or "").strip()
        if not title:
            raise GroupError("A group needs a name.")
        roster = _clean_members(members)
        self._assert_roster_size(len(roster), min_members, max_members)
        ts = _now()
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO groups (name, created_by, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (title, created_by, ts, ts),
            )
            group_id = int(cur.lastrowid or 0)
            conn.executemany(
                "INSERT INTO group_members (group_id, agent_name, position, added_at) "
                "VALUES (?, ?, ?, ?)",
                [(group_id, agent, i, ts) for i, agent in enumerate(roster)],
            )
        result = self.get(group_id)
        if result is None:  # pragma: no cover - the insert above just ran
            raise GroupError("Group creation failed.")
        return result

    def get(self, group_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
            if row is None:
                return None
            return self._group_dict(conn, row)

    def require(self, group_id: int) -> dict[str, Any]:
        group = self.get(group_id)
        if group is None:
            raise GroupError(f"No group #{group_id}")
        return group

    def list_groups(
        self,
        *,
        created_by: str | None = None,
        visible_to: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Groups, newest activity first.

        ``created_by`` filters to the groups one human made; ``visible_to``
        widens that to the groups a human made **or was shared into**, which is
        what a non-admin's rail shows. Admins pass neither and get everything.
        """
        sql = "SELECT * FROM groups"
        params: list[Any] = []
        if visible_to is not None:
            sql += (
                " WHERE created_by = ? OR id IN "
                "(SELECT group_id FROM group_shares WHERE user_id = ?)"
            )
            params.extend([visible_to, visible_to])
        elif created_by is not None:
            sql += " WHERE created_by = ?"
            params.append(created_by)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._group_dict(conn, row) for row in rows]

    def rename(self, group_id: int, name: str) -> dict[str, Any]:
        """Give a group a new title. The roster and transcript are untouched.

        A group's name is a *label*, never its identity: visibility is decided
        on ``created_by`` and every thread id derives from the numeric id, so
        renaming one cannot move a conversation, fork a member's memory or
        change who can see it. That is what makes this safe to expose as a
        right-click on the rail.
        """
        self.require(group_id)
        title = str(name or "").strip()
        if not title:
            raise GroupError("A group needs a name.")
        with self._lock, self._conn() as conn:
            # `updated_at` is what orders the rail, and a rename is not
            # activity — bumping it would float a renamed group above threads
            # that actually said something.
            conn.execute("UPDATE groups SET name = ? WHERE id = ?", (title, group_id))
        return self.require(group_id)

    def delete(self, group_id: int) -> bool:
        """Delete a group and (via ON DELETE CASCADE) its roster + transcript."""
        with self._lock, self._conn() as conn:
            cur = conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
            return cur.rowcount > 0

    # ── roster ─────────────────────────────────────────────────────────

    def add_member(
        self,
        group_id: int,
        agent_name: str,
        *,
        max_members: int = MAX_MEMBERS,
    ) -> dict[str, Any]:
        group = self.require(group_id)
        agent = str(agent_name or "").strip()
        if not agent:
            raise GroupError("An agent name is required.")
        if agent in group["members"]:
            raise GroupError(f"'{agent}' is already in this group.")
        if len(group["members"]) + 1 > max_members:
            raise GroupError(f"A group holds at most {max_members} agents.")
        ts = _now()
        with self._lock, self._conn() as conn:
            position = int(
                conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 AS p FROM group_members "
                    "WHERE group_id = ?",
                    (group_id,),
                ).fetchone()["p"]
            )
            conn.execute(
                "INSERT INTO group_members (group_id, agent_name, position, added_at) "
                "VALUES (?, ?, ?, ?)",
                (group_id, agent, position, ts),
            )
            conn.execute("UPDATE groups SET updated_at = ? WHERE id = ?", (ts, group_id))
        return self.require(group_id)

    def remove_member(
        self,
        group_id: int,
        agent_name: str,
        *,
        min_members: int = MIN_MEMBERS,
    ) -> dict[str, Any]:
        group = self.require(group_id)
        agent = str(agent_name or "").strip()
        if agent not in group["members"]:
            raise GroupError(f"'{agent}' is not in this group.")
        if len(group["members"]) - 1 < min_members:
            raise GroupError(f"A group needs at least {min_members} agents — delete it instead.")
        ts = _now()
        with self._lock, self._conn() as conn:
            conn.execute(
                "DELETE FROM group_members WHERE group_id = ? AND agent_name = ?",
                (group_id, agent),
            )
            conn.execute("UPDATE groups SET updated_at = ? WHERE id = ?", (ts, group_id))
        return self.require(group_id)

    # ── shares ─────────────────────────────────────────────────────────

    def share(self, group_id: int, user_id: str, *, added_by: str = "") -> dict[str, Any]:
        """Let one team account read and post in this group.

        Idempotent: sharing with someone already in is not an error, because
        two admins clicking the same name a second apart is not a conflict
        worth reporting. The creator is never a share, they are already the
        group's own, and the store says so rather than silently storing a row
        that visibility would never consult.
        """
        group = self.require(group_id)
        uid = str(user_id or "").strip()
        if not uid:
            raise GroupError("A user id is required.")
        if uid == str(group.get("created_by") or ""):
            raise GroupError("The group's creator already has it; nothing to share.")
        if uid in group.get("shared_with", []):
            return group
        ts = _now()
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO group_shares (group_id, user_id, added_by, added_at) "
                "VALUES (?, ?, ?, ?)",
                (group_id, uid, str(added_by or ""), ts),
            )
            # Sharing is activity: the rail orders on `updated_at`, and a group
            # that was just opened to somebody should float up for them.
            conn.execute("UPDATE groups SET updated_at = ? WHERE id = ?", (ts, group_id))
        return self.require(group_id)

    def unshare(self, group_id: int, user_id: str) -> dict[str, Any]:
        """Take a team account back out. What they posted stays attributed."""
        group = self.require(group_id)
        uid = str(user_id or "").strip()
        if uid not in group.get("shared_with", []):
            raise GroupError("That person is not shared into this group.")
        with self._lock, self._conn() as conn:
            conn.execute(
                "DELETE FROM group_shares WHERE group_id = ? AND user_id = ?", (group_id, uid)
            )
        return self.require(group_id)

    # ── messages ───────────────────────────────────────────────────────

    def add_message(
        self,
        group_id: int,
        *,
        author: str,
        author_kind: str,
        body: str,
        addressee: str = "",
        reply_to: str = "",
        segments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Append one attributed message.

        ``addressee`` records who a turn was aimed at (a human's ``@name``, or
        the member a bot's reply hands the thread to), so the transcript
        explains its own turn-taking; ``reply_to`` records which member a
        bot-to-bot turn was *answering*, so a reader can tell an answer from a
        new hand-off without either being written into the text.

        ``segments`` is the reply's ordered spans — ``[{"text", "after"}]``,
        where ``after`` names the tool call each span resumed behind — so a
        reload can put the tool cards between the paragraphs they belong to
        instead of stacking them all above one glued reply. Stored only when the
        turn actually interleaved: a single span is the pre-segment layout,
        which the renderer already handles by absence, and skipping it keeps
        every ordinary row byte-identical to what earlier releases wrote.
        """
        self.require(group_id)
        text = str(body or "").strip()
        if not text:
            raise GroupError("A message needs text.")
        if author_kind not in AUTHOR_KINDS:
            raise GroupError(f"author_kind must be one of {', '.join(AUTHOR_KINDS)}")
        who = str(author or "").strip()
        if not who:
            raise GroupError("A message needs an author.")
        ts = _now()
        spans = segments or []
        encoded = (
            json.dumps(spans, ensure_ascii=False, separators=(",", ":")) if len(spans) > 1 else ""
        )
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO group_messages "
                "(group_id, ts, author, author_kind, addressee, body, reply_to, segments) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    group_id,
                    ts,
                    who,
                    author_kind,
                    str(addressee or "").strip(),
                    text,
                    str(reply_to or "").strip(),
                    encoded,
                ),
            )
            conn.execute("UPDATE groups SET updated_at = ? WHERE id = ?", (ts, group_id))
            row = conn.execute(
                "SELECT * FROM group_messages WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
        return _message_row(row)

    def messages(self, group_id: int, *, limit: int = 200) -> list[dict[str, Any]]:
        """The newest ``limit`` messages, oldest→newest (reading order)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM group_messages WHERE group_id = ? ORDER BY id DESC LIMIT ?",
                (group_id, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [_message_row(r) for r in reversed(rows)]

    def unseen_for(self, group_id: int, agent_name: str, *, limit: int = 50) -> list[dict]:
        """Messages this agent has not been shown yet.

        Every member keeps its own checkpointed thread, so what it already
        holds is exactly the conversation up to and including its own last
        message. Everything after that — the other members' replies and the
        human's turns — is what has to travel into its next prompt. Deriving
        it from the transcript means there is no per-member watermark to keep
        in sync with the messages themselves.
        """
        with self._conn() as conn:
            last = conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS last_id FROM group_messages "
                "WHERE group_id = ? AND author = ? AND author_kind = 'agent'",
                (group_id, agent_name),
            ).fetchone()["last_id"]
            rows = conn.execute(
                "SELECT * FROM group_messages WHERE group_id = ? AND id > ? "
                "ORDER BY id DESC LIMIT ?",
                (group_id, int(last), max(1, min(int(limit), 200))),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    # ── internals ──────────────────────────────────────────────────────

    @staticmethod
    def _assert_roster_size(count: int, min_members: int, max_members: int) -> None:
        if count < min_members or count > max_members:
            raise GroupError(f"A group holds {min_members}–{max_members} agents (got {count}).")

    @staticmethod
    def _group_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        group = dict(row)
        members = conn.execute(
            "SELECT agent_name FROM group_members WHERE group_id = ? ORDER BY position, agent_name",
            (group["id"],),
        ).fetchall()
        group["members"] = [m["agent_name"] for m in members]
        group["message_count"] = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM group_messages WHERE group_id = ?",
                (group["id"],),
            ).fetchone()["n"]
        )
        # The team accounts let in, oldest share first (user ids: the route
        # layer resolves them to people, this store knows nothing of users).
        shares = conn.execute(
            "SELECT user_id FROM group_shares WHERE group_id = ? ORDER BY added_at, user_id",
            (group["id"],),
        ).fetchall()
        group["shared_with"] = [s["user_id"] for s in shares]
        # The humans the transcript has heard from, in order of first word
        # (`human-<handle>` principals). Derived rather than stored: an admin
        # who posted into somebody's group is in the room by having spoken,
        # and no share row will ever say so.
        spoke = conn.execute(
            "SELECT author, MIN(id) AS first_id FROM group_messages "
            "WHERE group_id = ? AND author_kind = 'human' GROUP BY author ORDER BY first_id",
            (group["id"],),
        ).fetchall()
        group["humans"] = [s["author"] for s in spoke]
        return group


def get_group_store(home: str | Path | None = None) -> GroupStore:
    """Store over ``<home>/groups.db``. Cheap to construct — no caching needed."""
    from olano.config.home import get_olano_paths

    return GroupStore(get_olano_paths(home).root / "groups.db")
