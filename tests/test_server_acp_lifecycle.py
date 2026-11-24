"""ACP sites are owned by the runtime, not by the agent that started them.

Every ACP site routes `/acp/agents/<name>/…` through one process-global registry,
so agents configured on the same port share a single server. Two consequences the
teardown has to respect, both covered here:

* stopping the agent that *happened* to open the socket must not take the endpoint
  away from the other agents still registered on that port;
* the site must close (and the agent unregister) once the last agent leaves,
  otherwise the port stays bound and a stopped agent stays callable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from olano.acp import server as acp_server
from olano.runtime.server import RuntimeServer


@pytest.fixture(autouse=True)
def _clean_registry():
    acp_server._REGISTERED_AGENTS.clear()
    yield
    acp_server._REGISTERED_AGENTS.clear()


def _server(monkeypatch, tmp_path: Path, names: list[str]) -> RuntimeServer:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    return RuntimeServer(agent_names=names)


async def _idle() -> None:
    await asyncio.Event().wait()


async def test_shared_acp_port_survives_its_starter_stopping(monkeypatch, tmp_path: Path) -> None:
    server = _server(monkeypatch, tmp_path, ["a", "b"])
    site = asyncio.create_task(_idle())
    server._acp_sites[8765] = site
    server._background_tasks.append(site)
    # `a` opened the socket; `b` joined it. Neither owns the task.
    server._acp_servers.extend([("acp-a", 8765), ("acp-b", 8765)])
    acp_server.register_agent_runner("a", object())
    acp_server.register_agent_runner("b", object())
    server._agent_resources["a"] = {"acp_servers": [("acp-a", 8765)]}
    server._agent_resources["b"] = {"acp_servers": [("acp-b", 8765)]}

    await server._stop_agent_locked("a")

    assert not site.done(), "stopping 'a' closed the site 'b' is still served by"
    assert acp_server.list_registered_agents() == ["b"]

    await server._stop_agent_locked("b")

    assert site.cancelled() or site.done(), "last agent left but the port stayed bound"
    assert acp_server.list_registered_agents() == []
    assert site not in server._background_tasks
