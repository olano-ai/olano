"""Tests for the public Meta Cloud API route (/api/meta/whatsapp/<agent>).

Meta checks the address with a GET and then POSTs signed events to it, with
no dashboard session. The surface is the check, the signature gate, the
refusals, the exemption from the dashboard gates (GET and POST, nothing
else), and the routing: an event goes to the connector that owns its phone
number ID, on the same Meta app, and nowhere else.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from olano.platforms.config import Platform, PlatformConfig
from olano.platforms.whatsapp_meta_api import WhatsAppMetaAPIAdapter
from olano.runtime.meta_webhook_routes import MAX_WEBHOOK_BODY_BYTES, WHATSAPP_ROUTE

AGENT = "meta-agent"
OTHER = "meta-other"
PATH = f"/api/meta/whatsapp/{AGENT}"
SECRET = "route-app-secret"


@pytest.fixture
def registry():
    from olano.platforms import get_platform_registry

    reg = get_platform_registry()
    yield reg
    reg.unregister_agent(AGENT)
    reg.unregister_agent(OTHER)


def _app(tmp_workspace, monkeypatch):
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_workspace / "home"))
    runtime = MagicMock()
    runtime._runners = {}
    from olano.runtime.api import create_api

    return create_api(runtime)


@pytest.fixture
def api_client(tmp_workspace, monkeypatch):
    from fastapi.testclient import TestClient

    return TestClient(_app(tmp_workspace, monkeypatch))


def _adapter(agent: str, phone_number_id: str, secret: str = SECRET) -> WhatsAppMetaAPIAdapter:
    return WhatsAppMetaAPIAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "access_token": "EAAtoken",
                "app_secret": secret,
                "waba_id": "1",
                "phone_number_id": phone_number_id,
            },
        ),
        agent_name=agent,
    )


def _signed(payload: dict[str, Any], secret: str = SECRET) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature-256": sig, "Content-Type": "application/json"}


def _event(phone_number_id: str, text: str = "hi") -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "1555",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [{"profile": {"name": "Ann"}, "wa_id": "12025550143"}],
                            "messages": [
                                {
                                    "from": "12025550143",
                                    "id": f"wamid.{phone_number_id}.{text}",
                                    "timestamp": "1750000000",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _collect(monkeypatch, adapter: WhatsAppMetaAPIAdapter) -> list[Any]:
    received: list[Any] = []

    async def _handle(event: Any) -> None:
        received.append(event)

    monkeypatch.setattr(adapter, "handle_message", _handle)
    monkeypatch.setattr(adapter, "_mark_read", False)
    return received


def test_the_route_and_the_adapter_agree_on_the_path():
    assert _adapter(AGENT, "1").webhook_path() == WHATSAPP_ROUTE.format(agent=AGENT)


def test_unknown_agent_is_404(api_client):
    assert api_client.get("/api/meta/whatsapp/nobody?hub.mode=subscribe").status_code == 404
    assert api_client.post("/api/meta/whatsapp/nobody", content=b"{}").status_code == 404


def test_metas_address_check_echoes_the_challenge_only_for_the_right_token(api_client, registry):
    adapter = _adapter(AGENT, "11")
    registry.register(AGENT, Platform.WHATSAPP_META_API, adapter)
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": adapter.verify_token,
        "hub.challenge": "1158201444",
    }
    ok = api_client.get(PATH, params=params)
    assert ok.status_code == 200 and ok.text == "1158201444"
    assert ok.headers["content-type"].startswith("text/plain")
    bad = api_client.get(PATH, params={**params, "hub.verify_token": "guess"})
    assert bad.status_code == 403


def test_an_unsigned_or_forged_delivery_is_refused(api_client, registry):
    adapter = _adapter(AGENT, "11")
    registry.register(AGENT, Platform.WHATSAPP_META_API, adapter)
    body, headers = _signed(_event("11"), secret="not-the-secret")
    assert api_client.post(PATH, content=body, headers=headers).status_code == 401
    assert api_client.post(PATH, content=body).status_code == 401


def test_oversized_body_is_refused_before_anything_reads_it(api_client, registry):
    registry.register(AGENT, Platform.WHATSAPP_META_API, _adapter(AGENT, "11"))
    resp = api_client.post(PATH, content=b"x" * (MAX_WEBHOOK_BODY_BYTES + 1))
    assert resp.status_code == 413


def test_the_dashboard_token_is_not_required_for_metas_calls(tmp_workspace, monkeypatch, registry):
    monkeypatch.setenv("LIONCLAW_DASHBOARD_TOKEN", "dashboard-secret")
    from fastapi.testclient import TestClient

    client = TestClient(_app(tmp_workspace, monkeypatch))
    adapter = _adapter(AGENT, "11")
    registry.register(AGENT, Platform.WHATSAPP_META_API, adapter)
    assert client.get("/api/agents").status_code == 401, "the dashboard is still gated"
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": adapter.verify_token,
        "hub.challenge": "7",
    }
    assert client.get(PATH, params=params).status_code == 200
    body, headers = _signed({"object": "page"})
    assert client.post(PATH, content=body, headers=headers).status_code == 200
    assert client.put(PATH, content=b"{}").status_code == 401, "only GET and POST are public"


async def test_each_event_reaches_the_connector_that_owns_its_number(
    tmp_workspace, monkeypatch, registry
):
    """An address set by hand at app level receives every number on the app."""
    import httpx

    mine = _adapter(AGENT, "11")
    theirs = _adapter(OTHER, "22")
    stranger = _adapter("meta-stranger", "33", secret="another-app")
    got_mine = _collect(monkeypatch, mine)
    got_theirs = _collect(monkeypatch, theirs)
    got_stranger = _collect(monkeypatch, stranger)
    registry.register(AGENT, Platform.WHATSAPP_META_API, mine)
    registry.register(OTHER, Platform.WHATSAPP_META_API, theirs)
    registry.register("meta-stranger", Platform.WHATSAPP_META_API, stranger)
    try:
        payload = _event("11", "for-me")
        payload["entry"][0]["changes"] += _event("22", "for-them")["entry"][0]["changes"]
        payload["entry"][0]["changes"] += _event("33", "other-app")["entry"][0]["changes"]
        payload["entry"][0]["changes"] += _event("44", "nobody")["entry"][0]["changes"]
        body, headers = _signed(payload)
        app = _app(tmp_workspace, monkeypatch)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            resp = await client.post(PATH, content=body, headers=headers)
        assert resp.status_code == 200
        for _ in range(20):
            if got_mine and got_theirs:
                break
            await asyncio.sleep(0.01)
        assert [e.text for e in got_mine] == ["for-me"]
        assert [e.text for e in got_theirs] == ["for-them"]
        assert got_stranger == [], "another Meta app's connector is never handed this app's events"
    finally:
        registry.unregister_agent("meta-stranger")
