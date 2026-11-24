"""Official-connector auth relay — box client, wire contract, reference service.

Covers the three moving parts of the official-connector path:

1. ``olano.core.connect_relay`` — config resolution (URL fallback to the MCP
   relay, shared identity) + the signed wire tuples, including refresh's
   ``sha256_hex(refresh_token)`` state-slot binding.
2. ``services/connect_relay/app.py`` — the reference relay's HTTP contract
   (register → callback-exchange → one-time pending → stateless refresh),
   exercised in-process via an ASGI transport with the outbound token POST
   stubbed at its ``_exchange`` seam.
3. Byte-parity between the two: payloads captured from the box client replay
   verbatim into the reference service and authenticate.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from olano.core import connect_relay, mcp_relay

_APP_PATH = Path(__file__).resolve().parents[1] / "services" / "connect_relay" / "app.py"

SECRET = "shared-connect-secret"

PROVIDERS_JSON = {
    "testprov": {
        "client_id": "cid-official",
        "client_secret": "csec-official",
        "token_url": "https://provider.example/{tenant}/token",
        "client_auth": "post",
        "pkce": True,
        "params": {"tenant": "common"},
    }
}


def _load_connect_app(secret: str = SECRET, providers: dict | None = None):
    """Import the standalone connect relay fresh with secret + providers set."""
    import sys

    spec = importlib.util.spec_from_file_location("_connect_relay_under_test", _APP_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass processing needs the module registered
    spec.loader.exec_module(module)
    module.RELAY_SECRET = secret
    module.PROVIDERS = module._load_providers(json.dumps(providers or PROVIDERS_JSON))
    module._STATES.clear()
    return module


def _stub_exchange(module, *, status=200, payload: dict[str, Any] | None = None):
    """Replace the reference service's outbound token POST; returns the call log."""
    calls: list[dict[str, Any]] = []
    payload = (
        payload
        if payload is not None
        else {"access_token": "AT-1", "refresh_token": "RT-1", "expires_in": 3600}
    )

    async def fake(url, data, headers, auth):
        calls.append({"url": url, "data": dict(data), "headers": dict(headers), "auth": auth})
        return status, dict(payload), json.dumps(payload)

    module._exchange = fake
    return calls


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://relay")


CFG = mcp_relay.RelayConfig(url="http://relay", secret=SECRET, instance_id="inst-1")


# ── Box-side config resolution ───────────────────────────────────────────────


def test_connect_relay_url_prefers_own_env_then_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.example.com")
    monkeypatch.delenv("LIONCLAW_CONNECT_RELAY_URL", raising=False)
    # Default: /connect lives on the same host as the MCP relay.
    assert connect_relay.connect_relay_url() == "https://auth.example.com"
    # A split deployment overrides it without touching the MCP relay config.
    monkeypatch.setenv("LIONCLAW_CONNECT_RELAY_URL", "https://connect.example.com/")
    assert connect_relay.connect_relay_url() == "https://connect.example.com"


def test_resolve_connect_relay_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIONCLAW_CONNECT_RELAY_URL", raising=False)
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_URL", raising=False)
    assert connect_relay.resolve_connect_relay_config() is None

    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.example.com")
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_SECRET", raising=False)
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_PRIVATE_KEY", raising=False)
    with pytest.raises(RuntimeError):
        connect_relay.resolve_connect_relay_config()

    # Same identity as the MCP relay: one enrolled credential signs both flows.
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_SECRET", SECRET)
    cfg = connect_relay.resolve_connect_relay_config()
    assert cfg is not None
    assert cfg.secret == SECRET
    assert connect_relay.connect_callback_uri(cfg) == "https://auth.example.com/connect/callback"


def test_refresh_binding_is_sha256_hex_of_utf8() -> None:
    token = "tok-cañón-✓"
    assert connect_relay.refresh_binding(token) == hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── Box-side wire tuples (captured at the httpx seam) ────────────────────────


class _FakeResponse:
    def __init__(self, status_code=200, payload: Any = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_register_wire_tuple_signs_connect_register(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, Any] = {}

    def fake_post(url, json=None, timeout=None):
        sent.update(url=url, body=json)
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(httpx, "post", fake_post)
    connect_relay.register_connect_state(
        CFG, "state-1", provider="testprov", code_verifier="v" * 43, params={"tenant": "t1"}
    )
    assert sent["url"] == "http://relay/connect/register"
    body = sent["body"]
    assert body["provider"] == "testprov"
    assert body["code_verifier"] == "v" * 43
    assert body["params"] == {"tenant": "t1"}
    # The signature verifies under the frozen scheme with the NEW action name —
    # and not under the MCP flow's, so one flow can never authorize the other.
    assert mcp_relay.verify(
        SECRET, body["sig"], "connect_register", "inst-1", "state-1", body["ts"]
    )
    assert not mcp_relay.verify(SECRET, body["sig"], "register", "inst-1", "state-1", body["ts"])


def test_register_refusal_raises_with_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx, "post", lambda *a, **kw: _FakeResponse(400, {"detail": "provider_unavailable"})
    )
    with pytest.raises(RuntimeError, match="provider_unavailable"):
        connect_relay.register_connect_state(
            CFG, "state-1", provider="nope", code_verifier=None, params={}
        )


def test_refresh_wire_binds_token_hash_in_state_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, Any] = {}

    def fake_post(url, json=None, timeout=None):
        sent.update(url=url, body=json)
        return _FakeResponse(200, {"ok": True, "token_response": {"access_token": "AT-2"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    payload = connect_relay.refresh_via_relay(CFG, "testprov", "RT-1", params={"tenant": "t1"})
    assert payload == {"access_token": "AT-2"}
    body = sent["body"]
    binding = hashlib.sha256(b"RT-1").hexdigest()
    assert mcp_relay.verify(SECRET, body["sig"], "connect_refresh", "inst-1", binding, body["ts"])
    # A swapped-in token changes the recomputed binding → the sig no longer holds.
    other = hashlib.sha256(b"RT-stolen").hexdigest()
    assert not mcp_relay.verify(SECRET, body["sig"], "connect_refresh", "inst-1", other, body["ts"])


def test_refresh_provider_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **kw: _FakeResponse(
            502, {"error": "provider_error", "status": 400, "detail": "invalid_grant"}
        ),
    )
    with pytest.raises(RuntimeError, match="invalid_grant"):
        connect_relay.refresh_via_relay(CFG, "testprov", "RT-dead")


# ── Reference service contract (in-process ASGI) ─────────────────────────────


def _signed_register(state: str, *, provider="testprov", verifier="v" * 43, params=None) -> dict:
    ts = str(int(time.time()))
    return {
        "instance_id": CFG.instance_id,
        "state": state,
        "ts": ts,
        "sig": mcp_relay.sign(SECRET, "connect_register", CFG.instance_id, state, ts),
        "provider": provider,
        "code_verifier": verifier,
        "params": params or {},
    }


def _signed_pending(state: str, instance_id: str = CFG.instance_id) -> dict:
    ts = str(int(time.time()))
    return {
        "instance_id": instance_id,
        "state": state,
        "ts": ts,
        "sig": mcp_relay.sign(SECRET, "connect_pending", instance_id, state, ts),
    }


async def test_reference_full_flow_register_exchange_pending_once() -> None:
    module = _load_connect_app()
    calls = _stub_exchange(module)
    state = "conn-state-1"
    async with await _client(module.app) as client:
        reg = await client.post("/connect/register", json=_signed_register(state))
        assert reg.status_code == 200

        pend = await client.get("/connect/pending", params=_signed_pending(state))
        assert pend.json()["status"] == "pending"

        # Provider redirect: the exchange runs AT the callback, centrally.
        cb = await client.get(
            "/connect/callback", params={"state": state, "code": "THECODE", "realmId": "9130"}
        )
        assert cb.status_code == 200 and "Authorization received" in cb.text
        assert calls[0]["url"] == "https://provider.example/common/token"
        assert calls[0]["data"]["code"] == "THECODE"
        assert calls[0]["data"]["code_verifier"] == "v" * 43
        assert calls[0]["data"]["client_secret"] == "csec-official"

        got = (await client.get("/connect/pending", params=_signed_pending(state))).json()
        assert got["status"] == "ready"
        bundle = got["bundle"]
        assert bundle["provider"] == "testprov"
        assert bundle["token_response"]["access_token"] == "AT-1"
        # Full callback query minus the spent code — box hooks depend on it.
        assert bundle["callback_query"] == {"state": state, "realmId": "9130"}
        # One-time delivery.
        assert (await client.get("/connect/pending", params=_signed_pending(state))).json() == {
            "status": "unknown"
        }


async def test_reference_rejects_cross_flow_and_cross_tenant() -> None:
    module = _load_connect_app()
    _stub_exchange(module)
    state = "conn-state-2"
    async with await _client(module.app) as client:
        # A signature minted for the MCP flow's action must not authorize.
        ts = str(int(time.time()))
        body = _signed_register(state)
        body["sig"] = mcp_relay.sign(SECRET, "register", CFG.instance_id, state, ts)
        body["ts"] = ts
        assert (await client.post("/connect/register", json=body)).status_code == 401

        assert (
            await client.post("/connect/register", json=_signed_register(state))
        ).status_code == 200
        await client.get("/connect/callback", params={"state": state, "code": "C"})
        # Correctly signed, wrong instance → unknown, never the bundle.
        assert (
            await client.get("/connect/pending", params=_signed_pending(state, "inst-2"))
        ).json() == {"status": "unknown"}


async def test_reference_register_gates_provider_params_and_pkce() -> None:
    module = _load_connect_app()
    _stub_exchange(module)
    async with await _client(module.app) as client:
        r = await client.post("/connect/register", json=_signed_register("s1", provider="nope"))
        assert r.status_code == 400 and r.json()["detail"] == "provider_unavailable"
        r = await client.post(
            "/connect/register", json=_signed_register("s2", params={"evil": "x"})
        )
        assert r.status_code == 400 and r.json()["detail"] == "invalid_params"
        r = await client.post("/connect/register", json=_signed_register("s3", verifier=None))
        assert r.status_code == 400  # pkce provider without a verifier fails fast


async def test_reference_exchange_failure_reaches_pending_as_error() -> None:
    module = _load_connect_app()
    _stub_exchange(module, status=400, payload={"error": "invalid_grant"})
    state = "conn-bad"
    async with await _client(module.app) as client:
        await client.post("/connect/register", json=_signed_register(state))
        cb = await client.get("/connect/callback", params={"state": state, "code": "C"})
        assert "Authorization problem" in cb.text
        got = (await client.get("/connect/pending", params=_signed_pending(state))).json()
        assert got["status"] == "error" and "invalid_grant" in got["error"]


async def test_reference_refresh_ok_and_binding() -> None:
    module = _load_connect_app()
    calls = _stub_exchange(module, payload={"access_token": "AT-2", "refresh_token": "RT-2"})
    ts = str(int(time.time()))
    binding = hashlib.sha256(b"RT-1").hexdigest()
    body = {
        "instance_id": CFG.instance_id,
        "ts": ts,
        "sig": mcp_relay.sign(SECRET, "connect_refresh", CFG.instance_id, binding, ts),
        "provider": "testprov",
        "refresh_token": "RT-1",
        "params": {},
    }
    async with await _client(module.app) as client:
        ok = await client.post("/connect/refresh", json=body)
        assert ok.json() == {
            "ok": True,
            "token_response": {"access_token": "AT-2", "refresh_token": "RT-2"},
        }
        assert calls[-1]["data"]["grant_type"] == "refresh_token"
        # The relay recomputes the hash from the body: a swapped token → 401.
        assert (
            await client.post("/connect/refresh", json={**body, "refresh_token": "RT-stolen"})
        ).status_code == 401


async def test_reference_refresh_maps_provider_rejection_to_502() -> None:
    module = _load_connect_app()
    _stub_exchange(module, status=400, payload={"error": "invalid_grant"})
    ts = str(int(time.time()))
    binding = hashlib.sha256(b"RT-x").hexdigest()
    body = {
        "instance_id": CFG.instance_id,
        "ts": ts,
        "sig": mcp_relay.sign(SECRET, "connect_refresh", CFG.instance_id, binding, ts),
        "provider": "testprov",
        "refresh_token": "RT-x",
    }
    async with await _client(module.app) as client:
        r = await client.post("/connect/refresh", json=body)
        assert r.status_code == 502
        payload = r.json()
        assert payload["error"] == "provider_error" and payload["status"] == 400


# ── Box client ↔ reference service byte-parity ───────────────────────────────


async def test_box_signed_payloads_replay_into_reference_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact bytes the box client sends must authenticate on the service."""
    module = _load_connect_app()
    _stub_exchange(module)
    captured: list[dict[str, Any]] = []

    def fake_post(url, json=None, timeout=None):
        captured.append({"url": url, "body": json})
        return _FakeResponse(200, {"ok": True, "token_response": {"access_token": "AT-2"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    connect_relay.register_connect_state(
        CFG, "parity-state", provider="testprov", code_verifier="v" * 43, params={}
    )
    connect_relay.refresh_via_relay(CFG, "testprov", "RT-parity")

    async with await _client(module.app) as client:
        reg = await client.post("/connect/register", json=captured[0]["body"])
        assert reg.status_code == 200
        ref = await client.post("/connect/refresh", json=captured[1]["body"])
        assert ref.status_code == 200 and ref.json()["ok"] is True
