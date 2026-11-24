"""A conversation started in the mobile app is labelled MOBILE.

The app sends ``client: "mobile"`` on ``POST /api/chat/stream``. The turn's
RequestContext must keep channel ``api`` (the human gate, magic links and
browser auto-approval treat that as the dashboard, and the app is the same
trusted surface); only the channel stamped on the transcript changes.
"""

from __future__ import annotations

import time
from typing import Any

import pytest


@pytest.fixture
def client(tmp_workspace, monkeypatch):
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    from unittest.mock import AsyncMock, MagicMock

    from fastapi.testclient import TestClient

    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig
    from olano.runtime.api import create_api

    config = AgentConfig(
        name="test_agent",
        model="anthropic:claude-haiku-4-5-20251001",
        backend=FilesystemBackendConfig(root_dir="workspace"),
    )
    runner = MagicMock()
    runner.config = config
    runner.ainvoke = AsyncMock(return_value="hi")
    runtime = MagicMock()
    runtime._runners = {"test_agent": runner}
    runtime._channels = []
    runtime.get_runner.return_value = runner
    with TestClient(create_api(runtime)) as c:
        c.runner = runner  # type: ignore[attr-defined]
        yield c


def _turn_context(client: Any, body: dict) -> tuple[str, str]:
    """(the context's channel, the channel the turn would be stamped with)."""
    from olano.core.thread_labels import channel_for_request
    from olano.security.request_context import get_request_context

    seen: list[tuple[str, str]] = []

    def fake_events(payload, **kwargs):
        async def gen():
            ctx = get_request_context()
            seen.append((ctx.channel, channel_for_request(ctx)))
            yield ("text", "ok")

        return gen()

    client.runner.astream_events = fake_events
    resp = client.post("/api/chat/stream", json={"message": "hi", "agent": "test_agent", **body})
    assert resp.status_code == 200, resp.text
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.02)
    assert seen, "the turn never ran"
    return seen[0]


def test_the_mobile_app_labels_its_turns_mobile_without_changing_their_channel(client) -> None:
    assert _turn_context(client, {"client": "mobile"}) == ("api", "mobile")


def test_a_dashboard_turn_is_still_the_dashboard(client) -> None:
    assert _turn_context(client, {}) == ("api", "dashboard")
    assert _turn_context(client, {"client": "something-else"}) == ("api", "dashboard")
