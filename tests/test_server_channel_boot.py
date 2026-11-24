"""Channel startup is isolated so one bad channel can't hang the boot.

Regression guard for the failure where a Telegram channel blocking on a
network timeout during ``channel.start()`` prevented the REST API from ever
binding — which made the dashboard and login unreachable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import olano.runtime.server as server_mod
from olano.runtime.server import RuntimeServer


class _HangingChannel:
    """A channel whose start() never returns (simulates a network hang)."""

    def __init__(self) -> None:
        self.started = False

    async def start(self) -> None:
        self.started = True
        await asyncio.Event().wait()  # blocks forever


class _RaisingChannel:
    async def start(self) -> None:
        raise RuntimeError("boom")


class _GoodChannel:
    def __init__(self) -> None:
        self.started = False

    async def start(self) -> None:
        self.started = True


async def test_hanging_channel_is_timed_out_and_boot_continues(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    # Keep the test fast — don't actually wait the full 30s timeout.
    monkeypatch.setattr(server_mod, "_CHANNEL_START_TIMEOUT", 0.05)

    server = RuntimeServer(agent_names=["merli"])
    hanging = _HangingChannel()
    good = _GoodChannel()
    server._channels = [hanging, good]

    # Must return (not hang) and must still start the channel after the bad one.
    await asyncio.wait_for(server._start_channels(), timeout=5.0)

    assert hanging.started is True
    assert good.started is True


async def test_raising_channel_is_swallowed_and_boot_continues(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))

    server = RuntimeServer(agent_names=["merli"])
    good = _GoodChannel()
    server._channels = [_RaisingChannel(), good]

    await server._start_channels()  # the RuntimeError must not propagate

    assert good.started is True


async def test_channel_started_on_single_agent_restart(monkeypatch, tmp_path: Path) -> None:
    """A channel attached during a single-agent restart must be started.

    Regression guard: ``_boot_agent`` attached the channel but only the
    one-time ``_start_channels()`` pass ever called ``start()`` — so on a
    ``/api/agents/{name}/restart`` the channel polled nothing and inbound
    messages (e.g. Telegram) were silently queued forever.
    """
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))

    server = RuntimeServer(agent_names=["merli"])
    server._server_started = True  # initial boot is done → restart path

    good = _GoodChannel()
    await server._start_channel_on_restart(good, "telegram", "merli")

    assert good.started is True


async def test_restart_channel_start_failure_is_isolated(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(server_mod, "_CHANNEL_START_TIMEOUT", 0.05)

    server = RuntimeServer(agent_names=["merli"])
    server._server_started = True

    # Neither a hang (timeout) nor a raise may propagate out of the restart path.
    await asyncio.wait_for(
        server._start_channel_on_restart(_HangingChannel(), "telegram", "merli"),
        timeout=5.0,
    )
    await server._start_channel_on_restart(_RaisingChannel(), "telegram", "merli")
