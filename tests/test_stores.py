"""Tests for the persistent SQLite store factory (Virtual File System mode)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from olano.config.home import ensure_olano_home, get_olano_paths
from olano.core.stores import (
    StoreFactory,
    get_async_store,
    get_store,
    reset_stores,
)


def test_store_db_path_is_in_sessions(lionclaw_home: Path) -> None:
    paths = get_olano_paths(ensure_olano_home(lionclaw_home).root)
    assert paths.store_db_path.name == "store.db"
    assert paths.store_db_path.parent == paths.sessions_dir


def test_get_store_is_cached_and_creates_db(lionclaw_home: Path) -> None:
    store_a = get_store(lionclaw_home)
    store_b = get_store(lionclaw_home)
    assert store_a is store_b
    assert get_olano_paths(lionclaw_home).store_db_path.exists()


def test_store_roundtrip(lionclaw_home: Path) -> None:
    store = get_store(lionclaw_home)
    namespace = ("agentx",)
    store.put(namespace, "greeting.txt", {"text": "hi"})
    item = store.get(namespace, "greeting.txt")
    assert item is not None
    assert item.value == {"text": "hi"}


async def test_get_async_store_is_cached(lionclaw_home: Path) -> None:
    store_a = await get_async_store(lionclaw_home)
    store_b = await get_async_store(lionclaw_home)
    assert store_a is store_b


async def test_async_store_is_keyed_per_event_loop(lionclaw_home: Path) -> None:
    """AsyncSqliteStore's internal lock binds to the loop that first uses it —
    a store cached on the runtime loop must not be reused from a transient
    ``asyncio.run`` loop (it crashed with "is bound to a different event loop")."""
    factory = StoreFactory()
    store_main = await factory.aget(lionclaw_home)
    await store_main.aput(("agentx",), "k", {"v": 1})  # bind the lock to this loop

    def _use_from_foreign_loop() -> bool:
        async def _inner() -> bool:
            store = await factory.aget(lionclaw_home)
            assert store is not store_main
            item = await store.aget(("agentx",), "k")  # raised on the shared store
            assert item is not None and item.value == {"v": 1}
            return True

        return asyncio.run(_inner())

    assert await asyncio.to_thread(_use_from_foreign_loop)
    await factory.aclose()


def test_reset_stores_clears_cache(lionclaw_home: Path) -> None:
    first = get_store(lionclaw_home)
    reset_stores()
    second = get_store(lionclaw_home)
    assert first is not second


def test_factory_swap_isolates_state(lionclaw_home: Path) -> None:
    factory = StoreFactory()
    store = factory.get(lionclaw_home)
    assert store is factory.get(lionclaw_home)
    factory.reset()
