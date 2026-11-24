"""SQLite-backed user store.

Mirrors the structure of ``olano/scheduler/db.py`` (synchronous ``sqlite3``,
one connection per call). Holds local human accounts, login sessions, and the
many-to-many human↔agent access mapping that powers per-agent authorization.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)

DEFAULT_SESSION_TTL_DAYS = 14

# Which dashboard shell a human sees after login. This is a **presentation**
# knob and nothing else: it is deliberately NOT derived from ``is_admin`` and
# is never consulted for authorization. Every real boundary stays where it is
# (``is_admin``, ``_member_forbidden``, ``assert_agent_access``), so a wrong
# value here costs a user the wrong-looking page, never access they lack.
UI_MODE_ADVANCED = "advanced"
UI_MODE_SIMPLE = "simple"
UI_MODES = (UI_MODE_ADVANCED, UI_MODE_SIMPLE)
DEFAULT_UI_MODE = UI_MODE_ADVANCED


def normalize_ui_mode(value: Any) -> str:
    """Clamp to a known UI mode; anything unrecognized reads as the default.

    The default is ``advanced`` — the surface every existing human already
    has — so a pre-migration row, a bad write, or a mode a future build knows
    and this one does not can never silently take the dashboard away.
    """
    return value if value in UI_MODES else DEFAULT_UI_MODE


_CREATE_USERS = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    username      TEXT,
    name          TEXT,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER DEFAULT 0,
    is_active     INTEGER DEFAULT 1,
    can_terminal  INTEGER DEFAULT 0,
    ui_mode       TEXT DEFAULT 'advanced',
    langfuse_links INTEGER DEFAULT 0,
    timezone      TEXT DEFAULT 'UTC',
    created_at    TEXT NOT NULL
)
"""

_CREATE_SESSIONS = """
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen  TEXT
)
"""

_CREATE_ACCESS = """
CREATE TABLE IF NOT EXISTS agent_access (
    user_id    TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    role       TEXT DEFAULT 'member',   -- 'owner' | 'member'
    granted_by TEXT,
    granted_at TEXT NOT NULL,
    PRIMARY KEY (user_id, agent_name)
)
"""

_CREATE_INVITES = """
CREATE TABLE IF NOT EXISTS user_invites (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_by  TEXT,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL,
    accepted_at TEXT
)
"""

# Per-user UI/client preferences, stored as a single JSON blob (e.g. nav order,
# layout toggles). Keyed by user_id with no FK so the synthetic single-user
# "owner" principal (absent from the users table) can persist prefs too.
_CREATE_PREFS = """
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id    TEXT PRIMARY KEY,
    prefs      TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
)
"""

_CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_access_agent ON agent_access(agent_name)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_invites_user ON user_invites(user_id)",
)

# `username` is the human's stable handle — the thing `human-<username>` is
# built from. SQLite cannot add a UNIQUE column via ALTER, so uniqueness is an
# index; it is partial because a row mid-migration is legitimately NULL and a
# plain unique index would treat two NULLs as distinct anyway.
_CREATE_USERNAME_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username "
    "ON users(username) WHERE username IS NOT NULL"
)


# The handle `quick-setup` burns for the account every managed box is
# provisioned with (`cli/quick_setup.py`). `claim_provisioned_account` keys on it:
# a row still carrying this handle with its temporary password in play is the
# one the onboarding wizard lets the owner take over as their own.
PROVISIONED_ADMIN_HANDLE = "admin"


def humanize_handle(username: str) -> str:
    """A presentable display name derived from a handle: ``dana-ray`` → ``Dana Ray``.

    Used only when an account has no name at all — a slug shown as a person's
    Name is exactly the ugliness the name/handle split exists to avoid. The
    inverse of :func:`olano.issues.principals.slugify` only in spirit: this is
    presentation, never an identity, so nothing may ever key off its output.
    """
    return " ".join(part.capitalize() for part in username.split("-") if part) or username


def mint_username(desired: str, taken: Iterable[str]) -> str:
    """Slugify *desired* into a handle not already in *taken*.

    Collisions get a numeric suffix (``carlos-m``, ``carlos-m-2``, …). Shared
    by the backfill and by ``create_user`` so a handle minted today and one
    minted during the migration follow the same rule — two implementations of
    "what do we call this person" is how they end up disagreeing.
    """
    from olano.issues.principals import slugify

    base = slugify(desired)
    seen = {t for t in taken if t}
    if base not in seen:
        return base
    n = 2
    while f"{base}-{n}" in seen:
        n += 1
    return f"{base}-{n}"


class DuplicateEmailError(ValueError):
    """Raised when creating a user with an email that already exists."""


class DuplicateUsernameError(ValueError):
    """Raised when a minted handle was taken by a concurrent writer."""


@dataclass
class User:
    """A local human account."""

    id: str
    email: str
    name: str
    password_hash: str
    is_admin: bool
    is_active: bool
    timezone: str
    created_at: str
    can_terminal: bool = False
    # Stable public handle — what ``human-<username>`` is built from, and the
    # reason `name` is free to change. Never editable after creation; see
    # ``update_user``'s allowlist.
    username: str = ""
    ui_mode: str = DEFAULT_UI_MODE
    # The person's position ("Head of Sales"), shown under their name — the
    # same field an agent carries as `org.title`, so an org chart can label
    # humans and agents alike. Free text, editable, empty by default; nothing
    # keys off it.
    title: str = ""
    # Whether the dashboard shows this person "Langfuse" links (on a reply,
    # on an Insights thread). Off for every account by default, existing ones
    # included; an admin turns it on per person in Admin -> Users. Display
    # only: the links route stays admin-only whatever this says.
    langfuse_links: bool = False

    def public(self) -> dict[str, Any]:
        """Serializable view safe to return over the API (no password hash)."""
        return {
            "id": self.id,
            "email": self.email,
            "username": self.username,
            "name": self.name,
            "title": self.title,
            "is_admin": self.is_admin,
            "is_active": self.is_active,
            "can_terminal": self.can_terminal,
            "ui_mode": normalize_ui_mode(self.ui_mode),
            "langfuse_links": self.langfuse_links,
            "timezone": self.timezone,
            "created_at": self.created_at,
        }


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


class UsersDB:
    """SQLite store for users, sessions, and agent access."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or get_olano_paths().users_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ----------------------------------------------------------------- init

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(_CREATE_USERS)
            conn.execute(_CREATE_SESSIONS)
            conn.execute(_CREATE_ACCESS)
            conn.execute(_CREATE_INVITES)
            conn.execute(_CREATE_PREFS)
            for stmt in _CREATE_INDEXES:
                conn.execute(stmt)
            # Idempotent column add for DBs created before per-user terminal
            # access existed (pre-release; no formal migration framework).
            try:
                conn.execute("ALTER TABLE users ADD COLUMN can_terminal INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # column already present
            try:
                conn.execute("ALTER TABLE users ADD COLUMN username TEXT")
            except sqlite3.OperationalError:
                pass  # column already present
            self._backfill_usernames(conn)
            conn.execute(_CREATE_USERNAME_INDEX)
            self._backfill_names(conn)
            # Same idempotent add for the simple/advanced shell selector. The
            # DEFAULT backfills every existing row to 'advanced', so upgrading
            # a live box changes nobody's dashboard.
            try:
                conn.execute("ALTER TABLE users ADD COLUMN ui_mode TEXT DEFAULT 'advanced'")
            except sqlite3.OperationalError:
                pass  # column already present
            # The position (see User.title). Same idempotent add; the DEFAULT
            # leaves every existing account untitled rather than NULL.
            try:
                conn.execute("ALTER TABLE users ADD COLUMN title TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # column already present
            # Langfuse links (see User.langfuse_links). The DEFAULT leaves
            # every existing account with them off.
            try:
                conn.execute("ALTER TABLE users ADD COLUMN langfuse_links INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # column already present
            conn.commit()

    def _backfill_usernames(self, conn: sqlite3.Connection) -> None:
        """Give every pre-existing row the handle its principal already used.

        Before ``username`` existed, ``human_principal()`` computed
        ``slugify(user.name)`` fresh on every call. Every ``human-*`` string
        already written into ``issues.db`` — an append-only audit log — was
        built that way. So the backfill must reproduce exactly that slug, or
        the migration silently orphans every issue on the box.

        Rows are processed oldest-first so the result is reproducible and the
        longest-standing account keeps the unsuffixed handle. Two names that
        slugified alike used to *collapse* into one principal; they now get
        distinct handles, which is the fix, but it does move the newer of the
        two — hence the warning.
        """
        rows = conn.execute(
            "SELECT id, name FROM users "
            "WHERE username IS NULL OR username = '' ORDER BY created_at ASC, id ASC"
        ).fetchall()
        if not rows:
            return
        taken = {
            r[0]
            for r in conn.execute(
                "SELECT username FROM users WHERE username IS NOT NULL AND username != ''"
            ).fetchall()
            if r[0]
        }
        for row in rows:
            # Strictly `name`, even when blank — no email-local-part fallback
            # here. slugify("") is the literal "user", so a nameless account's
            # principal is already `human-user`; minting something prettier
            # would orphan whatever was filed against it. New accounts get the
            # nicer fallback in `create_user`, where there is no history to keep.
            desired = (row["name"] or "").strip()
            username = mint_username(desired, taken)
            if username != mint_username(desired, set()):
                logger.warning(
                    "users: handle collision on %r — user %s takes %r; "
                    "issues previously filed against the shared principal stay "
                    "with the older account",
                    desired,
                    row["id"],
                    username,
                )
            taken.add(username)
            conn.execute("UPDATE users SET username = ? WHERE id = ?", (username, row["id"]))

    def _backfill_names(self, conn: sqlite3.Connection) -> None:
        """Fill blank display names from the handle (``user-2`` → ``User 2``).

        MUST run **after** ``_backfill_usernames``: handles are minted as
        ``slugify(name)`` over the *original* names so already-stored
        ``human-*`` principals survive byte-identical, and a name invented
        here must never feed that mint. The direction is one-way — handle →
        presentable name — and only for rows that have no name at all.
        """
        rows = conn.execute(
            "SELECT id, username FROM users "
            "WHERE (name IS NULL OR trim(name) = '') "
            "  AND username IS NOT NULL AND username != ''"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE users SET name = ? WHERE id = ?",
                (humanize_handle(row["username"]), row["id"]),
            )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ----------------------------------------------------------------- users

    def create_user(
        self,
        email: str,
        password_hash: str,
        name: str = "",
        is_admin: bool = False,
        timezone: str = "UTC",
        is_active: bool = True,
        username: str = "",
        ui_mode: str = DEFAULT_UI_MODE,
        title: str = "",
    ) -> User:
        """Create a user and return it. Raises DuplicateEmailError on conflict.

        ``is_active=False`` marks a pending (invited) account that cannot log in
        until the invite is accepted and a password is set.

        ``username`` is the account's permanent handle, and the two ways of
        getting one are deliberately different. **Left empty** it is minted
        from *name* (or the email local-part for a nameless invite), suffixed
        ``-2``, ``-3`` until unique — nobody asked for a specific handle, so a
        quiet suffix is fine. **Typed explicitly** it is slugified (a handle
        can never carry a character ``human-<username>`` couldn't) and, if
        taken, **refused** with :class:`DuplicateUsernameError` — silently
        handing someone ``carlos-m-2`` when they chose ``carlos-m`` would
        saddle them with the wrong permanent identity and never tell them.

        ``name`` is never left empty either: a blank one is derived from the
        minted handle (``dana-ray`` → ``Dana Ray``), so every creation path —
        invites, quick-setup, the magic-link bootstrap, the CLI — yields an
        account with both halves filled, from this one choke point.
        """
        user = User(
            id=str(uuid.uuid4()),
            email=_normalize_email(email),
            name=name or "",
            password_hash=password_hash,
            is_admin=is_admin,
            is_active=is_active,
            timezone=timezone or "UTC",
            created_at=_now_iso(),
            ui_mode=normalize_ui_mode(ui_mode),
            title=(title or "").strip(),
        )
        explicit = (username or "").strip()
        try:
            with self._conn() as conn:
                taken = {
                    r[0]
                    for r in conn.execute(
                        "SELECT username FROM users WHERE username IS NOT NULL"
                    ).fetchall()
                    if r[0]
                }
                if explicit:
                    from olano.issues.principals import slugify

                    candidate = slugify(explicit)
                    if candidate in taken:
                        raise DuplicateUsernameError(f"username already taken: {candidate}")
                    user.username = candidate
                else:
                    desired = (name or "").strip() or user.email.split("@", 1)[0]
                    user.username = mint_username(desired, taken)
                if not user.name.strip():
                    user.name = humanize_handle(user.username)
                conn.execute(
                    """
                    INSERT INTO users (id, email, username, name, password_hash,
                                       is_admin, is_active, timezone, created_at,
                                       ui_mode, title)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user.id,
                        user.email,
                        user.username,
                        user.name,
                        user.password_hash,
                        int(user.is_admin),
                        int(user.is_active),
                        user.timezone,
                        user.created_at,
                        user.ui_mode,
                        user.title,
                    ),
                )
                conn.commit()
        except sqlite3.IntegrityError as exc:
            # Two unique constraints can fire here now. The handle is minted
            # against the same connection that inserts, so a username collision
            # means a concurrent writer took it between the two statements —
            # report that honestly instead of blaming the email.
            if "username" in str(exc):
                raise DuplicateUsernameError(f"username already taken: {user.username}") from exc
            raise DuplicateEmailError(f"email already exists: {user.email}") from exc
        logger.debug("Created user %s (%s, @%s)", user.id, user.email, user.username)
        return user

    def get_user_by_id(self, user_id: str) -> User | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_user(row)

    def get_user_by_email(self, email: str) -> User | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?", (_normalize_email(email),)
            ).fetchone()
        return _row_to_user(row)

    def list_users(self) -> list[User]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at ASC").fetchall()
        return [u for u in (_row_to_user(r) for r in rows) if u is not None]

    def count_users(self) -> int:
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def count_admins(self, *, active_only: bool = True) -> int:
        query = "SELECT COUNT(*) FROM users WHERE is_admin = 1"
        if active_only:
            query += " AND is_active = 1"
        with self._conn() as conn:
            return int(conn.execute(query).fetchone()[0])

    def update_user(self, user_id: str, **fields: Any) -> bool:
        # `username` is deliberately absent and must stay absent: every
        # `human-<username>` already written into the issues audit log would
        # orphan the moment a handle moved. `name` is the editable one — that
        # split is the whole point of having both.
        allowed = {
            "name",
            "title",
            "is_admin",
            "is_active",
            "can_terminal",
            "ui_mode",
            "langfuse_links",
            "timezone",
            "password_hash",
            "email",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        # Unlike `name`, a title may be cleared: "no position" is a valid answer.
        if "title" in updates:
            updates["title"] = str(updates["title"] or "").strip()
        # A name may change but never vanish — the never-empty invariant. The
        # API layer 422s this with a message; here it is silently dropped so a
        # blank slipping through any other caller can't blank a stored name.
        if "name" in updates and not str(updates["name"] or "").strip():
            updates.pop("name")
        if not updates:
            return False
        if "is_admin" in updates:
            updates["is_admin"] = int(bool(updates["is_admin"]))
        if "is_active" in updates:
            updates["is_active"] = int(bool(updates["is_active"]))
        if "can_terminal" in updates:
            updates["can_terminal"] = int(bool(updates["can_terminal"]))
        if "ui_mode" in updates:
            updates["ui_mode"] = normalize_ui_mode(updates["ui_mode"])
        if "langfuse_links" in updates:
            updates["langfuse_links"] = int(bool(updates["langfuse_links"]))
        if "email" in updates:
            updates["email"] = _normalize_email(updates["email"])
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [user_id]
        try:
            with self._conn() as conn:
                cur = conn.execute(f"UPDATE users SET {set_clause} WHERE id = ?", values)
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise DuplicateEmailError("email already exists") from exc
        return cur.rowcount > 0

    def claim_provisioned_account(self, user_id: str, *, name: str, password_hash: str) -> User:
        """Turn the provisioned ``admin`` row into the owner's own account.

        The ONE sanctioned handle rewrite. ``update_user`` refuses ``username``
        because every ``human-<username>`` already written into the issues
        audit log would orphan the moment a handle moved — and that rule is
        exactly why the wizard could not simply rename Admin before. It does not
        bite here: the first-run wizard gates the whole app, so the provisioned
        row has filed nothing under ``human-admin`` by the time the owner
        claims it, and the claim is single-shot (the caller closes it by
        stamping the password rotation).

        A managed box is provisioned with the owner's OWN email on this row
        (``cli/quick_setup.py``), and ``users.email`` is unique — so creating a
        second account for the same person was never possible, and offering to
        forced them to invent a second address. Claiming keeps ``id``, ``email``,
        ``is_admin`` and every ``agent_access`` row (keyed on the id) untouched;
        only the three things the owner chooses — name, handle, password — move.

        The handle is minted from the name by the same rule as creation
        (``mint_username``), with the row's own current handle excluded from
        ``taken`` so a name that slugifies to ``admin`` keeps ``admin`` rather
        than becoming ``admin-2``. A blank name raises ``ValueError``; an
        unknown id raises ``KeyError``.
        """
        clean = (name or "").strip()
        if not clean:
            raise ValueError("name_required")
        with self._conn() as conn:
            if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
                raise KeyError(user_id)
            taken = {
                r[0]
                for r in conn.execute(
                    "SELECT username FROM users WHERE username IS NOT NULL AND id != ?",
                    (user_id,),
                ).fetchall()
                if r[0]
            }
            handle = mint_username(clean, taken)
            conn.execute(
                "UPDATE users SET name = ?, username = ?, password_hash = ? WHERE id = ?",
                (clean, handle, password_hash, user_id),
            )
            conn.commit()
        logger.info("Provisioned account %s claimed as @%s", user_id, handle)
        user = self.get_user_by_id(user_id)
        assert user is not None  # existence checked inside the transaction
        return user

    def delete_user(self, user_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM agent_access WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM user_invites WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM user_prefs WHERE user_id = ?", (user_id,))
            conn.commit()
        # Their phones must stop hearing about agents they no longer exist for.
        # The registry lives outside this database (olano.push.store), so it is
        # cleaned here, after the commit, best-effort.
        try:
            from olano.push.store import get_device_store

            get_device_store().delete_user(user_id)
        except Exception:
            logger.debug("could not drop push devices for a deleted user", exc_info=True)
        return cur.rowcount > 0

    # --------------------------------------------------------------- prefs

    def get_prefs(self, user_id: str) -> dict[str, Any]:
        """Return a user's preferences blob (empty dict if none / unparseable)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT prefs FROM user_prefs WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return {}
        try:
            data = json.loads(row["prefs"])
        except (json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def set_prefs(self, user_id: str, prefs: dict[str, Any]) -> dict[str, Any]:
        """Replace a user's entire preferences blob and return it."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO user_prefs (user_id, prefs, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    prefs = excluded.prefs,
                    updated_at = excluded.updated_at
                """,
                (user_id, json.dumps(prefs), _now_iso()),
            )
            conn.commit()
        return prefs

    def merge_prefs(self, user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Shallow-merge ``patch`` into the stored prefs and return the result.

        Lets a client update one key (e.g. ``nav_order``) without clobbering
        preferences it doesn't know about.
        """
        current = self.get_prefs(user_id)
        current.update(patch)
        return self.set_prefs(user_id, current)

    # --------------------------------------------------------------- sessions

    def create_session(
        self,
        user_id: str,
        token_hash: str,
        ttl_days: int = DEFAULT_SESSION_TTL_DAYS,
    ) -> str:
        """Persist a session and return its ISO expiry."""
        created = _now()
        expires_at = (created + timedelta(days=ttl_days)).isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO sessions (token_hash, user_id, created_at, expires_at, last_seen)
                VALUES (?, ?, ?, ?, ?)
                """,
                (token_hash, user_id, created.isoformat(), expires_at, created.isoformat()),
            )
            conn.commit()
        return expires_at

    def get_session(self, token_hash: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT token_hash, user_id, created_at, expires_at, last_seen "
                "FROM sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return dict(row) if row is not None else None

    def touch_session(self, token_hash: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET last_seen = ? WHERE token_hash = ?",
                (_now_iso(), token_hash),
            )
            conn.commit()

    def delete_session(self, token_hash: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            conn.commit()
        return cur.rowcount > 0

    def delete_sessions_for_user(self, user_id: str, *, keep_token_hash: str | None = None) -> int:
        """Revoke a user's sessions; ``keep_token_hash`` spares one (the caller's own).

        A password change that signs the changer out of the browser they made it
        from is a defect, and re-issuing their cookie is a second write with a
        Set-Cookie to get right — sparing the current token is the honest shape.
        """
        with self._conn() as conn:
            if keep_token_hash:
                cur = conn.execute(
                    "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                    (user_id, keep_token_hash),
                )
            else:
                cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.commit()
        return cur.rowcount

    def purge_expired(self) -> int:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE expires_at < ?", (_now_iso(),))
            conn.commit()
        return cur.rowcount

    # ----------------------------------------------------------------- access

    def grant_access(
        self,
        user_id: str,
        agent_name: str,
        role: str = "member",
        granted_by: str | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_access (user_id, agent_name, role, granted_by, granted_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, agent_name, role, granted_by, _now_iso()),
            )
            conn.commit()

    def set_owner(self, user_id: str, agent_name: str, granted_by: str | None = None) -> None:
        self.grant_access(user_id, agent_name, role="owner", granted_by=granted_by)

    def revoke_access(self, user_id: str, agent_name: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM agent_access WHERE user_id = ? AND agent_name = ?",
                (user_id, agent_name),
            )
            conn.commit()
        return cur.rowcount > 0

    def revoke_agent(self, agent_name: str) -> int:
        """Drop all access rows for an agent (e.g. when the agent is deleted)."""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM agent_access WHERE agent_name = ?", (agent_name,))
            conn.commit()
        return cur.rowcount

    def get_role(self, user_id: str, agent_name: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT role FROM agent_access WHERE user_id = ? AND agent_name = ?",
                (user_id, agent_name),
            ).fetchone()
        return row["role"] if row is not None else None

    def has_access(self, user_id: str, agent_name: str) -> bool:
        return self.get_role(user_id, agent_name) is not None

    def list_agents_for_user(self, user_id: str) -> list[dict[str, str]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT agent_name, role FROM agent_access WHERE user_id = ? ORDER BY agent_name",
                (user_id,),
            ).fetchall()
        return [{"agent_name": r["agent_name"], "role": r["role"]} for r in rows]

    def list_users_for_agent(self, agent_name: str) -> list[dict[str, str]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT user_id, role FROM agent_access WHERE agent_name = ? ORDER BY granted_at",
                (agent_name,),
            ).fetchall()
        return [{"user_id": r["user_id"], "role": r["role"]} for r in rows]

    def owner_of(self, agent_name: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT user_id FROM agent_access WHERE agent_name = ? AND role = 'owner' "
                "ORDER BY granted_at LIMIT 1",
                (agent_name,),
            ).fetchone()
        return row["user_id"] if row is not None else None

    # ----------------------------------------------------------------- invites

    def create_invite(
        self,
        user_id: str,
        token_hash: str,
        created_by: str | None = None,
        ttl_days: int = 7,
    ) -> str:
        """Replace any existing invite for the user and return the ISO expiry."""
        created = _now()
        expires_at = (created + timedelta(days=ttl_days)).isoformat()
        with self._conn() as conn:
            conn.execute("DELETE FROM user_invites WHERE user_id = ?", (user_id,))
            conn.execute(
                """
                INSERT INTO user_invites (token_hash, user_id, created_by, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (token_hash, user_id, created_by, created.isoformat(), expires_at),
            )
            conn.commit()
        return expires_at

    def get_invite(self, token_hash: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT token_hash, user_id, created_by, created_at, expires_at, accepted_at "
                "FROM user_invites WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
        return dict(row) if row is not None else None

    def mark_invite_accepted(self, token_hash: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE user_invites SET accepted_at = ? WHERE token_hash = ?",
                (_now_iso(), token_hash),
            )
            conn.commit()

    def has_open_invite(self, user_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM user_invites WHERE user_id = ? AND accepted_at IS NULL LIMIT 1",
                (user_id,),
            ).fetchone()
        return row is not None


# --------------------------------------------------------------------- helpers


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _row_to_user(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    return User(
        id=row["id"],
        email=row["email"],
        name=row["name"] or "",
        password_hash=row["password_hash"],
        is_admin=bool(row["is_admin"]),
        is_active=bool(row["is_active"]),
        timezone=row["timezone"] or "UTC",
        created_at=row["created_at"],
        # sqlite3.Row: `in row` checks values, so .keys() is required here.
        can_terminal=bool(row["can_terminal"]) if "can_terminal" in row.keys() else False,  # noqa: SIM118
        username=(row["username"] or "") if "username" in row.keys() else "",  # noqa: SIM118
        title=(row["title"] or "") if "title" in row.keys() else "",  # noqa: SIM118
        langfuse_links=(
            bool(row["langfuse_links"]) if "langfuse_links" in row.keys() else False  # noqa: SIM118
        ),
        ui_mode=(
            normalize_ui_mode(row["ui_mode"])
            if "ui_mode" in row.keys()  # noqa: SIM118
            else DEFAULT_UI_MODE
        ),
    )
