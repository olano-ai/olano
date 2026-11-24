"""One agent boots at most once, no matter how many callers ask at once.

Regression guard for the prod failure of 2026-07-30: a boot only registers the
agent in ``_runners`` at the very *end* (model build + MCP handshakes + RAG
ingest can take minutes), so ``/api/agents/{name}/start`` twice — an impatient
second click, or a second dashboard on another device — sailed past the
"already running?" check and booted the same agent twice inside one process.
The visible damage was two Telegram pollers on one bot token (a permanent 409
``getUpdates`` conflict, and messages intermittently answered twice), two sets
of scheduler jobs, two RAG watchers, and an ``_agent_resources`` entry that only
remembered one of the two boots for teardown.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from olano.runtime.server import RuntimeServer


def _server(tmp_path: Path, monkeypatch) -> RuntimeServer:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    return RuntimeServer(agent_names=["merli"])


async def test_concurrent_starts_boot_the_agent_once(monkeypatch, tmp_path: Path) -> None:
    server = _server(tmp_path, monkeypatch)
    boots: list[str] = []

    async def slow_boot(name: str) -> None:
        boots.append(name)
        await asyncio.sleep(0.05)  # stand in for the real multi-minute boot
        server._runners[name] = object()  # type: ignore[assignment]

    monkeypatch.setattr(server, "_boot_agent_locked", slow_boot)

    await asyncio.gather(*(server._boot_agent("merli") for _ in range(4)))

    assert boots == ["merli"]
    assert list(server._runners) == ["merli"]


async def test_boot_in_flight_is_visible_to_the_api(monkeypatch, tmp_path: Path) -> None:
    """`agent_boot_in_flight` is what /start uses to answer a duplicate click."""
    server = _server(tmp_path, monkeypatch)
    released = asyncio.Event()

    async def blocking_boot(name: str) -> None:
        await released.wait()
        server._runners[name] = object()  # type: ignore[assignment]

    monkeypatch.setattr(server, "_boot_agent_locked", blocking_boot)

    assert server.agent_boot_in_flight("merli") is False
    task = asyncio.create_task(server._boot_agent("merli"))
    await asyncio.sleep(0)  # let the task take the lock
    assert server.agent_boot_in_flight("merli") is True

    released.set()
    await task
    assert server.agent_boot_in_flight("merli") is False


async def test_stop_during_boot_waits_and_tears_down(monkeypatch, tmp_path: Path) -> None:
    """A stop issued mid-boot no longer no-ops (which left the agent running)."""
    server = _server(tmp_path, monkeypatch)
    order: list[str] = []

    async def slow_boot(name: str) -> None:
        await asyncio.sleep(0.05)
        server._runners[name] = object()  # type: ignore[assignment]
        order.append("booted")

    async def stop(name: str) -> None:
        order.append("stopped")
        server._runners.pop(name, None)

    monkeypatch.setattr(server, "_boot_agent_locked", slow_boot)
    monkeypatch.setattr(server, "_stop_agent_locked", stop)

    boot = asyncio.create_task(server._boot_agent("merli"))
    await asyncio.sleep(0)  # boot takes the lock first
    await asyncio.gather(boot, server._stop_agent("merli"))

    assert order == ["booted", "stopped"]
    assert "merli" not in server._runners


async def test_boots_of_different_agents_still_overlap(monkeypatch, tmp_path: Path) -> None:
    """The lock is per agent — it must not serialize the whole roster."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    server = RuntimeServer(agent_names=["merli", "marisol"])
    in_flight = 0
    peak = 0

    async def slow_boot(name: str) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        server._runners[name] = object()  # type: ignore[assignment]

    monkeypatch.setattr(server, "_boot_agent_locked", slow_boot)

    await asyncio.gather(server._boot_agent("merli"), server._boot_agent("marisol"))

    assert peak == 2
