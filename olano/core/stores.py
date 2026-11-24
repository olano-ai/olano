"""Persistent LangGraph stores for Olano homes.

A ``StoreBackend`` ("Virtual File System (Persistent)" environment mode) needs a
``store=`` passed to ``create_deep_agent``. deepagents requires it — without one,
``get_store()`` resolves to ``None`` at runtime and any file operation crashes with
``AttributeError: 'NoneType' object has no attribute 'search'``.

This module mirrors :mod:`olano.core.checkpoints`: a single ``StoreFactory`` owns
the store cache for one process. The backend is resolved from the home config / env
(:func:`resolve_store_backend`) — ``sqlite`` (default) persists at
``<home>/sessions/store.db``; ``postgres`` shares a pooled store. Agents are isolated
by the store *namespace* (the agent name), so one database is shared across agents.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, cast

import aiosqlite
from langgraph.store.base import BaseStore
from langgraph.store.sqlite import SqliteStore
from langgraph.store.sqlite.aio import AsyncSqliteStore

from olano.config.home import (
    ensure_olano_home,
    get_olano_paths,
    is_postgres_available,
    load_home_config,
    resolve_store_backend,
)

logger = logging.getLogger(__name__)

# Postgres (optional backend) pool config — mirrors core/checkpoints.py so a
# single Postgres server can back both with consistent sizing.
_DEFAULT_PG_POOL_MAX = 20
_PG_CONN_KWARGS = {"autocommit": True, "prepare_threshold": 0}


def _pg_pool_max() -> int:
    raw = os.environ.get("LIONCLAW_CHECKPOINT_POOL_MAX", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return _DEFAULT_PG_POOL_MAX


def get_store_db_path(home: str | Path | None = None) -> Path:
    paths = ensure_olano_home(home)
    return get_olano_paths(paths.root).store_db_path


class StoreFactory:
    """Owns the store cache for one process.

    The backend is resolved per call (:func:`resolve_store_backend`). For SQLite,
    one store per ``db_path`` is shared across all agents (isolated via their store
    namespace); for Postgres, a single pooled store is shared process-wide.
    Registration is guarded by a re-entrant lock so racing coroutines/threads share
    a single store instead of leaking connections.
    """

    def __init__(self) -> None:
        self._sync_stores: dict[str, SqliteStore] = {}
        self._sync_conns: dict[str, sqlite3.Connection] = {}
        # Async sqlite stores are keyed by ``(db-path, loop-id)``:
        # AsyncSqliteStore holds an internal ``asyncio.Lock`` bound to the loop
        # that first uses it, so a store cached on the runtime loop crashes with
        # "is bound to a different event loop" when reused from a transient
        # ``asyncio.run`` loop (thread-origin scheduler run-now, one-shot CLI).
        self._async_stores: dict[tuple[str, int], AsyncSqliteStore] = {}
        self._async_conns: dict[tuple[str, int], aiosqlite.Connection] = {}
        # Postgres (optional). Async stores/pools keyed by ``(url, loop-id)`` (they
        # bind to the loop they open on); sync store/pool keyed by url alone.
        self._pg_async_stores: dict[tuple[str, int], BaseStore] = {}
        self._pg_async_pools: dict[tuple[str, int], Any] = {}
        self._pg_sync_stores: dict[str, BaseStore] = {}
        self._pg_sync_pools: dict[str, Any] = {}
        self._lock = threading.RLock()

    # -- backend resolution ----------------------------------------------
    def _resolve_backend(self, home: str | Path | None) -> tuple[str, str | None]:
        backend, url = resolve_store_backend(load_home_config(home))
        if backend == "postgres" and not is_postgres_available():
            raise RuntimeError(
                "store_backend=postgres but the Postgres extra is not installed. "
                'Install it with: uv add "olano[postgres]"'
            )
        return backend, url

    # -- sync ------------------------------------------------------------
    def get(self, home: str | Path | None = None) -> BaseStore:
        backend, url = self._resolve_backend(home)
        if backend == "postgres":
            assert url is not None  # _resolve_backend guarantees a url for postgres
            return self._get_postgres(url)
        db_path = get_store_db_path(home)
        key = str(db_path.resolve())
        with self._lock:
            store = self._sync_stores.get(key)
            if store is not None:
                return store
            # isolation_level=None (autocommit) is required: SqliteStore issues
            # explicit BEGIN statements, which clash with sqlite3's implicit
            # transaction management otherwise.
            conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
            store = SqliteStore(conn)
            store.setup()
            self._sync_conns[key] = conn
            self._sync_stores[key] = store
            return store

    # -- async -----------------------------------------------------------
    async def aget(self, home: str | Path | None = None) -> BaseStore:
        backend, url = self._resolve_backend(home)
        if backend == "postgres":
            assert url is not None  # _resolve_backend guarantees a url for postgres
            return await self._aget_postgres(url)
        db_path = get_store_db_path(home)
        key = (str(db_path.resolve()), id(asyncio.get_running_loop()))
        with self._lock:
            store = self._async_stores.get(key)
            if store is not None:
                return store
        # isolation_level=None (autocommit) — see the sync path for why.
        conn = await aiosqlite.connect(str(db_path), isolation_level=None)
        store = AsyncSqliteStore(conn)
        await store.setup()
        with self._lock:
            existing = self._async_stores.get(key)
            if existing is not None:
                # Lost the race — close the duplicate and return the winner.
                try:
                    await conn.close()
                except Exception as exc:
                    logger.debug("duplicate aiosqlite store close failed: %s", exc)
                return existing
            self._async_conns[key] = conn
            self._async_stores[key] = store
            return store

    # -- postgres --------------------------------------------------------
    def _get_postgres(self, url: str) -> BaseStore:
        """Return the shared synchronous ``PostgresStore`` over a pooled connection."""
        with self._lock:
            store = self._pg_sync_stores.get(url)
            if store is not None:
                return store
        from langgraph.store.postgres import PostgresStore
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            conninfo=url, max_size=_pg_pool_max(), min_size=1, open=False, kwargs=_PG_CONN_KWARGS
        )
        pool.open()
        store = PostgresStore(pool)  # type: ignore[arg-type]  # store sets its own row factory
        store.setup()
        with self._lock:
            existing = self._pg_sync_stores.get(url)
            if existing is not None:
                try:
                    pool.close()
                except Exception as exc:
                    logger.debug("duplicate pg sync store pool close failed: %s", exc)
                return existing
            self._pg_sync_pools[url] = pool
            self._pg_sync_stores[url] = store
            # store is Any (langgraph.store.postgres is untyped under
            # --ignore-missing-imports); narrow it back to the declared return type.
            return cast(BaseStore, store)

    async def _aget_postgres(self, url: str) -> BaseStore:
        """Return the shared async ``AsyncPostgresStore`` for the running loop.

        Keyed by ``(url, loop-id)`` so a transient ``asyncio.run`` loop gets its
        own pool rather than reusing one bound to a dead loop.
        """
        loop = asyncio.get_running_loop()
        key = (url, id(loop))
        with self._lock:
            store = self._pg_async_stores.get(key)
            if store is not None:
                return store
        from langgraph.store.postgres.aio import AsyncPostgresStore
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            conninfo=url, max_size=_pg_pool_max(), min_size=1, open=False, kwargs=_PG_CONN_KWARGS
        )
        await pool.open()
        store = AsyncPostgresStore(pool)  # type: ignore[arg-type]  # store sets its row factory
        await store.setup()
        with self._lock:
            existing = self._pg_async_stores.get(key)
            if existing is not None:
                try:
                    await pool.close()
                except Exception as exc:
                    logger.debug("duplicate pg async store pool close failed: %s", exc)
                return existing
            self._pg_async_pools[key] = (pool, loop)
            self._pg_async_stores[key] = store
            # store is Any (untyped postgres aio module); narrow to the return type.
            return cast(BaseStore, store)

    # -- lifecycle -------------------------------------------------------
    async def aclose(self) -> None:
        """Close all cached connections / pools (graceful shutdown)."""
        with self._lock:
            async_conns = list(self._async_conns.items())
            sync_conns = list(self._sync_conns.items())
            pg_async_pools = list(self._pg_async_pools.values())
            pg_sync_pools = list(self._pg_sync_pools.values())
            self._async_conns.clear()
            self._async_stores.clear()
            self._sync_conns.clear()
            self._sync_stores.clear()
            self._pg_async_pools.clear()
            self._pg_async_stores.clear()
            self._pg_sync_pools.clear()
            self._pg_sync_stores.clear()
        for key, aconn in async_conns:
            try:
                await aconn.close()
            except Exception as exc:
                logger.debug("aclose async store %s: %s", key, exc)
        for sync_key, sconn in sync_conns:
            try:
                sconn.close()
            except Exception as exc:
                logger.debug("aclose sync store %s: %s", sync_key, exc)
        running = asyncio.get_running_loop()
        for pool, loop in pg_async_pools:
            # Only closable on the loop it opened on; drop pools from dead
            # transient loops rather than crash closing cross-loop.
            if loop is running:
                try:
                    await pool.close()
                except Exception as exc:
                    logger.debug("aclose pg async store pool: %s", exc)
        for pool in pg_sync_pools:
            try:
                pool.close()
            except Exception as exc:
                logger.debug("aclose pg sync store pool: %s", exc)

    def reset(self) -> None:
        """Synchronous full reset — used by tests."""
        with self._lock:
            sync_conns = list(self._sync_conns.values())
            async_conns = list(self._async_conns.values())
            pg_sync_pools = list(self._pg_sync_pools.values())
            pg_async_pools = list(self._pg_async_pools.values())
            self._sync_conns.clear()
            self._sync_stores.clear()
            self._async_conns.clear()
            self._async_stores.clear()
            self._pg_sync_pools.clear()
            self._pg_sync_stores.clear()
            self._pg_async_pools.clear()
            self._pg_async_stores.clear()
        for sconn in sync_conns:
            try:
                sconn.close()
            except Exception as exc:
                logger.debug("reset sync store close failed: %s", exc)
        for aconn in async_conns:
            try:
                asyncio.run(aconn.close())
            except Exception as exc:
                logger.debug("reset async store close failed: %s", exc)
        for pool in pg_sync_pools:
            try:
                pool.close()
            except Exception as exc:
                logger.debug("reset pg sync store pool close failed: %s", exc)
        # Async pools are loop-bound; reset() has no loop to close them on — drop
        # the references (sockets die with their loop). Tests close via aclose.
        pg_async_pools.clear()


# ─────────────────────────── module-level shim ────────────────────────────
_default_factory = StoreFactory()


def get_default_store_factory() -> StoreFactory:
    """Return the process-wide store factory."""
    return _default_factory


def set_default_store_factory(factory: StoreFactory) -> StoreFactory:
    """Replace the process-wide factory (for tests). Returns the previous factory."""
    global _default_factory
    previous, _default_factory = _default_factory, factory
    return previous


def get_store(home: str | Path | None = None) -> BaseStore:
    """Return a cached synchronous store (SQLite or Postgres) for a home."""
    return _default_factory.get(home)


async def get_async_store(home: str | Path | None = None) -> BaseStore:
    """Return a cached asynchronous store (SQLite or Postgres) for a home."""
    return await _default_factory.aget(home)


async def aclose_stores() -> None:
    """Close cached store connections for graceful process shutdown."""
    await _default_factory.aclose()


def reset_stores() -> None:
    """Clear cached stores for tests."""
    _default_factory.reset()
