"""Entitlement store: round-trip, subscription lookup, and idempotency."""

from __future__ import annotations

from pathlib import Path

from olano.monetization.store import (
    EntitlementRecord,
    EntitlementStatus,
    EntitlementStore,
)


def _store(tmp_path: Path) -> EntitlementStore:
    return EntitlementStore(tmp_path / "e.db")


def test_upsert_and_get_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert(
        EntitlementRecord(
            agent="sicop",
            channel="telegram",
            user_id="42",
            status=EntitlementStatus.ACTIVE,
            current_period_end=2_000_000.0,
            provider="stripe",
            external_sub_id="sub_1",
        )
    )
    got = store.get("sicop", "telegram", "42")
    assert got is not None
    assert got.status is EntitlementStatus.ACTIVE
    assert got.external_sub_id == "sub_1"
    # Missing row → None.
    assert store.get("sicop", "telegram", "999") is None


def test_upsert_overwrites_same_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rec = EntitlementRecord(
        agent="a", channel="telegram", user_id="1", status=EntitlementStatus.TRIAL
    )
    store.upsert(rec)
    rec.status = EntitlementStatus.ACTIVE
    store.upsert(rec)
    assert store.get("a", "telegram", "1").status is EntitlementStatus.ACTIVE


def test_hostile_user_id_is_sanitized(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert(
        EntitlementRecord(
            agent="a",
            channel="telegram",
            user_id="../../etc/passwd",
            status=EntitlementStatus.TRIAL,
        )
    )
    # Stored under a sanitized key; the same hostile input resolves back to it.
    assert store.get("a", "telegram", "../../etc/passwd") is not None


def test_find_by_subscription(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert(
        EntitlementRecord(
            agent="a",
            channel="telegram",
            user_id="7",
            status=EntitlementStatus.ACTIVE,
            provider="stripe",
            external_sub_id="sub_x",
        )
    )
    found = store.find_by_subscription("stripe", "sub_x")
    assert found is not None and found.user_id == "7"
    assert store.find_by_subscription("stripe", "nope") is None
    assert store.find_by_subscription("stripe", "") is None


def test_claim_event_idempotency(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.claim_event("stripe", "evt_1") is True
    assert store.claim_event("stripe", "evt_1") is False  # already processed
    assert store.claim_event("stripe", "evt_2") is True
    # An empty event id is always claimable (nothing to dedupe on).
    assert store.claim_event("stripe", "") is True
    assert store.claim_event("stripe", "") is True


def test_entitled_user_ids_excludes_expired_and_lapsed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = 1_000_000.0
    store.upsert(
        EntitlementRecord(
            agent="a",
            channel="telegram",
            user_id="active",
            status=EntitlementStatus.ACTIVE,
            current_period_end=now + 100,
        )
    )
    store.upsert(
        EntitlementRecord(
            agent="a", channel="telegram", user_id="trial", status=EntitlementStatus.TRIAL
        )
    )
    store.upsert(
        EntitlementRecord(
            agent="a", channel="telegram", user_id="expired", status=EntitlementStatus.EXPIRED
        )
    )
    store.upsert(
        EntitlementRecord(
            agent="a",
            channel="telegram",
            user_id="lapsed",
            status=EntitlementStatus.ACTIVE,
            current_period_end=now - 100,
        )
    )
    ids = set(store.entitled_user_ids("a", "telegram", now=now))
    assert ids == {"active", "trial"}


def test_get_migrates_a_pre_injective_row_to_the_new_key(tmp_path: Path) -> None:
    """Rows written before ``sanitize_identity`` were keyed by the lossy
    ``_sanitize_id`` spelling (a WhatsApp JID ``123@s.whatsapp.net`` stored as
    ``123_s_whatsapp_net``). A lookup with the raw id must still find them —
    otherwise every such subscriber looks first-seen and gets re-trialed —
    and the row is re-keyed so future reads and writes agree."""
    import sqlite3

    from olano.memory.customer_memory import _sanitize_id, sanitize_identity

    raw = "12025550188@s.whatsapp.net"
    legacy_key = _sanitize_id(raw)
    new_key = sanitize_identity(raw)
    assert legacy_key != new_key  # the migration case actually exercised

    store = _store(tmp_path)
    # Seed the row exactly as the old code would have stored it.
    with sqlite3.connect(tmp_path / "e.db") as conn:
        conn.execute(
            "INSERT INTO entitlements (agent, channel, user_id, status) VALUES (?, ?, ?, ?)",
            ("a", "whatsapp", legacy_key, EntitlementStatus.ACTIVE.value),
        )

    got = store.get("a", "whatsapp", raw)
    assert got is not None
    assert got.status is EntitlementStatus.ACTIVE
    assert got.user_id == new_key
    # Migrated in place: one row, under the new key.
    with sqlite3.connect(tmp_path / "e.db") as conn:
        rows = conn.execute(
            "SELECT user_id FROM entitlements WHERE agent='a' AND channel='whatsapp'"
        ).fetchall()
    assert rows == [(new_key,)]
    # And the fast path now hits directly.
    assert store.get("a", "whatsapp", raw).user_id == new_key


def test_get_survives_a_rekey_collision_with_a_concurrent_writer(tmp_path: Path) -> None:
    """The re-key UPDATE can lose a race with a writer in ANOTHER process (the
    store is shared with the dashboard): between our new-key miss and the
    UPDATE, someone inserts the new-key row, and the UPDATE hits the composite
    PK. get() must return the fresher row rather than raise."""
    import sqlite3

    from olano.memory.customer_memory import _sanitize_id, sanitize_identity

    raw = "12025550188@s.whatsapp.net"
    legacy_key = _sanitize_id(raw)
    new_key = sanitize_identity(raw)

    store = _store(tmp_path)
    with sqlite3.connect(tmp_path / "e.db") as conn:
        conn.execute(
            "INSERT INTO entitlements (agent, channel, user_id, status) VALUES (?, ?, ?, ?)",
            ("a", "whatsapp", legacy_key, EntitlementStatus.EXPIRED.value),
        )

    # Simulate the cross-process interleaving: the moment get() issues its
    # re-key UPDATE, the new-key row already exists (a webhook just wrote it).
    # sqlite3.Connection attributes are read-only, so interpose with a proxy.
    real_connect = store._connect

    class _RacingConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *params):
            if sql.lstrip().startswith("UPDATE entitlements SET user_id"):
                self._conn.execute(
                    "INSERT INTO entitlements (agent, channel, user_id, status) "
                    "VALUES (?, ?, ?, ?)",
                    ("a", "whatsapp", new_key, EntitlementStatus.ACTIVE.value),
                )
            return self._conn.execute(sql, *params)

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, *exc):
            return self._conn.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def _racing_connect():
        return _RacingConnection(real_connect())

    store._connect = _racing_connect
    try:
        got = store.get("a", "whatsapp", raw)
    finally:
        store._connect = real_connect

    # The concurrently written (fresher) row wins; nothing raised.
    assert got is not None
    assert got.status is EntitlementStatus.ACTIVE
    assert got.user_id == new_key
