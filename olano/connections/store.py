"""SQLite store for customer↔customer connections and invite codes.

One DB per agent at ``<home>/agents/<agent>/connections.db`` — deliberately
OUTSIDE the agent's workspace, so neither the backend file tools nor the
Docker sandbox (which mounts the workspace, never the home) can read or forge
a connection row. The only write paths are the identity-injected tools in
``connections/tools.py`` and the ``forget_me`` erasure hook.

Rows reference customers by their folder key (``<platform>-<id>``). Peer keys
are never surfaced to the other side by the tools — each side sees only the
display labels chosen at invite/accept time.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

#: Unambiguous, uppercase-only alphabet (no 0/O, 1/I/L) — the code survives
#: being read aloud, retyped from a screenshot, or autocorrected.
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE_LENGTH = 8

_SCHEMA = """
CREATE TABLE IF NOT EXISTS invites (
    code         TEXT PRIMARY KEY,
    inviter_key  TEXT NOT NULL,
    label        TEXT NOT NULL DEFAULT '',
    introduce_as TEXT NOT NULL DEFAULT '',
    max_uses     INTEGER NOT NULL DEFAULT 1,
    uses         INTEGER NOT NULL DEFAULT 0,
    revoked      INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    inviter_key  TEXT NOT NULL,
    peer_key     TEXT NOT NULL,
    inviter_label TEXT NOT NULL,  -- what the INVITER calls this peer
    peer_label    TEXT NOT NULL,  -- what the PEER calls the inviter
    status       TEXT NOT NULL DEFAULT 'active',
    invite_code  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    UNIQUE(inviter_key, peer_key)
);
CREATE TABLE IF NOT EXISTS sends (
    connection_id INTEGER NOT NULL,
    sender_key    TEXT NOT NULL,
    sent_on       TEXT NOT NULL,
    count         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (connection_id, sender_key, sent_on)
);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def default_db_path(agent_name: str) -> Path:
    from olano.config.home import get_olano_paths

    agent_dir = get_olano_paths().agent_workspace_dir(agent_name).parent
    return agent_dir / "connections.db"


class ConnectionsStore:
    """All reads/writes for one agent's connections DB (thread-safe per call)."""

    def __init__(self, agent_name: str, db_path: Path | None = None) -> None:
        self.agent_name = agent_name
        self.path = Path(db_path) if db_path else default_db_path(agent_name)

    def _conn(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        return conn

    # ── invites ────────────────────────────────────────────────────────────

    def create_invite(
        self,
        inviter_key: str,
        *,
        label: str,
        introduce_as: str,
        max_uses: int,
        ttl_hours: int,
    ) -> tuple[str, str]:
        """Mint an invite; returns ``(code, expires_at_iso)``."""
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
        code = f"{code[:4]}-{code[4:]}"
        expires = _iso(_utcnow() + timedelta(hours=max(1, ttl_hours)))
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO invites (code, inviter_key, label, introduce_as, max_uses,"
                " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (code, inviter_key, label, introduce_as, max_uses, _iso(_utcnow()), expires),
            )
        return code, expires

    def invites_for(self, inviter_key: str) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM invites WHERE inviter_key = ? AND revoked = 0"
                    " AND uses < max_uses AND expires_at > ? ORDER BY created_at",
                    (inviter_key, _iso(_utcnow())),
                )
            )

    def revoke_invite(self, inviter_key: str, code: str) -> bool:
        """Revoke one of the CALLER's own invites (never anyone else's)."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE invites SET revoked = 1 WHERE code = ? AND inviter_key = ?",
                (code.strip().upper(), inviter_key),
            )
            return cur.rowcount > 0

    def redeem(
        self, code: str, redeemer_key: str, *, your_name: str, max_connections: int
    ) -> tuple[str, sqlite3.Row | None]:
        """Accept an invite as *redeemer_key*.

        Returns ``("ok", connection_row)`` or ``(reason, None)`` where reason
        is one of ``unknown``, ``expired``, ``self``, ``already_connected``,
        ``cap`` — generic enough not to leak whether a guessed code ever
        existed (``unknown`` covers revoked/used-up/nonexistent alike).
        """
        code = code.strip().upper()
        with self._conn() as conn:
            inv = conn.execute("SELECT * FROM invites WHERE code = ?", (code,)).fetchone()
            if inv is None or inv["revoked"] or inv["uses"] >= inv["max_uses"]:
                return "unknown", None
            if inv["expires_at"] <= _iso(_utcnow()):
                return "expired", None
            if inv["inviter_key"] == redeemer_key:
                return "self", None
            pair = conn.execute(
                "SELECT * FROM connections WHERE status = 'active' AND"
                " ((inviter_key = ? AND peer_key = ?) OR (inviter_key = ? AND peer_key = ?))",
                (inv["inviter_key"], redeemer_key, redeemer_key, inv["inviter_key"]),
            ).fetchone()
            if pair is not None:
                return "already_connected", None
            mine = conn.execute(
                "SELECT COUNT(*) FROM connections WHERE status = 'active' AND"
                " (inviter_key = ? OR peer_key = ?)",
                (redeemer_key, redeemer_key),
            ).fetchone()[0]
            if mine >= max_connections:
                return "cap", None

            # The inviter's label for this peer: the invitee's self-chosen
            # name when given, else the invite's label (multi-use invites get
            # a joined-date suffix so redemptions stay distinguishable
            # WITHOUT ever showing the peer's platform id).
            inviter_label = (your_name or "").strip()
            if not inviter_label:
                inviter_label = (inv["label"] or "connection").strip()
                if inv["max_uses"] > 1:
                    inviter_label += f" (joined {_utcnow().date().isoformat()})"
            peer_label = (inv["introduce_as"] or "the person who invited you").strip()

            conn.execute("UPDATE invites SET uses = uses + 1 WHERE code = ?", (code,))
            cur = conn.execute(
                "INSERT INTO connections (inviter_key, peer_key, inviter_label,"
                " peer_label, invite_code, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    inv["inviter_key"],
                    redeemer_key,
                    inviter_label,
                    peer_label,
                    code,
                    _iso(_utcnow()),
                ),
            )
            row = conn.execute(
                "SELECT * FROM connections WHERE id = ?", (cur.lastrowid,)
            ).fetchone()
            return "ok", row

    # ── connections ─────────────────────────────────────────────────────────

    def connections_for(self, key: str) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM connections WHERE status = 'active' AND"
                    " (inviter_key = ? OR peer_key = ?) ORDER BY created_at",
                    (key, key),
                )
            )

    def find_connection(self, key: str, ref: str) -> sqlite3.Row | None:
        """Resolve *ref* (label as seen by *key*, or #id) among *key*'s rows only."""
        ref = (ref or "").strip().lstrip("#")
        matches = []
        for row in self.connections_for(key):
            label = row["inviter_label"] if row["inviter_key"] == key else row["peer_label"]
            if ref == str(row["id"]) or ref.lower() == label.lower():
                matches.append(row)
        return matches[0] if len(matches) == 1 else None

    def disconnect(self, key: str, connection_id: int) -> bool:
        """Sever a connection — either side may, and only a side may."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE connections SET status = 'ended' WHERE id = ? AND status = 'active'"
                " AND (inviter_key = ? OR peer_key = ?)",
                (connection_id, key, key),
            )
            return cur.rowcount > 0

    # ── send accounting ──────────────────────────────────────────────────────

    def record_send(self, connection_id: int, sender_key: str) -> int:
        """Increment today's send count for one direction; returns the new count."""
        today = _utcnow().date().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sends (connection_id, sender_key, sent_on, count)"
                " VALUES (?, ?, ?, 1)"
                " ON CONFLICT(connection_id, sender_key, sent_on)"
                " DO UPDATE SET count = count + 1",
                (connection_id, sender_key, today),
            )
            row = conn.execute(
                "SELECT count FROM sends WHERE connection_id = ? AND sender_key = ?"
                " AND sent_on = ?",
                (connection_id, sender_key, today),
            ).fetchone()
            return int(row["count"])

    def sends_today(self, connection_id: int, sender_key: str) -> int:
        today = _utcnow().date().isoformat()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT count FROM sends WHERE connection_id = ? AND sender_key = ?"
                " AND sent_on = ?",
                (connection_id, sender_key, today),
            ).fetchone()
            return int(row["count"]) if row else 0

    # ── erasure ──────────────────────────────────────────────────────────────

    def erase_customer(self, key: str) -> int:
        """Remove every trace of one customer: their invites and connections."""
        with self._conn() as conn:
            a = conn.execute("DELETE FROM invites WHERE inviter_key = ?", (key,)).rowcount
            rows = list(
                conn.execute(
                    "SELECT id FROM connections WHERE inviter_key = ? OR peer_key = ?",
                    (key, key),
                )
            )
            for row in rows:
                conn.execute("DELETE FROM sends WHERE connection_id = ?", (row["id"],))
            b = conn.execute(
                "DELETE FROM connections WHERE inviter_key = ? OR peer_key = ?", (key, key)
            ).rowcount
            return a + b


def erase_customer_connections(agent_name: str, customer_key: str) -> int:
    """forget_me hook: best-effort, returns rows removed (0 when no DB yet)."""
    store = ConnectionsStore(agent_name)
    if not store.path.exists():
        return 0
    try:
        return store.erase_customer(customer_key)
    except sqlite3.Error:
        logger.warning("Could not erase connections for %s", customer_key, exc_info=True)
        return 0
