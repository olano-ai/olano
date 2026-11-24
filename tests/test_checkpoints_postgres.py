"""Integration tests for the opt-in Postgres checkpoint/store backend.

Skipped unless BOTH the ``postgres`` extra (psycopg + langgraph-postgres) is
installed AND a Postgres is reachable — either ``LIONCLAW_TEST_POSTGRES_URL`` is
set (CI/external), or ``testcontainers`` + a running Docker spin one up. SQLite is
the default everywhere else, so the rest of the suite never touches Postgres.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest

from olano.config.home import is_postgres_available

try:
    from testcontainers.postgres import PostgresContainer

    _HAS_TESTCONTAINERS = True
except Exception:  # pragma: no cover - optional dev dep
    _HAS_TESTCONTAINERS = False

_EXTERNAL_URL = os.environ.get("LIONCLAW_TEST_POSTGRES_URL")

# The extra under test must actually be importable; otherwise the backend raises
# "Postgres extra is not installed" the moment a test touches it. CI installs
# testcontainers (so the reachability gate alone passes) but not the postgres
# extra, so without this the whole module errors instead of skipping.
pytestmark = pytest.mark.skipif(
    not (is_postgres_available() and (_HAS_TESTCONTAINERS or _EXTERNAL_URL)),
    reason=(
        'needs the postgres extra (uv add "olano[postgres]") plus '
        "LIONCLAW_TEST_POSTGRES_URL or testcontainers[postgres] + Docker"
    ),
)


def _to_libpq(url: str) -> str:
    """Normalise a SQLAlchemy-style URL (postgresql+psycopg2://) to libpq form."""
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


@pytest.fixture(scope="module")
def pg_url():
    """A reachable Postgres connection URL, or skip the module."""
    if _EXTERNAL_URL:
        yield _to_libpq(_EXTERNAL_URL)
        return
    if not _HAS_TESTCONTAINERS:  # pragma: no cover
        pytest.skip("testcontainers not installed")
    try:
        container = PostgresContainer("postgres:16")
        container.start()
    except Exception as exc:  # pragma: no cover - Docker not running
        pytest.skip(f"could not start Postgres testcontainer (Docker?): {exc}")
    try:
        yield _to_libpq(container.get_connection_url())
    finally:
        container.stop()


def _set_ckpt_env(monkeypatch, url: str) -> None:
    monkeypatch.setenv("LIONCLAW_CHECKPOINT_BACKEND", "postgres")
    monkeypatch.setenv("LIONCLAW_CHECKPOINT_DB_URL", url)


def _set_store_env(monkeypatch, url: str) -> None:
    monkeypatch.setenv("LIONCLAW_STORE_BACKEND", "postgres")
    monkeypatch.setenv("LIONCLAW_STORE_DB_URL", url)


def _cfg(thread_id: str, ns: str = ""):
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns}}


# ── checkpointer ─────────────────────────────────────────────────────────────


async def test_postgres_checkpoint_round_trip(pg_url, monkeypatch) -> None:
    from langgraph.checkpoint.base import empty_checkpoint

    from olano.core.checkpoints import CheckpointerFactory

    _set_ckpt_env(monkeypatch, pg_url)
    factory = CheckpointerFactory()
    try:
        saver = await factory.aget("agent-x")
        cfg = _cfg("t-round-trip")
        await saver.aput(cfg, empty_checkpoint(), {}, {})
        got = await saver.aget_tuple(cfg)
        assert got is not None and got.checkpoint is not None
    finally:
        await factory.aclose()


def test_postgres_per_loop_safety(pg_url, monkeypatch) -> None:
    """Two separate asyncio.run loops must each get a working saver.

    The factory keys async pools by ``(url, loop-id)``, so a transient loop never
    reuses a pool bound to a dead one — guarding against
    "Future attached to a different loop".
    """
    from langgraph.checkpoint.base import empty_checkpoint

    from olano.core.checkpoints import CheckpointerFactory

    _set_ckpt_env(monkeypatch, pg_url)
    factory = CheckpointerFactory()

    async def _once(tid: str) -> bool:
        saver = await factory.aget("agent-x")
        cfg = _cfg(tid)
        await saver.aput(cfg, empty_checkpoint(), {}, {})
        return (await saver.aget_tuple(cfg)) is not None

    try:
        assert asyncio.run(_once("loop-a")) is True
        assert asyncio.run(_once("loop-b")) is True  # fresh loop — must not raise
    finally:
        asyncio.run(factory.aclose())


async def test_postgres_aclose_then_rebuild(pg_url, monkeypatch) -> None:
    from olano.core.checkpoints import CheckpointerFactory

    _set_ckpt_env(monkeypatch, pg_url)
    factory = CheckpointerFactory()
    try:
        first = await factory.aget("agent-x")
        assert first is not None
        await factory.aclose()
        # Cache cleared → a fresh saver builds without error.
        second = await factory.aget("agent-x")
        assert second is not None
    finally:
        await factory.aclose()


async def test_postgres_aclose_agent_is_noop(pg_url, monkeypatch) -> None:
    """Ephemeral-worker eviction is a harmless no-op on the pooled backend."""
    from olano.core.checkpoints import CheckpointerFactory

    _set_ckpt_env(monkeypatch, pg_url)
    factory = CheckpointerFactory()
    try:
        await factory.aget("agent__cog__x__abc")
        await factory.aclose_agent("agent__cog__x__abc")  # must not raise / not tear down pool
        saver = await factory.aget("agent-x")
        assert saver is not None
    finally:
        await factory.aclose()


# ── store ────────────────────────────────────────────────────────────────────


async def test_postgres_store_round_trip(pg_url, monkeypatch) -> None:
    from olano.core.stores import StoreFactory

    _set_store_env(monkeypatch, pg_url)
    factory = StoreFactory()
    try:
        store = await factory.aget()
        await store.aput(("vfs",), "key1", {"v": 1})
        item = await store.aget(("vfs",), "key1")
        assert item is not None and item.value == {"v": 1}
    finally:
        await factory.aclose()


# ── threadcost dual-dialect reader ─────────────────────────────────────────────


async def test_threadcost_reader_on_postgres(pg_url, monkeypatch) -> None:
    from langchain_core.messages import AIMessage
    from langgraph.checkpoint.base import empty_checkpoint

    from olano.analytics.threadcost import reader
    from olano.core.checkpoints import CheckpointerFactory

    _set_ckpt_env(monkeypatch, pg_url)

    # Seed a thread whose latest checkpoint carries an AIMessage with usage.
    factory = CheckpointerFactory()
    try:
        saver = await factory.aget("agent-x")
        msg = AIMessage(
            content="hi",
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            response_metadata={"model_name": "anthropic:claude-sonnet-4-6"},
        )
        chk = empty_checkpoint()
        chk["channel_values"]["messages"] = [msg]
        # new_versions must list the channel so its blob is actually persisted.
        chk["channel_versions"]["messages"] = "1"
        cfg = _cfg("t-cost")
        await saver.aput(cfg, chk, {}, {"messages": "1"})
    finally:
        await factory.aclose()

    # The dual-dialect reader resolves postgres from env and reads it back.
    handle = reader.open_thread_db()
    try:
        assert handle.backend == "postgres"
        assert "t-cost" in reader.list_thread_ids(handle)
        callset = reader.read_thread_callset(handle, "t-cost")
        calls = callset.all_calls()
        assert len(calls) == 1
        assert calls[0].input_tokens == 10 and calls[0].output_tokens == 5
    finally:
        handle.close()


def test_thread_store_available_postgres(pg_url, monkeypatch) -> None:
    from olano.analytics.threadcost import reader

    _set_ckpt_env(monkeypatch, pg_url)
    assert reader.thread_store_available() is True


# ── migration ──────────────────────────────────────────────────────────────────


def test_migrate_sqlite_to_postgres(pg_url, monkeypatch, tmp_path: Path) -> None:
    import sqlite3

    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.sqlite import SqliteSaver

    from olano.core.checkpoint_migrate import migrate_sqlite_to_postgres
    from olano.core.checkpoints import CheckpointerFactory

    # Seed a legacy SQLite threads.db.
    src = tmp_path / "threads.db"
    conn = sqlite3.connect(str(src))
    s = SqliteSaver(conn)
    s.setup()
    cfg = _cfg("t-migrate")
    s.put(cfg, empty_checkpoint(), {}, {})
    conn.close()

    result = migrate_sqlite_to_postgres(source_db=src, dest_url=pg_url, log=lambda _m: None)
    assert result.failed_threads == 0
    assert result.checkpoints >= 1

    # The migrated thread is resumable from Postgres.
    _set_ckpt_env(monkeypatch, pg_url)
    factory = CheckpointerFactory()
    try:
        saver = factory.get("agent-x")  # sync postgres saver
        assert saver.get_tuple(cfg) is not None
    finally:
        asyncio.run(factory.aclose())


def test_migrate_dry_run_writes_nothing(pg_url, tmp_path: Path) -> None:
    import sqlite3

    from langgraph.checkpoint.base import empty_checkpoint
    from langgraph.checkpoint.sqlite import SqliteSaver

    from olano.core.checkpoint_migrate import migrate_sqlite_to_postgres

    src = tmp_path / "threads.db"
    conn = sqlite3.connect(str(src))
    s = SqliteSaver(conn)
    s.setup()
    s.put(_cfg("t-dry"), empty_checkpoint(), {}, {})
    conn.close()

    result = migrate_sqlite_to_postgres(
        source_db=src, dest_url=pg_url, dry_run=True, log=lambda _m: None
    )
    assert result.dry_run is True
    assert result.checkpoints >= 1
    assert result.failed_threads == 0
