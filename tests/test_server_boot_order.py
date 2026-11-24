"""Boot-order guarantees of ``RuntimeServer.start()``.

Regression: the REST API used to bind *after* the agent-boot loop, so anything
that stalled an agent's build (an MCP server downloading its package, a stdio
``mcp-remote`` wrapper stuck waiting for OAuth authorization, model warm-up)
kept ``/api/status`` dead — installers polling it for readiness timed out and
the dashboard/login were unreachable for the whole boot.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from olano.runtime.server import RuntimeServer


class _StopBoot(Exception):
    pass


async def test_start_binds_api_before_agent_boot(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_NO_HOUSEKEEPING", "1")

    server = RuntimeServer(agent_names=["merli"])
    calls: list[str] = []

    monkeypatch.setattr(server, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(server.webhook_dispatcher, "start", lambda: None)
    monkeypatch.setattr(server, "_unlock_vault_for_boot", lambda: None)
    monkeypatch.setattr(server, "_should_boot_on_start", lambda n: True)

    async def fake_api() -> None:
        calls.append("api")

    async def fake_boot(name: str) -> None:
        calls.append("boot")
        raise _StopBoot()

    monkeypatch.setattr(server, "_start_api", fake_api)
    monkeypatch.setattr(server, "_boot_agent", fake_boot)

    with pytest.raises(_StopBoot):
        await server.start()

    assert calls == ["api", "boot"], f"API must bind before the first agent boots, got {calls}"
    # The booting flag must clear even when a boot raises.
    assert server._agents_booting is False
