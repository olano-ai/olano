"""Persistent LangGraph checkpoint stores for Olano homes.

The historical implementation kept four module-level dicts caching SQLite
connections by ``(db_path, agent_name)``.  That works in a single-threaded
process, but it leaks state across tests and across long-running processes —
``reset_checkpointers()`` had to be called by hand from every fixture, and
async connections could outlive the loop they were opened on.

This module now owns a single ``CheckpointerFactory`` whose state can be
swapped (``set_default_factory``) or reset (``reset_checkpointers``) from a
fixture without callers having to know about the cache.  The free
``get_checkpointer`` / ``get_async_checkpointer`` / ``aclose_checkpointers`` /
``reset_checkpointers`` helpers are kept for backward compatibility with the
existing call sites in ``builder.py`` and the CLI.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import aiosqlite
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.base import SerializerProtocol

# Both classes are module-level imports of ``langgraph.checkpoint.base`` (just
# above), so these two lines load nothing new, and ``encrypted.py`` imports
# pycryptodome only inside ``from_pycryptodome_aes``, so no optional dependency
# is pulled either: the boot path is untouched. They have to be module-level
# because ``StrictEncryptedSerializer`` and its inner serializer subclass them.
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from olano.config.home import (
    ensure_olano_home,
    get_olano_paths,
    is_postgres_available,
    load_home_config,
    resolve_checkpoint_backend,
)

logger = logging.getLogger(__name__)

# threads.db is a single SQLite file shared by every agent in the process AND
# across processes (runtime server + CLI + scheduler all open it). SQLite (even
# in WAL mode, which langgraph's saver.setup() enables) allows only one writer
# at a time, so concurrent checkpoint writes contend for the write lock. Without
# a busy timeout SQLite raises ``OperationalError: database is locked`` the
# instant it can't grab the lock (the sqlite3 default is only ~5s). We give every
# connection a generous timeout so a contending writer WAITS instead of erroring;
# 30s sits comfortably under the worker wall-clock timeout (DEFAULT_TIMEOUT_SECONDS).
_BUSY_TIMEOUT_S = 30.0
_BUSY_TIMEOUT_MS = int(_BUSY_TIMEOUT_S * 1000)

# Postgres (optional backend) connection-pool sizing. Checkpoint writes hold a
# connection only for the brief write, so a modest pool covers the realistic
# concurrent fan-out; operators with heavy cross-process contention can raise it.
_DEFAULT_PG_POOL_MAX = 20

# psycopg/langgraph-postgres requirements for the saver's explicit-transaction
# model (pgbouncer-safe). Mirrors the SQLite store's ``isolation_level=None``.
_PG_CONN_KWARGS = {"autocommit": True, "prepare_threshold": 0}


def _pg_pool_max() -> int:
    raw = os.environ.get("LIONCLAW_CHECKPOINT_POOL_MAX", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return _DEFAULT_PG_POOL_MAX


# Encryption at rest for every checkpoint blob — ``checkpoints.checkpoint``,
# ``writes.value`` and their Postgres equivalents. When this variable holds a
# 16/24/32-byte string, every saver this module constructs (and the two sibling
# constructors in ``analytics/threadcost/reader.py`` and ``checkpoint_migrate.py``)
# wraps ``JsonPlusSerializer`` in langgraph's ``EncryptedSerializer`` (AES-EAX via
# pycryptodome), which tags each row's type string ``+aes`` and still decodes a
# row written before the key existed. Unset ⇒ no ``serde=`` is passed at all, so
# behaviour is byte-identical to a build that never had this. The name is
# langgraph's own (``from_pycryptodome_aes`` reads the same variable) and every
# encrypted row on every box can only be read back through it — renaming it
# silently makes every encrypted thread unreadable; tests/test_at_rest_constants.py
# pins it. The key itself is never logged.
#
# Strict mode. That "still decodes a row written before the key existed" is
# ``EncryptedSerializer.loads_typed`` handing any row whose type string carries
# no ``+<cipher>`` suffix straight to the inner serializer. It is what lets a
# key be switched on under a live box without losing a thread, and on a box that
# promises every row is encrypted (the managed sealed deployments) it is the
# hole: a row written without the key, or by a process that never had it, reads
# back as if nothing were wrong. With the second variable truthy (``1``/``true``/
# ``yes``/``on``, any case) ``checkpoint_serde()`` builds ``StrictEncryptedSerializer``
# instead, which raises ``PlaintextCheckpointRefused`` on such a row, and pins the
# inner ``JsonPlusSerializer`` to langgraph's built-in msgpack allowlist
# explicitly (``allowed_msgpack_modules=None``) rather than through
# ``LANGGRAPH_STRICT_MSGPACK``, which langgraph reads once, at import time. The
# flag without a key is refused outright: a box claiming strictness while it
# writes plaintext is the failure this exists to prevent. Without the flag the
# permissive serializer is built exactly as before, so a box that only sets the
# key sees no change. One deliberate exception: the migrator's *source* saver
# (``checkpoint_migrate.migrate_sqlite_to_postgres``) reads with ``strict=False``,
# because a legacy ``threads.db`` is plaintext by definition and the point is to
# move those rows and land them encrypted.
CHECKPOINT_AES_KEY_ENV = "LANGGRAPH_AES_KEY"
CHECKPOINT_REQUIRE_ENCRYPTED_ENV = "LIONCLAW_CHECKPOINT_REQUIRE_ENCRYPTED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_AES_KEY_LENGTHS = (16, 24, 32)
_serde_cache: dict[tuple[str, bool], SerializerProtocol] = {}
_serde_lock = threading.Lock()
# Guards the in-place allowlist merge below; separate from ``_serde_lock`` so a
# merge triggered while another thread constructs a serializer never waits on it.
_allowlist_lock = threading.Lock()


def require_encrypted_checkpoints() -> bool:
    """``True`` when this box says every checkpoint row must be encrypted."""
    return os.environ.get(CHECKPOINT_REQUIRE_ENCRYPTED_ENV, "").strip().lower() in _TRUTHY


class PlaintextCheckpointRefused(ValueError):
    """A plaintext checkpoint row was met on a box that requires encrypted rows.

    Raised by :class:`StrictEncryptedSerializer` where langgraph's serializer
    would silently decode the row. The row was written without
    ``LANGGRAPH_AES_KEY``: it predates the key, or a process that did not have
    the key wrote it.
    """

    def __init__(self, row_type: str) -> None:
        self.row_type = row_type
        super().__init__(
            f"Refusing to decode a plaintext checkpoint row (type {row_type!r}, no "
            f"cipher suffix): {CHECKPOINT_REQUIRE_ENCRYPTED_ENV} says every row on this "
            f"box must be encrypted, and this one was written without "
            f"{CHECKPOINT_AES_KEY_ENV}, either before the key existed or by a process "
            "that did not have it. Migrate or delete the row; it will not be read here."
        )


class _StrictJsonPlusSerializer(JsonPlusSerializer):
    """The inner serializer of strict mode: explicit allowlist, merged in place.

    Two jobs. It is constructed with ``allowed_msgpack_modules=None`` (only
    langgraph's built-in safe types decode, anything else is blocked with a
    warning) so strictness never depends on ``LANGGRAPH_STRICT_MSGPACK`` having
    been set before langgraph was imported. And it overrides
    ``with_msgpack_allowlist`` to merge in place and return ``self``. That matters
    because when that env var IS on, every ``compile()`` and every
    ``stream``/``get_state`` call pushes the graph's own state types into the
    checkpointer through ``BaseCheckpointSaver.with_allowlist``, and langgraph
    rebuilds the wrapper as the base ``EncryptedSerializer(cipher, new_inner)``
    whenever the inner serializer changed: :class:`StrictEncryptedSerializer` would
    be silently replaced by the permissive class on exactly the boxes that set the
    flag. Returning the same inner is the documented "nothing changed" path, on
    which langgraph keeps the original wrapper. The allowlist is therefore the
    union of every graph compiled in this process, which is what one shared
    serializer per key means anyway; it only ever widens by types this process's
    own graphs declare, never by anything read from a row.
    """

    def __init__(self) -> None:
        super().__init__(allowed_msgpack_modules=None)

    def with_msgpack_allowlist(
        self, extra_allowlist: Iterable[tuple[str, ...] | type]
    ) -> JsonPlusSerializer:
        with _allowlist_lock:
            merged = super().with_msgpack_allowlist(extra_allowlist)
            if merged is not self:
                # The base class computed the union on a clone; adopt its allowlist
                # and the msgpack ext hook built over it, and keep this instance.
                self._allowed_msgpack_modules = merged._allowed_msgpack_modules
                self._unpack_ext_hook = merged._unpack_ext_hook
        return self


class StrictEncryptedSerializer(EncryptedSerializer):
    """``EncryptedSerializer`` that refuses plaintext rows instead of decoding them.

    An encrypted row (type ``<typ>+<cipher>``) decodes exactly as in the parent;
    a row without the suffix raises :class:`PlaintextCheckpointRefused`.
    """

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        row_type = data[0]
        if "+" not in row_type:
            raise PlaintextCheckpointRefused(row_type)
        return super().loads_typed(data)


def checkpoint_serde(strict: bool | None = None) -> SerializerProtocol | None:
    """The at-rest serializer every checkpoint saver must share, or ``None``.

    ``strict=None`` reads ``LIONCLAW_CHECKPOINT_REQUIRE_ENCRYPTED``; an explicit
    value overrides it (the migrator's source saver passes ``False``). Cached
    per ``(key, strict)`` so repeated saver construction (one per agent, per
    loop) reuses one instance. A key of the wrong length raises before any row
    could be written with it, and strict mode without a key raises rather than
    open a store that promises encrypted rows while it writes plaintext ones.
    No key and no flag returns ``None``, exactly as before.
    """
    if strict is None:
        strict = require_encrypted_checkpoints()
    key = os.environ.get(CHECKPOINT_AES_KEY_ENV)
    if not key:
        if strict:
            raise ValueError(
                f"{CHECKPOINT_REQUIRE_ENCRYPTED_ENV} is set but {CHECKPOINT_AES_KEY_ENV} "
                "is not: this box requires every checkpoint row to be encrypted and has "
                "no key to encrypt with. Refusing to open the checkpoint store rather "
                f"than write plaintext rows on a box that promises none. Set "
                f"{CHECKPOINT_AES_KEY_ENV} (16, 24 or 32 bytes) or unset "
                f"{CHECKPOINT_REQUIRE_ENCRYPTED_ENV}."
            )
        return None
    cache_key = (key, strict)
    with _serde_lock:
        serde = _serde_cache.get(cache_key)
        if serde is not None:
            return serde
        key_bytes = key.encode()
        if len(key_bytes) not in _AES_KEY_LENGTHS:
            raise ValueError(
                f"{CHECKPOINT_AES_KEY_ENV} must be exactly 16, 24 or 32 bytes "
                f"(got {len(key_bytes)}). Refusing to start checkpoint encryption "
                "with a key of the wrong length rather than write rows nothing can read."
            )
        if strict:
            serde = StrictEncryptedSerializer.from_pycryptodome_aes(
                serde=_StrictJsonPlusSerializer(), key=key_bytes
            )
        else:
            serde = EncryptedSerializer.from_pycryptodome_aes(key=key_bytes)
        _serde_cache[cache_key] = serde
        logger.info(
            "Checkpoint encryption at rest active (%s is set%s)",
            CHECKPOINT_AES_KEY_ENV,
            "; strict: plaintext rows are refused" if strict else "",
        )
        return serde


def checkpoint_serde_kwargs(strict: bool | None = None) -> dict[str, Any]:
    """``{"serde": ...}`` for a saver constructor, or ``{}`` when no key is set.

    Spread into every ``*Saver(...)`` call so an unset key constructs the saver
    exactly as before (not even an explicit ``serde=None``); ``strict`` is passed
    through to :func:`checkpoint_serde`.
    """
    serde = checkpoint_serde(strict)
    return {"serde": serde} if serde is not None else {}


def reset_checkpoint_serde() -> None:
    """Drop the cached at-rest serializers (tests set a different key per test)."""
    with _serde_lock:
        _serde_cache.clear()


def _checkpoint_cache_key(agent_name: str, db_path: Path) -> str:
    return f"{db_path.resolve()}::{agent_name}"


# threads.db is a single file shared by every agent, and LangGraph addresses
# checkpoint rows by ``thread_id`` alone. Thread ids are derived from the *human*
# (Telegram chat id, dashboard user, …), never the agent — so two agents the same
# human talks to would resume ONE shared checkpoint lineage and bleed identity
# into each other. We bind every checkpoint key to its agent with this prefix so
# lineages can never collide. The scoped key exists ONLY as the SQL key inside
# threads.db: it is never written to a JSONL filename / .state.json, returned to a
# channel, or fed back through the runner — the human-facing ``thread_id`` stays
# the currency everywhere else. ``::`` is unambiguous against every existing
# scheme (``u:<user>:<uuid>`` uses a single colon; ``acp::``/``cog::``/``tg_``/
# ``heartbeat_``/raw numeric ids never start with ``<agent>::``).
_SCOPE_DELIM = "::"


def scope_thread(agent_name: str, thread_id: str) -> str:
    """Bind a checkpoint ``thread_id`` to one agent so agents never share a lineage.

    Idempotent: an already-scoped id is returned unchanged.
    """
    prefix = f"{agent_name}{_SCOPE_DELIM}"
    return thread_id if thread_id.startswith(prefix) else f"{prefix}{thread_id}"


def unscope_thread(agent_name: str, scoped: str) -> str:
    """Inverse of :func:`scope_thread` for filesystem / label lookups.

    Per-agent JSONL transcripts live under ``sessions/<agent>/<thread_id>.jsonl``
    keyed by the *human-facing* id, so analytics that resolved a scoped checkpoint
    key back to an agent must unscope before hitting the transcript path.
    """
    prefix = f"{agent_name}{_SCOPE_DELIM}"
    return scoped[len(prefix) :] if scoped.startswith(prefix) else scoped


def get_thread_db_path(home: str | Path | None = None) -> Path:
    paths = ensure_olano_home(home)
    return get_olano_paths(paths.root).thread_db_path


class CheckpointerFactory:
    """Owns the checkpoint saver cache for one process.

    The backend is resolved per call from the home config / env
    (:func:`resolve_checkpoint_backend`): ``sqlite`` (default, zero-config) caches
    one connection per ``(db_path, agent_name)``; ``postgres`` shares a single
    pooled saver per process.

    Instances are safe to use concurrently — registration is guarded by a
    re-entrant lock so two coroutines / threads racing to open the same saver
    share it instead of leaking sockets.
    """

    def __init__(self) -> None:
        self._sync_savers: dict[str, SqliteSaver] = {}
        self._sync_conns: dict[str, sqlite3.Connection] = {}
        # Async sqlite savers are keyed by ``(cache-key, loop-id)``:
        # AsyncSqliteSaver holds an internal ``asyncio.Lock`` bound to the loop
        # that first uses it, so a saver cached on the runtime loop crashes with
        # "is bound to a different event loop" when reused from a transient
        # ``asyncio.run`` loop (thread-origin scheduler run-now, one-shot CLI).
        self._async_savers: dict[tuple[str, int], AsyncSqliteSaver] = {}
        self._async_conns: dict[tuple[str, int], aiosqlite.Connection] = {}
        # Postgres (optional). Async savers/pools are keyed by ``(url, loop-id)``
        # because AsyncPostgresSaver and its pool bind to the loop they open on;
        # the sync saver/pool are keyed by url alone.
        self._pg_async_savers: dict[tuple[str, int], BaseCheckpointSaver] = {}
        self._pg_async_pools: dict[tuple[str, int], Any] = {}
        self._pg_sync_savers: dict[str, BaseCheckpointSaver] = {}
        self._pg_sync_pools: dict[str, Any] = {}
        self._lock = threading.RLock()

    # -- backend resolution ----------------------------------------------
    def _resolve_backend(self, home: str | Path | None) -> tuple[str, str | None]:
        """Return ``(backend, url)`` for *home*, validating Postgres availability."""
        backend, url = resolve_checkpoint_backend(load_home_config(home))
        if backend == "postgres" and not is_postgres_available():
            raise RuntimeError(
                "checkpoint_backend=postgres but the Postgres extra is not "
                'installed. Install it with: uv add "olano[postgres]"'
            )
        return backend, url

    # -- sync ------------------------------------------------------------
    def get(self, agent_name: str, home: str | Path | None = None) -> BaseCheckpointSaver:
        backend, url = self._resolve_backend(home)
        if backend == "postgres":
            assert url is not None  # _resolve_backend guarantees a url for postgres
            return self._get_postgres(url)
        db_path = get_thread_db_path(home)
        key = _checkpoint_cache_key(agent_name, db_path)
        with self._lock:
            saver = self._sync_savers.get(key)
            if saver is not None:
                return saver
            conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=_BUSY_TIMEOUT_S)
            # ``timeout=`` maps to busy_timeout, but set the PRAGMA explicitly too
            # so the wait survives any later connection reset and is unambiguous.
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            saver = SqliteSaver(conn, **checkpoint_serde_kwargs())
            saver.setup()
            self._sync_conns[key] = conn
            self._sync_savers[key] = saver
            return saver

    def _get_postgres(self, url: str) -> BaseCheckpointSaver:
        """Return the shared synchronous ``PostgresSaver`` over a pooled connection."""
        with self._lock:
            saver = self._pg_sync_savers.get(url)
            if saver is not None:
                return saver
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(
            conninfo=url, max_size=_pg_pool_max(), min_size=1, open=False, kwargs=_PG_CONN_KWARGS
        )
        pool.open()
        # The saver sets its own row factory on the pool (hence the arg-type ignore).
        saver = PostgresSaver(pool, **checkpoint_serde_kwargs())  # type: ignore[arg-type]
        saver.setup()
        with self._lock:
            existing = self._pg_sync_savers.get(url)
            if existing is not None:
                try:
                    pool.close()
                except Exception as exc:
                    logger.debug("duplicate pg sync pool close failed: %s", exc)
                return existing
            self._pg_sync_pools[url] = pool
            self._pg_sync_savers[url] = saver
            # saver is Any (langgraph.checkpoint.postgres is untyped under
            # --ignore-missing-imports); narrow it back to the declared return type.
            return cast(BaseCheckpointSaver, saver)

    async def _aget_postgres(self, url: str) -> BaseCheckpointSaver:
        """Return the shared async ``AsyncPostgresSaver`` for the running loop.

        Keyed by ``(url, loop-id)`` so the scheduler's ``asyncio.run`` fallback
        gets its own pool rather than reusing one bound to a dead loop.
        """
        loop = asyncio.get_running_loop()
        key = (url, id(loop))
        with self._lock:
            saver = self._pg_async_savers.get(key)
            if saver is not None:
                return saver
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            conninfo=url, max_size=_pg_pool_max(), min_size=1, open=False, kwargs=_PG_CONN_KWARGS
        )
        await pool.open()
        # The saver sets its own row factory on the pool (hence the arg-type ignore).
        saver = AsyncPostgresSaver(pool, **checkpoint_serde_kwargs())  # type: ignore[arg-type]
        await saver.setup()
        with self._lock:
            existing = self._pg_async_savers.get(key)
            if existing is not None:
                # Lost the race — close the duplicate and return the winner.
                try:
                    await pool.close()
                except Exception as exc:
                    logger.debug("duplicate pg async pool close failed: %s", exc)
                return existing
            self._pg_async_pools[key] = (pool, loop)
            self._pg_async_savers[key] = saver
            # saver is Any (untyped postgres aio module); narrow to the return type.
            return cast(BaseCheckpointSaver, saver)

    # -- async -----------------------------------------------------------
    async def aget(self, agent_name: str, home: str | Path | None = None) -> BaseCheckpointSaver:
        backend, url = self._resolve_backend(home)
        if backend == "postgres":
            assert url is not None  # _resolve_backend guarantees a url for postgres
            return await self._aget_postgres(url)
        db_path = get_thread_db_path(home)
        key = (_checkpoint_cache_key(agent_name, db_path), id(asyncio.get_running_loop()))
        # Async path: only the dict reads/writes need locking, the
        # ``await aiosqlite.connect`` is a coroutine so we hold the lock
        # only to claim the slot and release it before awaiting.
        with self._lock:
            saver = self._async_savers.get(key)
            if saver is not None:
                return saver
        conn = await aiosqlite.connect(str(db_path), timeout=_BUSY_TIMEOUT_S)
        # ``timeout=`` maps to busy_timeout, but set the PRAGMA explicitly too so
        # the wait survives any later connection reset and is unambiguous. The
        # busy-wait sleeps aiosqlite's worker thread, not the event loop.
        await conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        saver = AsyncSqliteSaver(conn, **checkpoint_serde_kwargs())
        await saver.setup()
        with self._lock:
            existing = self._async_savers.get(key)
            if existing is not None:
                # Lost the race — close the duplicate and return the winner.
                try:
                    await conn.close()
                except Exception as exc:
                    logger.debug("duplicate aiosqlite close failed: %s", exc)
                return existing
            self._async_conns[key] = conn
            self._async_savers[key] = saver
            return saver

    # -- deletion --------------------------------------------------------
    async def adelete(
        self, agent_name: str, thread_id: str, home: str | Path | None = None
    ) -> None:
        """Purge a thread's checkpoints from the shared threads.db.

        threads.db is shared across agents; rows are keyed by an **agent-scoped**
        thread id (:func:`scope_thread`) so lineages never collide. The caller
        passes the human-facing ``thread_id``; we scope it with ``agent_name``
        (the same binding the runner applies) so we delete this agent's row, not
        another agent's identically-named thread. ``agent_name`` also selects that
        agent's cached connection.
        """
        saver = await self.aget(agent_name, home)
        await saver.adelete_thread(scope_thread(agent_name, thread_id))

    async def aclose_agent(self, agent_name: str, home: str | Path | None = None) -> None:
        """Close & evict ONE agent's cached async checkpointer connection.

        Ephemeral workers (cognition ``__cog__`` / delegate ``__sub__``) mint a
        unique agent name per run, so each invocation opens a fresh aiosqlite
        connection — and each aiosqlite connection spawns a dedicated OS thread.
        Left in the cache they accumulate without bound over a long-running
        autonomous process, multiplying the writers that contend for the SQLite
        write lock. Call this when an ephemeral worker finishes to release the
        connection. The persisted checkpoint rows stay in threads.db (analytics
        still reads ``cog::``/``sub::`` threads back), only the live socket is freed.

        Postgres no-op: the pooled backend shares one connection set process-wide,
        so there is no per-agent socket to free (the per-run leak this fixes is a
        SQLite-only artifact of opening one connection per unique agent name).
        """
        backend, _ = self._resolve_backend(home)
        if backend == "postgres":
            return
        db_path = get_thread_db_path(home)
        cache_key = _checkpoint_cache_key(agent_name, db_path)
        # Savers are keyed per loop — evict this agent's entries on every loop
        # (aiosqlite routes each call via the loop current at call time, so
        # closing a connection opened under another loop is safe).
        with self._lock:
            keys = [k for k in self._async_conns if k[0] == cache_key]
            conns = [self._async_conns.pop(k) for k in keys]
            for k in keys:
                self._async_savers.pop(k, None)
        for conn in conns:
            try:
                await conn.close()
            except Exception as exc:
                logger.debug("aclose_agent async %s: %s", cache_key, exc)

    # -- lifecycle -------------------------------------------------------
    async def aclose(self) -> None:
        """Close all cached connections / pools (graceful shutdown)."""
        with self._lock:
            async_conns = list(self._async_conns.items())
            sync_conns = list(self._sync_conns.items())
            pg_async_pools = list(self._pg_async_pools.values())
            pg_sync_pools = list(self._pg_sync_pools.values())
            self._async_conns.clear()
            self._async_savers.clear()
            self._sync_conns.clear()
            self._sync_savers.clear()
            self._pg_async_pools.clear()
            self._pg_async_savers.clear()
            self._pg_sync_pools.clear()
            self._pg_sync_savers.clear()
        for key, async_conn in async_conns:
            try:
                await async_conn.close()
            except Exception as exc:
                logger.debug("aclose async %s: %s", key, exc)
        for sync_key, sync_conn in sync_conns:
            try:
                sync_conn.close()
            except Exception as exc:
                logger.debug("aclose sync %s: %s", sync_key, exc)
        running = asyncio.get_running_loop()
        for pool, loop in pg_async_pools:
            # A pool can only be closed on the loop it was opened on. For the
            # long-lived runtime loop that's this one; pools from a transient
            # ``asyncio.run`` loop are already dead — drop them (their sockets are
            # torn down with that loop) rather than crash closing cross-loop.
            if loop is running:
                try:
                    await pool.close()
                except Exception as exc:
                    logger.debug("aclose pg async pool: %s", exc)
        for pool in pg_sync_pools:
            try:
                pool.close()
            except Exception as exc:
                logger.debug("aclose pg sync pool: %s", exc)

    def reset(self) -> None:
        """Synchronous full reset — used by tests."""
        with self._lock:
            sync_conns = list(self._sync_conns.values())
            async_conns = list(self._async_conns.values())
            pg_sync_pools = list(self._pg_sync_pools.values())
            pg_async_pools = list(self._pg_async_pools.values())
            self._sync_conns.clear()
            self._sync_savers.clear()
            self._async_conns.clear()
            self._async_savers.clear()
            self._pg_sync_pools.clear()
            self._pg_sync_savers.clear()
            self._pg_async_pools.clear()
            self._pg_async_savers.clear()
        for sync_conn in sync_conns:
            try:
                sync_conn.close()
            except Exception as exc:
                logger.debug("reset sync close failed: %s", exc)
        for async_conn in async_conns:
            try:
                asyncio.run(async_conn.close())
            except Exception as exc:
                logger.debug("reset async close failed: %s", exc)
        for pool in pg_sync_pools:
            try:
                pool.close()
            except Exception as exc:
                logger.debug("reset pg sync pool close failed: %s", exc)
        # Async pools are loop-bound and reset() has no running loop to close them
        # on — drop the references (sockets die with their loop). Tests that need
        # a clean async close call ``aclose`` on the owning loop explicitly.
        pg_async_pools.clear()


# ─────────────────────────── module-level shim ────────────────────────────
# A single default factory keeps backward compatibility with the previous
# free functions used across builder.py / cli / tests.

_default_factory = CheckpointerFactory()


def get_default_factory() -> CheckpointerFactory:
    """Return the process-wide checkpointer factory."""
    return _default_factory


def set_default_factory(factory: CheckpointerFactory) -> CheckpointerFactory:
    """Replace the process-wide factory (for tests).

    Returns the previous factory so callers can restore it.
    """
    global _default_factory
    previous, _default_factory = _default_factory, factory
    return previous


def get_checkpointer(agent_name: str, home: str | Path | None = None) -> BaseCheckpointSaver:
    """Return a cached synchronous checkpointer (SQLite or Postgres) for an agent/home."""
    return _default_factory.get(agent_name, home)


async def get_async_checkpointer(
    agent_name: str, home: str | Path | None = None
) -> BaseCheckpointSaver:
    """Return a cached asynchronous checkpointer (SQLite or Postgres) for an agent/home."""
    return await _default_factory.aget(agent_name, home)


async def aget_postgres_pool(home: str | Path | None = None) -> Any | None:
    """The shared async Postgres pool for the running loop, or ``None`` on sqlite.

    What the checkpoint garbage collector (``core/checkpoint_gc``) runs on: the
    same pool the saver writes through, so the sweep sees every row the
    runtime does and opens no connection of its own.
    """
    saver = await aget_postgres_saver(home)
    return getattr(saver, "conn", None) if saver is not None else None


async def aget_postgres_saver(home: str | Path | None = None) -> Any | None:
    """The shared ``AsyncPostgresSaver`` for the running loop, or ``None`` on sqlite.

    The sweep's compaction writes through it (``checkpoint_gc.compact_namespace``)
    so a snapshot it appends is serialized, and encrypted, exactly as the
    runtime's own checkpoints are.
    """
    backend, url = _default_factory._resolve_backend(home)
    if backend != "postgres" or not url:
        return None
    return await _default_factory._aget_postgres(url)


#: The raw thread prefixes of background runs. A cognition worker or a
#: ``delegate_task`` child runs the graph directly under
#: ``cog::<parent>__cog__<label>__<hex>`` (``__sub__`` for a delegate), never
#: through :func:`scope_thread`, so its rows carry no ``<parent>::`` prefix.
_WORKER_THREAD_PREFIXES = ("cog::", "sub::")
_WORKER_NAME_MARKERS = ("__cog__", "__sub__")


def thread_owner(thread_id: str) -> str | None:
    """The agent a checkpoint thread belongs to, or ``None`` when it names none.

    ``<agent>::…`` for every runner turn, and the parent for a background run
    (``cog::merli__cog__pulse_agent__ab12cd`` is merli's). The one definition
    the deleted-agent purge and the sweep's orphan report share: reading
    ``cog`` as an agent name counted every background run as an orphan and let
    a deleted agent's runs outlive it.
    """
    if not isinstance(thread_id, str) or _SCOPE_DELIM not in thread_id:
        return None
    head, rest = thread_id.split(_SCOPE_DELIM, 1)
    if f"{head}{_SCOPE_DELIM}" in _WORKER_THREAD_PREFIXES:
        child = rest.split(_SCOPE_DELIM, 1)[0]
        for marker in _WORKER_NAME_MARKERS:
            if marker in child:
                return child.split(marker, 1)[0] or None
        # ``cog::<agent>`` with no worker marker: an id from before workers
        # minted unique names, still the named agent's. (An agent literally
        # named ``cog`` scopes as ``cog::<uuid>``; its uuid is no agent name,
        # and the caller's own prefix match still finds those rows.)
        return child or None
    return head or None


def _agent_thread_patterns(agent_name: str) -> list[str]:
    """``LIKE`` patterns covering every row :func:`thread_owner` gives to ``agent_name``."""
    patterns = [f"{agent_name}{_SCOPE_DELIM}%"]
    for prefix in _WORKER_THREAD_PREFIXES:
        patterns.append(f"{prefix}{agent_name}%")
    return patterns


def delete_agent_threads(agent_name: str, home: str | Path | None = None) -> int:
    """Purge every checkpoint row of ``agent_name``'s threads; returns how many.

    Rows are keyed by the agent-scoped id (:func:`scope_thread`) for every
    runner turn and by ``cog::<agent>__cog__…`` for its background runs, so the
    agent's whole history is the set of thread ids :func:`thread_owner` gives
    to it, on either backend. Deleting an agent used to leave these behind as
    "harmless" orphans: on a busy box they are the largest rows in the store
    and every sweep scans them. Best-effort by contract: a store that cannot be
    reached costs the purge, never the deletion.
    """
    prefix = f"{agent_name}{_SCOPE_DELIM}"
    patterns = _agent_thread_patterns(agent_name)
    try:
        saver = _default_factory.get(agent_name, home)
        cursor_ctx = getattr(saver, "_cursor", None)
        rows: list[Any] = []
        if callable(cursor_ctx):  # PostgresSaver
            with cursor_ctx() as cur:
                for pattern in patterns:
                    cur.execute(
                        "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE %s",
                        (pattern,),
                    )
                    rows.extend(cur.fetchall())
        else:  # SqliteSaver: a plain sqlite3 connection behind the saver's lock
            with saver.lock:  # type: ignore[attr-defined]
                for pattern in patterns:
                    rows.extend(
                        saver.conn.execute(  # type: ignore[attr-defined]
                            "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE ?",
                            (pattern,),
                        ).fetchall()
                    )
        thread_ids = []
        for row in rows:
            tid = row["thread_id"] if isinstance(row, dict) else row[0]
            # LIKE treats ``_`` as a wildcard and ``cog::<name>%`` also matches
            # ``cog::<name>x__cog__…``; the owner rule is the real test.
            if not isinstance(tid, str) or tid in thread_ids:
                continue
            if tid.startswith(prefix) or thread_owner(tid) == agent_name:
                thread_ids.append(tid)
        for tid in thread_ids:
            saver.delete_thread(tid)
        if thread_ids:
            logger.info(
                "checkpoints: purged %d thread(s) of deleted agent %s", len(thread_ids), agent_name
            )
        return len(thread_ids)
    except Exception:
        logger.warning("checkpoints: purge for deleted agent %s failed", agent_name, exc_info=True)
        return 0


async def adelete_agent_threads(agent_name: str, home: str | Path | None = None) -> int:
    """:func:`delete_agent_threads` off the event loop (the purge is sync I/O)."""
    return await asyncio.to_thread(delete_agent_threads, agent_name, home)


async def adelete_thread(agent_name: str, thread_id: str, home: str | Path | None = None) -> None:
    """Purge a thread's checkpoints from the shared threads.db checkpoint store."""
    await _default_factory.adelete(agent_name, thread_id, home)


async def aclose_agent_checkpointer(agent_name: str, home: str | Path | None = None) -> None:
    """Close & evict one agent's cached async checkpointer (ephemeral workers).

    See :meth:`CheckpointerFactory.aclose_agent`. No-op if nothing is cached for
    that name, so it is safe to call unconditionally in a worker's cleanup path.
    """
    await _default_factory.aclose_agent(agent_name, home)


async def aclose_checkpointers() -> None:
    """Close cached checkpoint connections for graceful process shutdown."""
    await _default_factory.aclose()


def reset_checkpointers() -> None:
    """Clear cached checkpointers for tests."""
    _default_factory.reset()
