"""SQLite-backed entitlement store: one subscription record per end-user.

Why SQLite (and not the file-per-user layout of ``memory/customer_memory.py``):
entitlement has a **concurrent-writer** problem the memory store doesn't — the
Stripe webhook (runtime loop) and an in-flight message turn can touch the same
record at once, and the dashboard process may read it too. So we mirror the
discipline of ``core/checkpoints.py``: a single on-disk DB, every connection
opened with a generous ``busy_timeout`` so a contending writer *waits* instead
of raising ``database is locked``.

Keyed by ``(agent, channel, user_id)``. ``user_id`` is sanitized through the
same :func:`~olano.memory.customer_memory._sanitize_id` used for per-customer
memory, so a hostile id can never break the key. A second ``processed_events``
table gives webhook idempotency (Stripe/Stars retry the same event).

The concrete class sits behind the small :class:`EntitlementStore` surface so a
Postgres backend can be added later (mirroring ``resolve_checkpoint_backend()``)
without touching the gate/service callers. SQLite is the day-one default.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from olano.memory.customer_memory import _sanitize_id, sanitize_identity

logger = logging.getLogger(__name__)

# Match the checkpointer's busy timeout: a contending writer WAITS up to 30s for
# the single SQLite write lock instead of erroring the instant it's contended.
_BUSY_TIMEOUT_MS = 30_000


class EntitlementStatus(str, Enum):
    """Lifecycle of one end-user's access to a monetized agent."""

    NONE = "none"  # never seen / no entitlement
    TRIAL = "trial"  # inside the free trial window
    ACTIVE = "active"  # paid, current
    PAST_DUE = "past_due"  # renewal payment failed, in grace
    CANCELED = "canceled"  # will not renew; access until current_period_end
    EXPIRED = "expired"  # trial lapsed or paid period ended → blocked


@dataclass
class EntitlementRecord:
    """One end-user's subscription state for a single ``(agent, channel)``."""

    agent: str
    channel: str
    user_id: str
    status: EntitlementStatus = EntitlementStatus.NONE
    trial_start: float | None = None
    current_period_end: float | None = None
    provider: str = ""
    external_customer_id: str = ""
    external_sub_id: str = ""
    updated_at: float = 0.0


def _row_to_record(row: sqlite3.Row) -> EntitlementRecord:
    return EntitlementRecord(
        agent=row["agent"],
        channel=row["channel"],
        user_id=row["user_id"],
        status=EntitlementStatus(row["status"]),
        trial_start=row["trial_start"],
        current_period_end=row["current_period_end"],
        provider=row["provider"] or "",
        external_customer_id=row["external_customer_id"] or "",
        external_sub_id=row["external_sub_id"] or "",
        updated_at=row["updated_at"] or 0.0,
    )


class EntitlementStore:
    """Thread-safe SQLite store of per-end-user entitlement records.

    All operations open a short-lived connection with ``busy_timeout`` set, so
    the store is safe to share across the webhook path, the message-handler
    gate, and a separate dashboard process. A process-local lock serializes our
    own writers; SQLite's busy timeout covers cross-process contention.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or self._default_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._ensure_schema()

    @staticmethod
    def _default_path() -> Path:
        from olano.config.home import get_olano_home

        return get_olano_home() / "monetization" / "entitlements.db"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _ensure_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entitlements (
                    agent TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    trial_start REAL,
                    current_period_end REAL,
                    provider TEXT,
                    external_customer_id TEXT,
                    external_sub_id TEXT,
                    updated_at REAL,
                    PRIMARY KEY (agent, channel, user_id)
                )
                """
            )
            # Idempotency: a provider's retried webhook delivery is ignored once
            # its event id has been recorded here.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_events (
                    provider TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    ts REAL,
                    PRIMARY KEY (provider, event_id)
                )
                """
            )
            # Reverse lookup for provider webhooks that carry only a subscription
            # id (invoice.paid, subscription.updated/deleted have no client
            # reference back to the chat user).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ent_sub ON entitlements (provider, external_sub_id)"
            )

    # ── reads ─────────────────────────────────────────────────────────────
    def get(self, agent: str, channel: str, user_id: str) -> EntitlementRecord | None:
        uid = sanitize_identity(user_id)
        if not uid:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM entitlements WHERE agent=? AND channel=? AND user_id=?",
                (agent, channel, uid),
            ).fetchone()
            if row is None:
                # Rows written before the injective encoding were keyed by the
                # lossy `_sanitize_id` spelling (e.g. a WhatsApp JID
                # `123@s.whatsapp.net` stored as `123_s_whatsapp_net`). Without
                # this fallback every such subscriber would look first-seen —
                # re-trialed and then paywalled despite paying. Migrate the row
                # to the new key on first read so webhooks and the gate agree
                # on one row from here on.
                legacy = _sanitize_id(user_id)
                if legacy and legacy != uid:
                    row = conn.execute(
                        "SELECT * FROM entitlements WHERE agent=? AND channel=? AND user_id=?",
                        (agent, channel, legacy),
                    ).fetchone()
                    if row is not None:
                        try:
                            conn.execute(
                                "UPDATE entitlements SET user_id=? "
                                "WHERE agent=? AND channel=? AND user_id=?",
                                (uid, agent, channel, legacy),
                            )
                        except sqlite3.IntegrityError:
                            # `self._lock` serializes THIS process, but the
                            # store is shared with a separate dashboard
                            # process: another writer can insert the new-key
                            # row between our miss above and this UPDATE. That
                            # row is fresher than the legacy one (typically a
                            # webhook just landed), so prefer it and leave the
                            # legacy row where find_by_subscription can still
                            # see it.
                            logger.warning(
                                "Entitlement re-key collided for agent=%s channel=%s; "
                                "preferring the concurrently written row",
                                agent,
                                channel,
                            )
                            fresh = conn.execute(
                                "SELECT * FROM entitlements "
                                "WHERE agent=? AND channel=? AND user_id=?",
                                (agent, channel, uid),
                            ).fetchone()
                            return _row_to_record(fresh) if fresh else _row_to_record(row)
                        record = _row_to_record(row)
                        record.user_id = uid
                        return record
        return _row_to_record(row) if row else None

    def find_by_subscription(self, provider: str, external_sub_id: str) -> EntitlementRecord | None:
        """Resolve the record a provider event refers to by subscription id."""
        if not external_sub_id:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM entitlements WHERE provider=? AND external_sub_id=? LIMIT 1",
                (provider, external_sub_id),
            ).fetchone()
        return _row_to_record(row) if row else None

    def entitled_user_ids(self, agent: str, channel: str, *, now: float | None = None) -> list[str]:
        """User ids currently allowed to receive pushes (active or valid trial).

        The trial cutoff can't be expressed in SQL without the per-agent
        ``trial_days``, so callers that need trial precision should filter with
        the gate; this returns everyone whose stored status is serviceable and
        whose paid period (if any) hasn't ended. Good enough for "don't push to
        canceled/expired users" — the twice-daily fan-out.
        """
        now = time.time() if now is None else now
        serviceable = (
            EntitlementStatus.ACTIVE.value,
            EntitlementStatus.TRIAL.value,
            EntitlementStatus.PAST_DUE.value,
            EntitlementStatus.CANCELED.value,
        )
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT user_id, current_period_end FROM entitlements "
                f"WHERE agent=? AND channel=? AND status IN "
                f"({','.join('?' * len(serviceable))})",
                (agent, channel, *serviceable),
            ).fetchall()
        out = []
        for row in rows:
            end = row["current_period_end"]
            if end is None or end > now:
                out.append(row["user_id"])
        return out

    # ── writes ────────────────────────────────────────────────────────────
    def upsert(self, record: EntitlementRecord) -> EntitlementRecord:
        uid = sanitize_identity(record.user_id)
        if not uid:
            raise ValueError("cannot store entitlement for an empty/unusable user_id")
        record.user_id = uid
        record.updated_at = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO entitlements
                    (agent, channel, user_id, status, trial_start, current_period_end,
                     provider, external_customer_id, external_sub_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent, channel, user_id) DO UPDATE SET
                    status=excluded.status,
                    trial_start=excluded.trial_start,
                    current_period_end=excluded.current_period_end,
                    provider=excluded.provider,
                    external_customer_id=excluded.external_customer_id,
                    external_sub_id=excluded.external_sub_id,
                    updated_at=excluded.updated_at
                """,
                (
                    record.agent,
                    record.channel,
                    record.user_id,
                    record.status.value,
                    record.trial_start,
                    record.current_period_end,
                    record.provider,
                    record.external_customer_id,
                    record.external_sub_id,
                    record.updated_at,
                ),
            )
        return record

    def claim_event(self, provider: str, event_id: str) -> bool:
        """Record a webhook event id once; return True the first time only.

        Used so a provider retrying the same delivery doesn't double-apply. An
        empty ``event_id`` is always claimable (nothing to dedupe on).
        """
        if not event_id:
            return True
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO processed_events (provider, event_id, ts) VALUES (?, ?, ?)",
                (provider, event_id, time.time()),
            )
            return cur.rowcount > 0


_store: EntitlementStore | None = None
_store_lock = threading.Lock()


def get_entitlement_store() -> EntitlementStore:
    """Return the process-wide entitlement store (lazily created)."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = EntitlementStore()
    return _store


def reset_entitlement_store(store: EntitlementStore | None = None) -> None:
    """Swap/clear the singleton — for tests that point at a tmp DB."""
    global _store
    with _store_lock:
        _store = store
