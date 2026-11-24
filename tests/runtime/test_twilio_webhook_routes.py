"""Tests for the public Twilio inbound route (/api/twilio/whatsapp/<agent>).

The route is how a customer's WhatsApp message reaches an agent with nothing
for the operator to paste anywhere, and it is unauthenticated until the
adapter checks ``X-Twilio-Signature``. So the surface is the refusals (no
adapter, adapter not running, oversized body), the POST-only exemption from
the dashboard gates, and one real round trip through a real adapter.
"""

from __future__ import annotations

import asyncio
import urllib.parse
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from olano.platforms.config import Platform, PlatformConfig
from olano.platforms.twilio_signature import compute_signature
from olano.runtime.twilio_webhook_routes import MAX_WEBHOOK_BODY_BYTES

AGENT = "wa-agent"
PATH = f"/api/twilio/whatsapp/{AGENT}"
AUTH_TOKEN = "route-test-token"
CUSTOMER = "whatsapp:+15551234567"


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


def _app(tmp_workspace, monkeypatch):
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_workspace / "home"))

    from olano.runtime.api import create_api

    return create_api(_runtime())


@pytest.fixture
def api_client(tmp_workspace, monkeypatch):
    from fastapi.testclient import TestClient

    return TestClient(_app(tmp_workspace, monkeypatch))


class _StubAdapter:
    def __init__(self, *, connected: bool = True, status: int = 200):
        self.is_connected = connected
        self.status = status
        self.calls: list[tuple[bytes, str, list[str]]] = []

    async def receive_webhook(self, raw: bytes, signature: str, *, candidate_urls=()) -> int:
        self.calls.append((raw, signature, list(candidate_urls)))
        return self.status


# ── refusals ────────────────────────────────────────────────────────────


def test_unknown_agent_is_404(api_client):
    resp = api_client.post("/api/twilio/whatsapp/nobody", content=b"a=b")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/xml")


def test_agent_without_the_connector_is_404(api_client, registry):
    """Same answer as an unknown agent: this surface must not leak the roster."""
    registry.register(AGENT, Platform.API_SERVER, SimpleNamespace(is_connected=True))
    resp = api_client.post(PATH, content=b"a=b")
    assert resp.status_code == 404


def test_disconnected_connector_is_503(api_client, registry):
    adapter = _StubAdapter(connected=False)
    registry.register(AGENT, Platform.WHATSAPP_TWILIO, adapter)
    resp = api_client.post(PATH, content=b"a=b")
    assert resp.status_code == 503
    assert adapter.calls == []


def test_oversized_body_is_refused_before_the_adapter_sees_it(api_client, registry):
    adapter = _StubAdapter()
    registry.register(AGENT, Platform.WHATSAPP_TWILIO, adapter)
    resp = api_client.post(PATH, content=b"a" * (MAX_WEBHOOK_BODY_BYTES + 1))
    assert resp.status_code == 413
    assert adapter.calls == []


def test_the_adapter_decides_and_sees_the_request_url(api_client, registry):
    adapter = _StubAdapter(status=403)
    registry.register(AGENT, Platform.WHATSAPP_TWILIO, adapter)

    resp = api_client.post(
        PATH,
        content=b"EventType=onMessageAdded",
        headers={
            "X-Twilio-Signature": "sig",
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": "acme.example",
        },
    )

    assert resp.status_code == 403, "the adapter's verdict is the response"
    ((raw, signature, urls),) = adapter.calls
    assert raw == b"EventType=onMessageAdded"
    assert signature == "sig"
    assert f"https://acme.example{PATH}" in urls


# ── middleware exemption ────────────────────────────────────────────────


def test_dashboard_token_is_not_required_for_a_post(tmp_workspace, monkeypatch, registry):
    """Twilio carries a signature, never a dashboard session."""
    monkeypatch.setenv("LIONCLAW_DASHBOARD_TOKEN", "dashboard-secret")
    from fastapi.testclient import TestClient

    client = TestClient(_app(tmp_workspace, monkeypatch))
    adapter = _StubAdapter(status=200)
    registry.register(AGENT, Platform.WHATSAPP_TWILIO, adapter)

    # Gated dashboard surface still demands the token...
    assert client.get("/api/agents").status_code == 401
    # ...and so does anything but POST under the prefix...
    assert client.get(PATH).status_code == 401
    # ...while Twilio's POST reaches the adapter.
    assert client.post(PATH, content=b"a=b").status_code == 200
    assert len(adapter.calls) == 1


# ── a real round trip ───────────────────────────────────────────────────


async def test_a_signed_message_reaches_the_agent(tmp_workspace, monkeypatch, registry):
    import httpx

    from olano.platforms.whatsapp_twilio import WhatsAppTwilioAdapter

    adapter = WhatsAppTwilioAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "account_sid": "AC" + "0" * 32,
                "auth_token": AUTH_TOKEN,
                "from_number": "+12025550111",
            },
        ),
        agent_name=AGENT,
    )
    webhook_url = f"https://acme.example{PATH}"
    adapter._webhook_url = webhook_url
    adapter._mark_connected()
    received: list[Any] = []

    async def _handle(event):
        received.append(event)

    monkeypatch.setattr(adapter, "handle_message", _handle)
    registry.register(AGENT, Platform.WHATSAPP_TWILIO, adapter)

    fields = {
        "EventType": "onMessageAdded",
        "Author": CUSTOMER,
        "Body": "hello from WhatsApp",
        "ConversationSid": "CH" + "1" * 32,
        "MessageSid": "IM-route",
    }
    headers = {
        "X-Twilio-Signature": compute_signature(AUTH_TOKEN, webhook_url, fields),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    app = _app(tmp_workspace, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        forged = await client.post(
            PATH,
            content=urllib.parse.urlencode(fields),
            headers={**headers, "X-Twilio-Signature": "x"},
        )
        resp = await client.post(PATH, content=urllib.parse.urlencode(fields), headers=headers)

    assert forged.status_code == 403
    assert resp.status_code == 200
    for _ in range(20):
        if received:
            break
        await asyncio.sleep(0.01)
    assert [e.text for e in received] == ["hello from WhatsApp"]
    assert received[0].source.chat_id == CUSTOMER
