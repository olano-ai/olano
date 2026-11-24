"""Runtime-driven agent status changes must reach connected dashboards.

Regression: the registry is in-process state that `GET /api/agents` reads per
request, and only the API's own handlers ever emitted a roster frame. Every
status transition the runtime made on its own — the whole boot loop — was
therefore invisible to an already-open dashboard, which left the sidebar agent
picker showing "offline" for agents that had finished booting long before. (The
Overview grid only appeared to work because it separately sniffs boot lines out
of the log stream.)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from olano.runtime import api as api_mod
from olano.runtime.server import RuntimeServer


@pytest.fixture
def subscriber():
    """A live /api/stream subscriber queue, removed again after the test."""
    q: asyncio.Queue = asyncio.Queue()
    api_mod._subscribers.append(q)
    yield q
    if q in api_mod._subscribers:
        api_mod._subscribers.remove(q)


def _frames(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_status_change_broadcasts_roster_frame(monkeypatch, tmp_path: Path, subscriber) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    server = RuntimeServer(agent_names=["merli"])

    server._set_agent_status("merli", "running")

    frames = _frames(subscriber)
    assert [f["type"] for f in frames] == ["agents_update"]
    assert frames[0]["data"] == {"name": "merli"}


def test_every_lifecycle_status_reaches_subscribers(
    monkeypatch, tmp_path: Path, subscriber
) -> None:
    """starting → running → stopping → stopped each produce their own frame, so a
    dashboard open across a restart can follow the boot rather than sit stale."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    server = RuntimeServer(agent_names=["merli"])

    for status in ("starting", "running", "stopping", "stopped"):
        server._set_agent_status("merli", status)

    frames = _frames(subscriber)
    assert [f["type"] for f in frames] == ["agents_update"] * 4


def test_status_is_recorded_in_the_registry(monkeypatch, tmp_path: Path) -> None:
    """The broadcast is additive — the registry write it wraps still happens."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    server = RuntimeServer(agent_names=["merli"])

    from olano.orchestration.agent_registry import AgentEntry

    server.registry.register(AgentEntry(name="merli"))
    server._set_agent_status("merli", "error", error="boom")

    entry = server.registry.get("merli")
    assert entry is not None
    assert entry.status == "error"
    assert entry.error == "boom"


def test_broadcast_failure_does_not_break_the_status_write(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    """Best-effort contract: the API may not be up yet during early boot, and a
    dashboard notification must never be able to fail an agent transition."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    server = RuntimeServer(agent_names=["merli"])

    from olano.orchestration.agent_registry import AgentEntry

    server.registry.register(AgentEntry(name="merli"))

    def _boom(_name: str | None = None) -> None:
        raise RuntimeError("stream is down")

    monkeypatch.setattr(api_mod, "_broadcast_roster_changed", _boom)

    server._set_agent_status("merli", "running")  # must not raise

    assert server.registry.get("merli").status == "running"
