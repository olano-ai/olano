"""The loop's default thread pool is widened before anything can queue on it.

Guard for the failure class behind the 2026-08-04 API freeze. Once the runtime
started pushing its blocking work off the loop, every one of those calls landed
in the SAME pool: ``asyncio.to_thread`` (config parses, vault KDFs, rmtree, a
60s ``npm install``, 30s IMAP/SMTP probes), langchain's sync tools
(``BaseTool._arun`` -> ``run_in_executor(None, ...)``) and the adapters'
blocking sends. Python sizes that pool at ``cpu_count + 4`` — six threads on the
two-core production box — which is narrow enough for a handful of slow calls to
queue everything behind them, and narrow enough for a sync tool whose own
completion needs a second slot to deadlock outright.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from olano.runtime.server import _DEFAULT_EXECUTOR_WORKERS, RuntimeServer


def _server(tmp_path: Path, monkeypatch) -> RuntimeServer:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    return RuntimeServer(agent_names=["merli"])


def _installed_executor() -> ThreadPoolExecutor:
    executor = asyncio.get_running_loop()._default_executor  # type: ignore[attr-defined]
    assert isinstance(executor, ThreadPoolExecutor)
    return executor


async def test_default_executor_is_widened(monkeypatch, tmp_path: Path) -> None:
    server = _server(tmp_path, monkeypatch)
    server._loop = asyncio.get_running_loop()
    server._install_default_executor()

    executor = _installed_executor()
    assert executor._max_workers == _DEFAULT_EXECUTOR_WORKERS
    # Well clear of the stock cpu_count+4, which is what made the pool a
    # bottleneck shared by every offload in the process.
    assert executor._max_workers >= 32


async def test_worker_count_is_overridable(monkeypatch, tmp_path: Path) -> None:
    server = _server(tmp_path, monkeypatch)
    server._loop = asyncio.get_running_loop()
    monkeypatch.setenv("LIONCLAW_EXECUTOR_WORKERS", "12")
    server._install_default_executor()

    assert _installed_executor()._max_workers == 12


async def test_bad_override_never_kills_the_boot(monkeypatch, tmp_path: Path) -> None:
    """A typo in the env var must not take the runtime down with it."""
    server = _server(tmp_path, monkeypatch)
    server._loop = asyncio.get_running_loop()

    for bad in ("not-a-number", "0", "-4"):
        monkeypatch.setenv("LIONCLAW_EXECUTOR_WORKERS", bad)
        server._install_default_executor()
        assert _installed_executor()._max_workers >= 1


async def test_offloads_run_concurrently_beyond_the_stock_pool(monkeypatch, tmp_path: Path) -> None:
    """The point of the widening: many blocking calls in flight at once.

    Twelve simultaneous sleepers exceed the stock six-slot pool, so on the
    default executor the last of them could not start until an earlier one
    finished.
    """
    server = _server(tmp_path, monkeypatch)
    server._loop = asyncio.get_running_loop()
    server._install_default_executor()

    started = asyncio.Semaphore(0)
    release = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _blocking() -> None:
        loop.call_soon_threadsafe(started.release)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result(timeout=10)

    tasks = [asyncio.create_task(asyncio.to_thread(_blocking)) for _ in range(12)]
    try:
        for _ in range(12):
            await asyncio.wait_for(started.acquire(), timeout=10)
    finally:
        release.set()
        await asyncio.gather(*tasks)
