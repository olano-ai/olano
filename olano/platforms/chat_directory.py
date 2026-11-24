"""Persistent cross-platform chat directory — name ↔ id memory for agents.

Every adapter already extracts full identity for every inbound message (a
:class:`~olano.platforms.session.SessionSource` with ``chat_id``,
``chat_name``, ``chat_type``, ``user_id``, ``username``, …) and then throws it
away after the turn. This module keeps it: a SQLite store under the Olano
home that records every chat and participant an agent has seen, so "send the
reminder to the community group" is a lookup instead of a memory exercise.

Populated from three sources, in decreasing order of universality:

1. **Inbound messages** (``record_message_event`` — called from
   ``BasePlatformAdapter.handle_message``): works identically on every
   platform, costs no API calls, and self-heals as names change. Covers
   platforms whose APIs cannot enumerate chats at all (Telegram bots, WeChat /
   WeCom / weixin / debox, SMS…).
2. **Membership events** (``record_membership`` — Telegram ``my_chat_member``,
   Matrix room joins): captures a chat the moment the bot is added, before
   anyone has spoken.
3. **Native enumeration** (``record_enumerated`` — adapters that implement
   ``list_chats()``: WhatsApp groups, Discord channels, Slack conversations,
   Matrix rooms, Signal groups).

Scoping: rows are keyed per **agent** — an agent only ever sees chats that its
own adapters witnessed. Chat and user names are attacker-controlled platform
data; lookup results therefore always carry the id next to the name, and the
resolver refuses ambiguous name matches instead of guessing.

Writes are best-effort and must never break message handling: the recorder
swallows every exception and throttles repeat writes per (chat, user) so a
busy group costs ~one write per interval, not one per message.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from olano.platforms.session import SessionSource

logger = logging.getLogger(__name__)

_CREATE_CHATS = """
CREATE TABLE IF NOT EXISTS chats (
    agent       TEXT NOT NULL,
    platform    TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    chat_name   TEXT DEFAULT '',
    chat_type   TEXT DEFAULT 'dm',   -- dm | group | channel | thread | forum
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    msg_count   INTEGER DEFAULT 0,
    active      INTEGER DEFAULT 1,   -- 0 once the bot left / was removed
    migrated_to TEXT DEFAULT '',     -- successor chat_id (Telegram supergroup upgrade)
    origin      TEXT DEFAULT 'message',  -- message | membership | enumeration
    PRIMARY KEY (agent, platform, chat_id)
)
"""

_CREATE_PARTICIPANTS = """
CREATE TABLE IF NOT EXISTS participants (
    agent        TEXT NOT NULL,
    platform     TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    username     TEXT DEFAULT '',
    display_name TEXT DEFAULT '',
    is_bot       INTEGER DEFAULT 0,
    last_chat_id TEXT DEFAULT '',
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    msg_count    INTEGER DEFAULT 0,
    PRIMARY KEY (agent, platform, user_id)
)
"""

_CREATE_OBSERVED = """
CREATE TABLE IF NOT EXISTS observed_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,
    platform    TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    thread_id   TEXT DEFAULT '',
    sender_id   TEXT DEFAULT '',
    sender_name TEXT DEFAULT '',
    text        TEXT NOT NULL,
    ts          TEXT NOT NULL
)
"""

_CREATE_OBSERVED_INDEX = """
CREATE INDEX IF NOT EXISTS idx_observed_chat
    ON observed_messages (agent, platform, chat_id, id)
"""

#: Repeat writes for the same (agent, platform, chat, user) are skipped inside
#: this window unless a name changed — a busy group must not turn every message
#: into a disk write.
_WRITE_THROTTLE_SECONDS = 30.0

#: Observed-message defaults; per-connector overrides via the
#: ``group_listen_max_messages`` / ``group_listen_retention_days`` extra keys.
OBSERVED_MAX_PER_CHAT = 500
OBSERVED_RETENTION_DAYS = 14
#: Observed lines are context, not transcripts — cap each entry.
OBSERVED_MAX_TEXT_CHARS = 1200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _like_escape(query: str) -> str:
    """Escape SQL LIKE wildcards so user text matches literally."""
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class ChatDirectory:
    """SQLite-backed store of chats and participants, scoped per agent."""

    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            from olano.config.home import get_olano_paths

            db_path = get_olano_paths().chat_directory_db_path
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(_CREATE_CHATS)
            conn.execute(_CREATE_PARTICIPANTS)
            conn.execute(_CREATE_OBSERVED)
            conn.execute(_CREATE_OBSERVED_INDEX)
            conn.commit()

    # ------------------------------------------------------------- recording

    def record_chat(
        self,
        agent: str,
        platform: str,
        chat_id: str,
        *,
        chat_name: str = "",
        chat_type: str = "dm",
        origin: str = "message",
        bump_count: bool = True,
        active: bool | None = None,
    ) -> None:
        """Insert or refresh one chat row.

        A non-empty ``chat_name`` always overwrites (names change and the
        latest wins); an empty one never erases a known name. ``active`` is
        only touched when explicitly passed so an enumeration refresh cannot
        resurrect a chat the bot was kicked from.
        """
        agent, platform, chat_id = str(agent), str(platform), str(chat_id)
        if not agent or not platform or not chat_id:
            return
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO chats (agent, platform, chat_id, chat_name, chat_type,
                                   first_seen, last_seen, msg_count, active, origin)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (agent, platform, chat_id) DO UPDATE SET
                    chat_name = CASE WHEN excluded.chat_name != ''
                                     THEN excluded.chat_name ELSE chats.chat_name END,
                    chat_type = CASE WHEN excluded.chat_type != ''
                                     THEN excluded.chat_type ELSE chats.chat_type END,
                    last_seen = excluded.last_seen,
                    msg_count = chats.msg_count + ?,
                    active    = COALESCE(?, chats.active)
                """,
                (
                    agent,
                    platform,
                    chat_id,
                    str(chat_name or ""),
                    str(chat_type or ""),
                    now,
                    now,
                    1 if bump_count else 0,
                    1 if (active is None or active) else 0,
                    origin,
                    1 if bump_count else 0,
                    None if active is None else (1 if active else 0),
                ),
            )
            conn.commit()

    def record_participant(
        self,
        agent: str,
        platform: str,
        user_id: str,
        *,
        username: str = "",
        display_name: str = "",
        is_bot: bool = False,
        chat_id: str = "",
    ) -> None:
        agent, platform, user_id = str(agent), str(platform), str(user_id)
        if not agent or not platform or not user_id:
            return
        now = _now()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO participants (agent, platform, user_id, username,
                                          display_name, is_bot, last_chat_id,
                                          first_seen, last_seen, msg_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT (agent, platform, user_id) DO UPDATE SET
                    username     = CASE WHEN excluded.username != ''
                                        THEN excluded.username ELSE participants.username END,
                    display_name = CASE WHEN excluded.display_name != ''
                                        THEN excluded.display_name
                                        ELSE participants.display_name END,
                    is_bot       = excluded.is_bot,
                    last_chat_id = CASE WHEN excluded.last_chat_id != ''
                                        THEN excluded.last_chat_id
                                        ELSE participants.last_chat_id END,
                    last_seen    = excluded.last_seen,
                    msg_count    = participants.msg_count + 1
                """,
                (
                    agent,
                    platform,
                    user_id,
                    str(username or "").lstrip("@"),
                    str(display_name or ""),
                    1 if is_bot else 0,
                    str(chat_id or ""),
                    now,
                    now,
                ),
            )
            conn.commit()

    def record_message(self, agent: str, platform: str, source: SessionSource) -> None:
        """Record one inbound message's chat + sender (+ forum topic)."""
        self.record_chat(
            agent,
            platform,
            source.chat_id,
            chat_name=source.chat_name or "",
            chat_type=source.chat_type or "dm",
            origin="message",
        )
        # Telegram forum topics are stable, named sub-chats and directly
        # addressable (target "<chat_id>:<thread_id>") — record each as its
        # own row so "post it in the Events topic" is a name lookup. Only
        # Telegram: Slack's thread_ts would mint a row per threaded
        # conversation (noise), and a Discord thread already arrives as its
        # own channel-like chat.
        if platform == "telegram" and source.thread_id and (source.chat_type or "dm") != "dm":
            topic = (source.chat_topic or "").strip()
            group = (source.chat_name or "").strip()
            name = f"{group} / {topic}" if group and topic else topic
            self.record_chat(
                agent,
                platform,
                f"{source.chat_id}:{source.thread_id}",
                chat_name=name,
                chat_type="topic",
                origin="message",
            )
        if source.user_id:
            self.record_participant(
                agent,
                platform,
                source.user_id,
                username=source.username or "",
                display_name=source.user_name or "",
                is_bot=bool(source.is_bot),
                chat_id=source.chat_id,
            )

    def record_membership(
        self,
        agent: str,
        platform: str,
        chat_id: str,
        *,
        chat_name: str = "",
        chat_type: str = "group",
        joined: bool = True,
    ) -> None:
        """Record the bot being added to (or removed from) a chat."""
        self.record_chat(
            agent,
            platform,
            chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            origin="membership",
            bump_count=False,
            active=joined,
        )

    def record_migration(self, agent: str, platform: str, old_id: str, new_id: str) -> None:
        """Record a platform-side chat id change (Telegram supergroup upgrade).

        The old row is kept (inactive, pointing at its successor) so stale ids
        stored in prompts or schedules still resolve to the new chat.
        """
        old_id, new_id = str(old_id), str(new_id)
        if not old_id or not new_id or old_id == new_id:
            return
        with self._conn() as conn:
            row = conn.execute(
                "SELECT chat_name, chat_type FROM chats"
                " WHERE agent = ? AND platform = ? AND chat_id = ?",
                (agent, platform, old_id),
            ).fetchone()
            conn.execute(
                "UPDATE chats SET migrated_to = ?, active = 0, last_seen = ?"
                " WHERE agent = ? AND platform = ? AND chat_id = ?",
                (new_id, _now(), agent, platform, old_id),
            )
            conn.commit()
        self.record_chat(
            agent,
            platform,
            new_id,
            chat_name=(row["chat_name"] if row else "") or "",
            chat_type=(row["chat_type"] if row else "") or "group",
            origin="membership",
            bump_count=False,
            active=True,
        )

    def record_enumerated(self, agent: str, platform: str, chats: list[dict[str, Any]]) -> None:
        """Merge a native ``list_chats()`` result into the directory."""
        for entry in chats or []:
            chat_id = str(entry.get("chat_id") or entry.get("id") or "").strip()
            if not chat_id:
                continue
            self.record_chat(
                agent,
                platform,
                chat_id,
                chat_name=str(entry.get("name") or ""),
                chat_type=str(entry.get("type") or "group"),
                origin="enumeration",
                bump_count=False,
            )

    # ------------------------------------------------------ observed messages

    def record_observed(
        self,
        agent: str,
        platform: str,
        *,
        chat_id: str,
        text: str,
        thread_id: str = "",
        sender_id: str = "",
        sender_name: str = "",
        max_per_chat: int = OBSERVED_MAX_PER_CHAT,
        max_age_days: int = OBSERVED_RETENTION_DAYS,
    ) -> None:
        """Store one group message the agent saw but did not answer.

        Retention is enforced on write: each chat keeps at most
        ``max_per_chat`` rows and nothing older than ``max_age_days`` — the
        store is a bounded rolling window, never an unbounded archive.
        """
        agent, platform, chat_id = str(agent), str(platform), str(chat_id)
        body = str(text or "").strip()
        if not agent or not platform or not chat_id or not body:
            return
        if len(body) > OBSERVED_MAX_TEXT_CHARS:
            body = body[:OBSERVED_MAX_TEXT_CHARS] + "…"
        max_per_chat = max(1, int(max_per_chat))
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(max_age_days)))
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO observed_messages
                    (agent, platform, chat_id, thread_id, sender_id, sender_name, text, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent,
                    platform,
                    chat_id,
                    str(thread_id or ""),
                    str(sender_id or ""),
                    str(sender_name or ""),
                    body,
                    _now(),
                ),
            )
            conn.execute(
                """
                DELETE FROM observed_messages
                WHERE agent = ? AND platform = ? AND chat_id = ?
                  AND id NOT IN (
                      SELECT id FROM observed_messages
                      WHERE agent = ? AND platform = ? AND chat_id = ?
                      ORDER BY id DESC LIMIT ?
                  )
                """,
                (agent, platform, chat_id, agent, platform, chat_id, max_per_chat),
            )
            conn.execute(
                "DELETE FROM observed_messages WHERE agent = ? AND ts < ?",
                (agent, cutoff.isoformat()),
            )
            conn.commit()

    def recent_observed(
        self,
        agent: str,
        *,
        chat_id: str = "",
        thread_id: str = "",
        platform: str = "",
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        """The newest observed messages, returned oldest-first for display."""
        sql = "SELECT * FROM observed_messages WHERE agent = ?"
        params: list[Any] = [agent]
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        if chat_id:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        if thread_id:
            sql += " AND thread_id = ?"
            params.append(thread_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._conn() as conn:
            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        rows.reverse()
        return rows

    def search_observed(
        self,
        agent: str,
        query: str,
        *,
        chat_id: str = "",
        platform: str = "",
        since_days: int = 0,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Observed messages whose text or sender matches *query* (newest first)."""
        query = str(query or "").strip()
        if not query:
            return []
        escaped = f"%{_like_escape(query)}%"
        sql = (
            "SELECT * FROM observed_messages WHERE agent = ?"
            " AND (text LIKE ? ESCAPE '\\' OR sender_name LIKE ? ESCAPE '\\')"
        )
        params: list[Any] = [agent, escaped, escaped]
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        if chat_id:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        if since_days and int(since_days) > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=int(since_days))
            sql += " AND ts >= ?"
            params.append(cutoff.isoformat())
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def purge_observed(self, agent: str, *, chat_id: str = "", platform: str = "") -> int:
        """Delete observed messages (whole agent, one platform, or one chat)."""
        sql = "DELETE FROM observed_messages WHERE agent = ?"
        params: list[Any] = [agent]
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        if chat_id:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        with self._conn() as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
            return int(cursor.rowcount or 0)

    # --------------------------------------------------------------- lookups

    def find_chats(
        self,
        agent: str,
        query: str,
        *,
        platform: str = "",
        chat_type: str = "",
        limit: int = 10,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        """Rank-matched chats for a free-text query (name, id, or fragment)."""
        query = str(query or "").strip()
        if not query:
            return self.list_chats(agent, platform=platform, chat_type=chat_type, limit=limit)
        escaped = _like_escape(query)
        sql = """
            SELECT *, CASE
                WHEN chat_id = :q THEN 0
                WHEN LOWER(chat_name) = LOWER(:q) THEN 1
                WHEN chat_name LIKE :prefix ESCAPE '\\' THEN 2
                WHEN chat_name LIKE :sub ESCAPE '\\' THEN 3
                ELSE 4 END AS rank
            FROM chats
            WHERE agent = :agent
              AND (chat_id = :q
                   OR chat_name LIKE :sub ESCAPE '\\')
        """
        params: dict[str, Any] = {
            "agent": agent,
            "q": query,
            "prefix": f"{escaped}%",
            "sub": f"%{escaped}%",
        }
        if platform:
            sql += " AND platform = :platform"
            params["platform"] = platform
        if chat_type:
            sql += " AND chat_type = :chat_type"
            params["chat_type"] = chat_type
        if not include_inactive:
            sql += " AND (active = 1 OR chat_id = :q)"
        sql += " ORDER BY rank ASC, last_seen DESC LIMIT :limit"
        params["limit"] = max(1, int(limit))
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def list_chats(
        self,
        agent: str,
        *,
        platform: str = "",
        chat_type: str = "",
        limit: int = 50,
        include_inactive: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM chats WHERE agent = ?"
        params: list[Any] = [agent]
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        if chat_type:
            sql += " AND chat_type = ?"
            params.append(chat_type)
        if not include_inactive:
            sql += " AND active = 1"
        sql += " ORDER BY last_seen DESC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def find_users(
        self,
        agent: str,
        query: str,
        *,
        platform: str = "",
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        query = str(query or "").strip().lstrip("@")
        if not query:
            return []
        escaped = _like_escape(query)
        sql = """
            SELECT *, CASE
                WHEN user_id = :q THEN 0
                WHEN LOWER(username) = LOWER(:q) THEN 1
                WHEN LOWER(display_name) = LOWER(:q) THEN 2
                WHEN username LIKE :sub ESCAPE '\\' THEN 3
                WHEN display_name LIKE :sub ESCAPE '\\' THEN 4
                ELSE 5 END AS rank
            FROM participants
            WHERE agent = :agent
              AND (user_id = :q
                   OR username LIKE :sub ESCAPE '\\'
                   OR display_name LIKE :sub ESCAPE '\\')
        """
        params: dict[str, Any] = {"agent": agent, "q": query, "sub": f"%{escaped}%"}
        if platform:
            sql += " AND platform = :platform"
            params["platform"] = platform
        sql += " ORDER BY rank ASC, last_seen DESC LIMIT :limit"
        params["limit"] = max(1, int(limit))
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_chat(self, agent: str, platform: str, chat_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM chats WHERE agent = ? AND platform = ? AND chat_id = ?",
                (agent, platform, str(chat_id)),
            ).fetchone()
            return dict(row) if row else None

    def resolve_chat_id(
        self, agent: str, platform: str, name: str
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Resolve a chat *name* to its id — only when unambiguous.

        Returns ``(chat_id, candidates)``: a resolved id follows any recorded
        migration chain; on ambiguity or no match the id is ``None`` and the
        candidates say why. Exact (case-insensitive) name matches shadow
        substring matches, so "team" resolves to the chat literally named
        "team" even when "team announcements" also exists.
        """
        matches = self.find_chats(agent, name, platform=platform, limit=10)
        # find_chats also matches by chat_id — this resolver is for names.
        named = [m for m in matches if (m.get("chat_name") or "").strip()]
        exact = [m for m in named if m["chat_name"].strip().lower() == name.strip().lower()]
        pool = exact or named
        if len(pool) == 1:
            return self._follow_migration(agent, platform, pool[0]), matches
        return None, matches

    def _follow_migration(self, agent: str, platform: str, row: dict[str, Any]) -> str:
        """Chase ``migrated_to`` pointers (bounded — a cycle stops the walk)."""
        chat_id = str(row.get("chat_id") or "")
        seen = {chat_id}
        current = row
        for _ in range(5):
            nxt = str(current.get("migrated_to") or "").strip()
            if not nxt or nxt in seen:
                return chat_id
            seen.add(nxt)
            chat_id = nxt
            found = self.get_chat(agent, platform, nxt)
            if found is None:
                return chat_id
            current = found
        return chat_id


# ─── Module-level singleton + best-effort recorder ──────────────────────────

_directory: ChatDirectory | None = None
_directory_path: Path | None = None
_directory_lock = threading.Lock()

#: (agent, platform, chat_id, user_id) → (monotonic ts, name signature).
_recent_writes: dict[tuple[str, str, str, str], tuple[float, str]] = {}


def get_chat_directory() -> ChatDirectory:
    """Process-wide directory, re-created when the Olano home moves (tests)."""
    global _directory, _directory_path
    from olano.config.home import get_olano_paths

    path = get_olano_paths().chat_directory_db_path
    with _directory_lock:
        if _directory is None or _directory_path != path:
            _directory = ChatDirectory(path)
            _directory_path = path
        return _directory


def _should_write(key: tuple[str, str, str, str], name_sig: str) -> bool:
    now = time.monotonic()
    last = _recent_writes.get(key)
    if last is not None and (now - last[0]) < _WRITE_THROTTLE_SECONDS and last[1] == name_sig:
        return False
    if len(_recent_writes) > 4096:  # crude bound; correctness never depends on it
        _recent_writes.clear()
    _recent_writes[key] = (now, name_sig)
    return True


def record_message_event(adapter: Any, event: Any) -> None:
    """Best-effort capture of one inbound message into the directory.

    Called from ``BasePlatformAdapter.handle_message`` for every message that
    reaches an agent. Must never raise and never block the event loop: the
    SQLite write is throttled per chat/user and handed to a worker thread when
    a loop is running.
    """
    try:
        agent = str(getattr(adapter, "agent_name", "") or "")
        source = getattr(event, "source", None)
        if not agent or source is None or not getattr(source, "chat_id", ""):
            return
        platform = getattr(getattr(source, "platform", None), "value", "") or ""
        if not platform:
            return
        # thread_id keys the throttle too, so the first message in each forum
        # topic always writes (it creates the topic row); chat_topic joins the
        # name signature so a late topic-name discovery bypasses the window.
        key = (
            agent,
            platform,
            f"{source.chat_id}:{getattr(source, 'thread_id', '') or ''}",
            str(source.user_id or ""),
        )
        name_sig = "\x00".join(
            (
                source.chat_name or "",
                source.user_name or "",
                source.username or "",
                getattr(source, "chat_topic", "") or "",
            )
        )
        if not _should_write(key, name_sig):
            return

        def _write() -> None:
            try:
                get_chat_directory().record_message(agent, platform, source)
            except Exception:
                logger.debug("chat-directory write failed", exc_info=True)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.run_in_executor(None, _write)
        else:
            _write()
    except Exception:
        logger.debug("chat-directory capture failed", exc_info=True)


def record_membership_event(
    adapter: Any,
    chat_id: str,
    *,
    chat_name: str = "",
    chat_type: str = "group",
    joined: bool = True,
) -> None:
    """Best-effort membership capture (bot added to / removed from a chat)."""
    try:
        agent = str(getattr(adapter, "agent_name", "") or "")
        platform = getattr(getattr(adapter, "platform", None), "value", "") or ""
        if not agent or not platform or not chat_id:
            return
        get_chat_directory().record_membership(
            agent,
            platform,
            str(chat_id),
            chat_name=chat_name,
            chat_type=chat_type,
            joined=joined,
        )
    except Exception:
        logger.debug("chat-directory membership capture failed", exc_info=True)


def record_migration_event(adapter: Any, old_id: str, new_id: str) -> None:
    """Best-effort chat-id migration capture (Telegram supergroup upgrade)."""
    try:
        agent = str(getattr(adapter, "agent_name", "") or "")
        platform = getattr(getattr(adapter, "platform", None), "value", "") or ""
        if not agent or not platform:
            return
        get_chat_directory().record_migration(agent, platform, str(old_id), str(new_id))
    except Exception:
        logger.debug("chat-directory migration capture failed", exc_info=True)


class _SourceEvent:
    """Minimal event shim so a bare SessionSource reuses the metadata recorder."""

    __slots__ = ("source",)

    def __init__(self, source: Any) -> None:
        self.source = source


def record_observed_event(adapter: Any, source: Any, text: str) -> None:
    """Best-effort persist of one group message that will NOT get an agent turn.

    Called from adapter drop-branches when the connector's ``group_listen``
    mode says to record. Never raises and never blocks the event loop; the
    write is handed to a worker thread when a loop is running. Also refreshes
    the chat-directory metadata for the chat/sender (throttled), so listened
    chats stay resolvable by name even if the bot is never addressed there.
    """
    try:
        agent = str(getattr(adapter, "agent_name", "") or "")
        if not agent or source is None or not getattr(source, "chat_id", ""):
            return
        platform = getattr(getattr(source, "platform", None), "value", "") or ""
        body = " ".join(str(text or "").split())
        if not platform or not body:
            return
        extra = getattr(getattr(adapter, "config", None), "extra", None) or {}
        try:
            max_per_chat = int(extra.get("group_listen_max_messages") or OBSERVED_MAX_PER_CHAT)
        except (TypeError, ValueError):
            max_per_chat = OBSERVED_MAX_PER_CHAT
        try:
            max_age_days = int(extra.get("group_listen_retention_days") or OBSERVED_RETENTION_DAYS)
        except (TypeError, ValueError):
            max_age_days = OBSERVED_RETENTION_DAYS

        def _write() -> None:
            try:
                get_chat_directory().record_observed(
                    agent,
                    platform,
                    chat_id=str(source.chat_id),
                    thread_id=str(getattr(source, "thread_id", "") or ""),
                    sender_id=str(getattr(source, "user_id", "") or ""),
                    sender_name=str(
                        getattr(source, "user_name", "") or getattr(source, "username", "") or ""
                    ),
                    text=body,
                    max_per_chat=max_per_chat,
                    max_age_days=max_age_days,
                )
            except Exception:
                logger.debug("observed-message write failed", exc_info=True)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.run_in_executor(None, _write)
        else:
            _write()
        # Keep the name↔id directory fresh for silently-listened chats too.
        record_message_event(adapter, _SourceEvent(source))
    except Exception:
        logger.debug("observed-message capture failed", exc_info=True)
