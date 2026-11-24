"""Tests for the public per-agent OpenAI API routes (/api/openai/<agent>/…).

The proxy makes a loopback-bound agent API reachable from the deployment's
public origin, so its refusals are the security surface: an unknown agent, an
agent whose server is not running, and above all an agent with no usable API
key must never be proxied.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestServer

from olano.platforms.api_server import APIServerAdapter
from olano.platforms.config import Platform, PlatformConfig

API_KEY = "test-key-0123456789abcdef"  # gitleaks:allow
AGENT = "test-agent"


@pytest.fixture
def registry():
    """The live platform registry, emptied after each test."""
    from olano.platforms import get_platform_registry

    reg = get_platform_registry()
    yield reg
    reg.unregister_agent(AGENT)


def _runtime():
    runtime = MagicMock()
    runtime._runners = {}
    return runtime


@pytest.fixture
def api_client(tmp_workspace, monkeypatch):
    """Sync client — for the refusals, which never reach an upstream."""
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_workspace / "home"))

    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    return TestClient(create_api(_runtime()))


@pytest.fixture
async def async_client(tmp_workspace, monkeypatch):
    """Async ASGI client — required whenever a test proxies to a live upstream.

    The upstream aiohttp server runs on this test's event loop, so a *sync*
    client would block that loop inside the request and the upstream could
    never answer: a deadlock, not a slow test.
    """
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_workspace / "home"))

    import httpx

    from olano.runtime.api import create_api

    app = create_api(_runtime())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield client


def _stub_adapter(*, connected=True, usable_key=True, port=1):
    return SimpleNamespace(
        is_connected=connected,
        has_usable_api_key=lambda: usable_key,
        bind_host="127.0.0.1",
        bind_port=port,
    )


def _error_code(resp) -> str:
    return resp.json()["error"]["code"]


# ── refusals ────────────────────────────────────────────────────────────


def test_unknown_agent_is_404(api_client):
    resp = api_client.get("/api/openai/nobody/v1/models")
    assert resp.status_code == 404
    assert _error_code(resp) == "connector_not_configured"


def test_agent_without_the_connector_is_404(api_client, registry):
    """Same body as an unknown agent — this surface must not leak the roster."""
    resp = api_client.get(f"/api/openai/{AGENT}/v1/models")
    assert resp.status_code == 404
    assert _error_code(resp) == "connector_not_configured"


def test_keyless_connector_is_refused(api_client, registry):
    """A keyless server is loopback-safe only; proxying would expose it."""
    registry.register(AGENT, Platform.API_SERVER, _stub_adapter(usable_key=False))
    resp = api_client.get(f"/api/openai/{AGENT}/v1/models")
    assert resp.status_code == 403
    assert _error_code(resp) == "api_key_required"


def test_disconnected_connector_is_503(api_client, registry):
    registry.register(AGENT, Platform.API_SERVER, _stub_adapter(connected=False))
    resp = api_client.get(f"/api/openai/{AGENT}/v1/models")
    assert resp.status_code == 503
    assert _error_code(resp) == "connector_not_running"


def test_unreachable_upstream_is_502(api_client, registry):
    """Port 1 is closed: a refused connection is a gateway error, not a 500."""
    registry.register(AGENT, Platform.API_SERVER, _stub_adapter(port=1))
    resp = api_client.get(f"/api/openai/{AGENT}/v1/models")
    assert resp.status_code == 502
    assert _error_code(resp) == "connector_unreachable"


# ── live proxying against the real adapter app ──────────────────────────


@pytest.fixture
async def live_adapter(base_config, monkeypatch):
    """A real adapter app on an ephemeral port, registered for the agent."""
    monkeypatch.setenv("LIONCLAW_FAKE_TOKENS", "8")
    base_config.model = "fake:instant"
    base_config.name = AGENT

    from olano.core.builder import build_agent_async
    from olano.core.runner import AgentRunner

    runner = AgentRunner(await build_agent_async(base_config), base_config)
    adapter = APIServerAdapter(
        PlatformConfig.from_dict({"enabled": True, "api_key": API_KEY}),
        agent_name=AGENT,
    )
    adapter.set_agent_runner(runner)

    server = TestServer(adapter._build_app())
    await server.start_server()
    adapter._host, adapter._port = "127.0.0.1", server.port
    adapter._mark_connected()
    try:
        yield adapter
    finally:
        await server.close()


async def test_proxies_a_real_request(async_client, registry, live_adapter):
    registry.register(AGENT, Platform.API_SERVER, live_adapter)
    resp = await async_client.get(
        f"/api/openai/{AGENT}/v1/models",
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()["data"]] == [AGENT]


async def test_upstream_auth_still_applies_through_the_proxy(async_client, registry, live_adapter):
    """The adapter's own bearer check is the authentication on this surface."""
    registry.register(AGENT, Platform.API_SERVER, live_adapter)
    resp = await async_client.get(f"/api/openai/{AGENT}/v1/models")
    assert resp.status_code == 401


async def test_health_is_proxied(async_client, registry, live_adapter):
    registry.register(AGENT, Platform.API_SERVER, live_adapter)
    resp = await async_client.get(f"/api/openai/{AGENT}/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_detailed_health_is_not_exposed(async_client, registry, live_adapter):
    """It reports process-wide state and answers without auth — LAN only."""
    registry.register(AGENT, Platform.API_SERVER, live_adapter)
    resp = await async_client.get(f"/api/openai/{AGENT}/health/detailed")
    assert resp.status_code == 404


async def test_chat_completion_round_trip(async_client, registry, live_adapter):
    registry.register(AGENT, Platform.API_SERVER, live_adapter)
    resp = await async_client.post(
        f"/api/openai/{AGENT}/v1/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"model": "x", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status_code == 200
    assert "Fake response" in resp.json()["choices"][0]["message"]["content"]
    assert resp.headers.get("x-olano-session-id")


async def test_streaming_passes_through(async_client, registry, live_adapter):
    registry.register(AGENT, Platform.API_SERVER, live_adapter)
    async with async_client.stream(
        "POST",
        f"/api/openai/{AGENT}/v1/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"model": "x", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        # X-Accel-Buffering must survive: it is what stops an intermediary
        # from buffering the stream into uselessness.
        assert resp.headers.get("x-accel-buffering") == "no"
        body = "".join([chunk async for chunk in resp.aiter_text()])

    chunks = [
        json.loads(line[6:])
        for line in body.splitlines()
        if line.startswith("data: ") and line[6:].strip() != "[DONE]"
    ]
    text = "".join(
        c["choices"][0]["delta"].get("content", "")
        for c in chunks
        if c.get("choices") and "content" in c["choices"][0]["delta"]
    )
    assert "Fake response" in text


async def test_origin_is_not_forwarded(async_client, registry, live_adapter):
    """The adapter 403s unknown browser origins; proxied hops carry none."""
    registry.register(AGENT, Platform.API_SERVER, live_adapter)
    resp = await async_client.get(
        f"/api/openai/{AGENT}/v1/models",
        headers={"Authorization": f"Bearer {API_KEY}", "Origin": "https://evil.example"},
    )
    assert resp.status_code == 200


# ── middleware exemption ────────────────────────────────────────────────


def test_dashboard_token_is_not_required(tmp_workspace, monkeypatch, registry):
    """OpenAI SDKs carry the connector key, never a dashboard session."""
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_workspace / "home"))
    monkeypatch.setenv("LIONCLAW_DASHBOARD_TOKEN", "dashboard-secret")

    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    client = TestClient(create_api(_runtime()))
    registry.register(AGENT, Platform.API_SERVER, _stub_adapter(usable_key=False))

    # Gated dashboard surface still demands the token…
    assert client.get("/api/agents").status_code == 401
    # …while the OpenAI surface reaches its own handler (403 = the proxy's
    # own key rule, i.e. the request got past the dashboard gates).
    assert client.get(f"/api/openai/{AGENT}/v1/models").status_code == 403
