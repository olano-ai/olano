"""Central MCP OAuth relay: box client, relay service contract, and the
relay-mode authorization driver.

Covers the three moving parts of the domain-less OAuth path:

1. ``olano.core.mcp_relay``: config resolution + HMAC signing (box side).
2. ``services/mcp_oauth_relay/app.py`` — the relay HTTP contract (register →
   callback → pending), exercised in-process via an ASGI transport.
3. ``mcp_setup.start_mcp_authorization`` relay mode — the background pump that
   shuttles the code from the relay into the waiting flow, plus per-agent token
   isolation and redirect-target precedence.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest

from olano.core import mcp_relay, mcp_setup

_RELAY_APP_PATH = Path(__file__).resolve().parents[1] / "services" / "mcp_oauth_relay" / "app.py"


def _load_relay_app(secret: str):
    """Import the standalone relay app module fresh and set its shared secret."""
    import sys

    spec = importlib.util.spec_from_file_location("_relay_app_under_test", _RELAY_APP_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass processing needs the module registered
    spec.loader.exec_module(module)
    module.RELAY_SECRET = secret
    module._STATES.clear()
    return module


# ── Box-side config + signing ────────────────────────────────────────────────


def test_sign_verify_roundtrip() -> None:
    sig = mcp_relay.sign("s3cr3t", "register", "inst", "state-1", "123")
    assert mcp_relay.verify("s3cr3t", sig, "register", "inst", "state-1", "123")
    assert not mcp_relay.verify("s3cr3t", sig, "register", "inst", "state-1", "124")
    assert not mcp_relay.verify("other", sig, "register", "inst", "state-1", "123")


def test_resolve_relay_config_none_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_URL", raising=False)
    assert mcp_relay.resolve_relay_config() is None


def test_resolve_relay_config_requires_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.example.com")
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        mcp_relay.resolve_relay_config()


def test_resolve_relay_config_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.example.com/")
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_SECRET", "shared")
    cfg = mcp_relay.resolve_relay_config()
    assert cfg is not None
    assert cfg.url == "https://auth.example.com"  # trailing slash trimmed
    assert cfg.callback_uri == "https://auth.example.com/mcp/callback"
    assert cfg.instance_id  # generated + persisted


def test_instance_id_is_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIONCLAW_INSTANCE_ID", raising=False)
    first = mcp_relay.instance_id()
    assert first and mcp_relay.instance_id() == first  # persisted across calls


# ── Relay service HTTP contract (in-process ASGI) ────────────────────────────


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://relay")


async def test_relay_register_callback_pending_happy_path() -> None:
    app = _load_relay_app("shared")
    cfg = mcp_relay.RelayConfig(url="http://relay", secret="shared", instance_id="inst-1")
    state = "state-abc"

    async with await _client(app.app) as client:
        # Patch the box client's httpx calls to go through the ASGI transport.
        # (register_state / poll_code build their own signed payloads.)
        import time

        ts = str(int(time.time()))
        reg = await client.post(
            "/mcp/register",
            json={
                "instance_id": cfg.instance_id,
                "state": state,
                "ts": ts,
                "sig": mcp_relay.sign(cfg.secret, "register", cfg.instance_id, state, ts),
            },
        )
        assert reg.status_code == 200

        # Before the browser callback → pending.
        p1 = await client.get(
            "/mcp/pending",
            params={
                "instance_id": cfg.instance_id,
                "state": state,
                "ts": ts,
                "sig": mcp_relay.sign(cfg.secret, "pending", cfg.instance_id, state, ts),
            },
        )
        assert p1.json()["status"] == "pending"

        # Provider redirect lands on the public callback.
        cb = await client.get("/mcp/callback", params={"code": "the-code", "state": state})
        assert cb.status_code == 200
        assert "Authorization received" in cb.text

        # The box polls → gets the code exactly once.
        p2 = await client.get(
            "/mcp/pending",
            params={
                "instance_id": cfg.instance_id,
                "state": state,
                "ts": ts,
                "sig": mcp_relay.sign(cfg.secret, "pending", cfg.instance_id, state, ts),
            },
        )
        body = p2.json()
        assert body["status"] == "ready" and body["code"] == "the-code"

        # Second poll → already delivered (unknown/consumed).
        p3 = await client.get(
            "/mcp/pending",
            params={
                "instance_id": cfg.instance_id,
                "state": state,
                "ts": ts,
                "sig": mcp_relay.sign(cfg.secret, "pending", cfg.instance_id, state, ts),
            },
        )
        assert p3.json()["status"] == "unknown"


async def test_relay_rejects_bad_signature() -> None:
    app = _load_relay_app("shared")
    async with await _client(app.app) as client:
        resp = await client.post(
            "/mcp/register",
            json={"instance_id": "x", "state": "s", "ts": "123", "sig": "deadbeef"},
        )
        assert resp.status_code == 401


async def test_relay_callback_unknown_state_is_harmless() -> None:
    app = _load_relay_app("shared")
    async with await _client(app.app) as client:
        cb = await client.get("/mcp/callback", params={"code": "x", "state": "never-registered"})
        assert cb.status_code == 200
        assert "problem" in cb.text.lower()


# ── Redirect-target precedence ───────────────────────────────────────────────


def test_resolve_redirect_uri_prefers_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.example.com")
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_SECRET", "shared")
    uri, cfg = mcp_setup._resolve_redirect_uri({"url": "https://mcp.x/mcp"}, None)
    assert uri == "https://auth.example.com/mcp/callback"
    assert cfg is not None


def test_resolve_redirect_uri_falls_back_to_public_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_URL", raising=False)
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://box.example.com")
    uri, cfg = mcp_setup._resolve_redirect_uri({"url": "https://mcp.x/mcp"}, None)
    assert uri == "https://box.example.com/api/mcp/oauth/callback"
    assert cfg is None


def test_resolve_redirect_uri_errors_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIONCLAW_MCP_RELAY_URL", raising=False)
    monkeypatch.delenv("LIONCLAW_PUBLIC_URL", raising=False)
    mcp_setup.remember_public_origin(None)
    mcp_setup._LAST_SEEN_ORIGIN = None
    with pytest.raises(ValueError, match="redirect target"):
        mcp_setup._resolve_redirect_uri({"url": "https://mcp.x/mcp"}, None)


# ── Per-agent / per-account token isolation ──────────────────────────────────


def test_token_store_isolated_per_agent() -> None:
    url = "https://mcp.notion.com/mcp"
    a = mcp_setup.token_store_path("sales", url)
    b = mcp_setup.token_store_path("support", url)
    assert a != b  # two agents → two account token files for the same MCP
    # Same agent + url is stable (so refresh/reuse finds the tokens).
    assert mcp_setup.token_store_path("sales", url) == a


# ── Relay-mode driver end-to-end (faked provider + faked relay) ──────────────


async def test_start_mcp_authorization_relay_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_URL", "https://auth.example.com")
    monkeypatch.setenv("LIONCLAW_MCP_RELAY_SECRET", "shared")

    class _Tool:
        name = "pages.search"

    captured: dict[str, object] = {}
    relay_store: dict[str, str] = {}

    async def _drive(url, transport, *, auth=None, headers=None, log=None):
        await captured["redirect"]("https://provider/authorize?state=st-relay")  # type: ignore[operator]
        code, state = await captured["callback"]()  # type: ignore[operator]
        assert code == "relay-code" and state == "st-relay"
        return [_Tool()]

    def _capture_make(
        agent, url, *, redirect_uri=None, redirect_handler=None, callback_handler=None, **_kw
    ):
        captured["redirect"] = redirect_handler
        captured["callback"] = callback_handler
        captured["redirect_uri"] = redirect_uri
        return object()

    async def _fake_register(cfg, state, *, timeout=10.0):
        relay_store["state"] = state  # relay now knows this pending state

    async def _fake_poll(cfg, state, *, timeout=10.0):
        # Simulate the browser having hit the relay callback with the code.
        if relay_store.get("state") == state:
            return {"status": "ready", "code": "relay-code"}
        return {"status": "pending"}

    monkeypatch.setattr(mcp_setup, "make_oauth_provider", _capture_make)
    monkeypatch.setattr(mcp_setup, "_connect_and_list_tools", _drive)
    monkeypatch.setattr(mcp_relay, "register_state", _fake_register)
    monkeypatch.setattr(mcp_relay, "poll_code", _fake_poll)

    session = mcp_setup.start_mcp_authorization(
        "merli", {"url": "https://mcp.notion.com/mcp", "transport": "http"}
    )

    for _ in range(200):
        await asyncio.sleep(0.02)
        if session.status in {"authorized", "error", "expired"}:
            break
    assert session.status == "authorized", session.public()
    assert session.tool_count == 1
    assert session.state == "st-relay"
    # The flow (in its background task) built its provider against the relay callback.
    assert captured["redirect_uri"] == "https://auth.example.com/mcp/callback"
