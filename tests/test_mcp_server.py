"""Inbound MCP server (``olano/mcp_server``): agents as tools, bearer-token auth.

The endpoint is exercised with the official ``mcp`` *client* SDK over an
in-process ASGI transport, so the handshake, tool listing and tool calls are
the real protocol. What the tests pin: no token ⇒ 401 with the RFC 9728
pointer, token scoping per agent, DNS-rebinding refusal, the home-wide switch,
the HTTPS requirement, the request context an inbound turn runs under, the
activity frames, JWT verification (issuer / audience / expiry), the token
store's at-rest shape, and the admin token API's gating.
"""

from __future__ import annotations

import asyncio
import json
import types
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette

from olano.config.system_settings import ExternalAccessJwtSettings, ExternalAccessSettings
from olano.mcp_server.auth import OlanoTokenVerifier, token_allows_agent
from olano.mcp_server.server import OlanoMcpServer, register_mcp_server_routes
from olano.mcp_server.tokens import McpTokenStore, parse_token_id

ORIGIN = "https://box.test"


class _Runner:
    def __init__(self, name: str, description: str = "") -> None:
        self.config = types.SimpleNamespace(
            name=name,
            description=description or f"{name} description",
            org=types.SimpleNamespace(title="Analyst"),
        )
        self.calls: list[tuple] = []
        self.fail = False

    async def ainvoke(self, message: str, thread_id: str | None = None) -> str:
        from olano.security.request_context import current_origin, get_request_context

        if self.fail:
            raise RuntimeError("model exploded")
        ctx = get_request_context()
        self.calls.append(
            (
                message,
                thread_id,
                ctx.source,
                ctx.user_id,
                ctx.is_owner,
                ctx.is_admin,
                current_origin(),
            )
        )
        return f"pong: {message}"


@pytest.fixture
def store(tmp_path: Path) -> McpTokenStore:
    return McpTokenStore(tmp_path / "tokens.json")


@pytest.fixture
def events(monkeypatch) -> list:
    out: list = []
    import olano.tools.talk_to as tt

    monkeypatch.setattr(tt, "_PUBLISHER", lambda t, p: out.append((t, p)))
    return out


@pytest.fixture
async def server(store):
    state = {"settings": ExternalAccessSettings(mcp={"enabled": True}), "origin": ORIGIN}
    runtime = types.SimpleNamespace(get_runner=lambda n: None)
    srv = OlanoMcpServer(
        runtime,
        settings_loader=lambda: state["settings"],
        origin_resolver=lambda: state["origin"],
        token_store=store,
    )
    srv.state = state  # type: ignore[attr-defined]
    srv.merli = _Runner("merli")  # type: ignore[attr-defined]
    srv.register_agent("merli", srv.merli)  # type: ignore[attr-defined]
    srv.register_agent("scout", _Runner("scout"))
    app = Starlette()
    register_mcp_server_routes(app, srv)
    srv.app = app  # type: ignore[attr-defined]
    yield srv
    await srv.stop()


def _http(server) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url=ORIGIN, headers={"Host": "box.test"}
    )


def _mcp_headers(token: str | None = None, host: str = "box.test") -> dict[str, str]:
    h = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Host": host,
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


_INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}


async def _session(server, token: str):
    """Open a real MCP client session against the in-process endpoint."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.app),
            base_url=ORIGIN,
            headers=headers,
            timeout=timeout,
            auth=auth,
        )

    return streamablehttp_client(
        f"{ORIGIN}/api/mcp",
        headers={"Authorization": f"Bearer {token}"},
        httpx_client_factory=factory,
    ), ClientSession


# ── auth boundary ─────────────────────────────────────────────────────────────


async def test_no_token_is_401_with_metadata_pointer(server):
    async with _http(server) as c:
        r = await c.post("/api/mcp", json=_INIT, headers=_mcp_headers())
    assert r.status_code == 401
    www = r.headers["www-authenticate"]
    assert www.startswith("Bearer ")
    assert 'error="invalid_token"' in www
    assert f'resource_metadata="{ORIGIN}/api/mcp/.well-known/oauth-protected-resource"' in www


async def test_wrong_revoked_and_expired_tokens_are_401(server, store):
    raw, rec = store.mint(label="t", agents=["merli"], ttl_days=30)
    async with _http(server) as c:
        bad = await c.post("/api/mcp", json=_INIT, headers=_mcp_headers(raw[:-3] + "xyz"))
        assert bad.status_code == 401
        store.revoke(rec.id)
        revoked = await c.post("/api/mcp", json=_INIT, headers=_mcp_headers(raw))
        assert revoked.status_code == 401
        raw2, rec2 = store.mint(label="t2", agents=["merli"], ttl_days=1)
        rec2.expires_at = "2000-01-01T00:00:00+00:00"
        store._save()  # the record object is the cached one; persist the edit
        expired = await c.post("/api/mcp", json=_INIT, headers=_mcp_headers(raw2))
        assert expired.status_code == 401


async def test_protected_resource_metadata_at_both_locations(server):
    async with _http(server) as c:
        alias = await c.get("/api/mcp/.well-known/oauth-protected-resource")
        derived = await c.get("/.well-known/oauth-protected-resource/api/mcp")
    assert alias.status_code == 200 and derived.status_code == 200
    body = alias.json()
    assert body["resource"] == f"{ORIGIN}/api/mcp"
    assert body["bearer_methods_supported"] == ["header"]
    assert set(body["scopes_supported"]) >= {"agents:*", "agent:merli", "agent:scout"}
    assert body["authorization_servers"]  # spec: at least one


async def test_metadata_advertises_external_issuer_when_jwt_configured(server):
    server.state["settings"] = ExternalAccessSettings(
        mcp={"enabled": True},
        jwt=ExternalAccessJwtSettings(issuer="https://idp.test/", jwks_url="https://idp.test/jwks"),
    )
    async with _http(server) as c:
        body = (await c.get("/api/mcp/.well-known/oauth-protected-resource")).json()
    # Olano's own authorization server first, then the external issuer.
    assert body["authorization_servers"] == [f"{ORIGIN}/api/oauth", "https://idp.test/"]
    server.state["settings"] = ExternalAccessSettings(
        mcp={"enabled": True},
        oauth={"enabled": False},
        jwt=ExternalAccessJwtSettings(issuer="https://idp.test/", jwks_url="https://idp.test/jwks"),
    )
    async with _http(server) as c:
        body = (await c.get("/api/mcp/.well-known/oauth-protected-resource")).json()
    assert body["authorization_servers"] == ["https://idp.test/"]


async def test_dns_rebinding_protection_refuses_foreign_host(server, store):
    raw, _ = store.mint(label="t", agents=["merli"], ttl_days=30)
    async with _http(server) as c:
        r = await c.post("/api/mcp", json=_INIT, headers=_mcp_headers(raw, host="evil.test"))
    assert r.status_code == 421


async def test_disabled_server_answers_404_even_with_a_valid_token(server, store):
    raw, _ = store.mint(label="t", agents=["merli"], ttl_days=30)
    server.state["settings"] = ExternalAccessSettings()
    async with _http(server) as c:
        r = await c.post("/api/mcp", json=_INIT, headers=_mcp_headers(raw))
        m = await c.get("/api/mcp/.well-known/oauth-protected-resource")
    assert r.status_code == 404 and r.json()["detail"] == "mcp_server_disabled"
    assert m.status_code == 404


async def test_plain_http_public_origin_is_refused_unless_opted_out(server, store):
    raw, _ = store.mint(label="t", agents=["merli"], ttl_days=30)
    server.state["origin"] = "http://box.test"
    async with _http(server) as c:
        r = await c.post("/api/mcp", json=_INIT, headers=_mcp_headers(raw))
        assert r.status_code == 403 and r.json()["detail"] == "mcp_server_requires_https"
        server.state["settings"] = ExternalAccessSettings(
            mcp={"enabled": True}, require_https=False
        )
        r = await c.post("/api/mcp", json=_INIT, headers=_mcp_headers(raw))
        assert r.status_code == 200
    # Loopback development origins are exempt from the HTTPS rule.
    server.state["settings"] = ExternalAccessSettings(mcp={"enabled": True})
    server.state["origin"] = "http://127.0.0.1:8765"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1:8765"
    ) as c:
        r = await c.post("/api/mcp", json=_INIT, headers=_mcp_headers(raw, host="127.0.0.1:8765"))
    assert r.status_code == 200


async def test_unknown_origin_is_503_not_a_guess(server, store):
    raw, _ = store.mint(label="t", agents=["merli"], ttl_days=30)
    server.state["origin"] = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url=ORIGIN
    ) as c:
        # No LIONCLAW_PUBLIC_URL and no learned origin: the Host header is the
        # only clue, and the server uses it (X-Forwarded-Proto aware).
        r = await c.post(
            "/api/mcp",
            json=_INIT,
            headers={**_mcp_headers(raw), "X-Forwarded-Proto": "https"},
        )
    assert r.status_code == 200


# ── the protocol, via the real client ─────────────────────────────────────────


async def test_tools_list_and_call_through_mcp_client(server, store, events):
    raw, _ = store.mint(label="claude-desktop", agents=["merli"], ttl_days=30)
    transport, ClientSession = await _session(server, raw)
    async with transport as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        names = {t.name for t in (await session.list_tools()).tools}
        assert names == {"olano_list_agents", "ask_merli", "ask_scout"}
        res = await session.call_tool("ask_merli", {"message": "hello", "thread_id": "t1"})
        assert res.isError is False
        assert res.content[0].text == "pong: hello"
        listed = await session.call_tool("olano_list_agents", {})
        assert "merli" in listed.content[0].text and "scout" not in listed.content[0].text

    # The turn ran as an external, non-owner caller on an mcp:: thread …
    (call,) = server.merli.calls
    message, thread_id, source, user_id, is_owner, is_admin, origin = call
    assert (message, thread_id, source, user_id) == (
        "hello",
        "mcp::claude-desktop::t1",
        "mcp",
        "mcp:claude-desktop",
    )
    assert is_owner is False and is_admin is False
    assert origin["root_source"] == "external" and origin["interactive"] is False
    # … and left the same activity frames talk_to_agent leaves.
    frames = [p for t, p in events if t == "agent_message"]
    assert all(f["kind"] == "inbound" and f["protocol"] == "mcp" for f in frames)
    assert [(f["kind"], f["phase"], f["from"], f["to"]) for f in frames] == [
        ("inbound", "send", "mcp:claude-desktop", "merli"),
        ("inbound", "reply", "mcp:claude-desktop", "merli"),
    ]
    assert frames[1]["reply"] == "pong: hello"


async def test_token_scope_is_enforced_per_agent(server, store):
    raw, _ = store.mint(label="narrow", agents=["merli"], ttl_days=30)
    transport, ClientSession = await _session(server, raw)
    async with transport as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        res = await session.call_tool("ask_scout", {"message": "hello"})
    assert res.isError is True
    assert "not authorized for agent 'scout'" in res.content[0].text


async def test_wildcard_token_reaches_every_exposed_agent(server, store):
    raw, _ = store.mint(label="wide", agents=["*"], ttl_days=30)
    transport, ClientSession = await _session(server, raw)
    async with transport as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        a = await session.call_tool("ask_merli", {"message": "a"})
        b = await session.call_tool("ask_scout", {"message": "b"})
    assert a.isError is False and b.isError is False


async def test_agent_failure_is_a_tool_error_with_an_error_frame(server, store, events):
    raw, _ = store.mint(label="t", agents=["merli"], ttl_days=30)
    server.merli.fail = True
    transport, ClientSession = await _session(server, raw)
    async with transport as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        res = await session.call_tool("ask_merli", {"message": "boom"})
    assert res.isError is True and "could not complete" in res.content[0].text
    reply = next(p for t, p in events if t == "agent_message" and p["phase"] == "reply")
    assert reply["error"].startswith("RuntimeError") and reply["reply"].startswith("[error]")


async def test_unregistered_agent_disappears_from_tools(server, store):
    raw, _ = store.mint(label="t", agents=["*"], ttl_days=30)
    server.unregister_agent("scout")
    transport, ClientSession = await _session(server, raw)
    async with transport as (read, write, _), ClientSession(read, write) as session:
        await session.initialize()
        names = {t.name for t in (await session.list_tools()).tools}
    assert names == {"olano_list_agents", "ask_merli"}
    assert server.exposed_names() == ["merli"]


# ── token store ───────────────────────────────────────────────────────────────


def test_token_store_hashes_at_rest_and_verifies_constant_time(store, tmp_path):
    raw, rec = store.mint(label="x", agents=["Merli"], ttl_days=7)
    assert raw.startswith("olano_mcp_") and parse_token_id(raw) == rec.id
    on_disk = json.loads((tmp_path / "tokens.json").read_text())
    assert raw not in json.dumps(on_disk)
    assert on_disk["tokens"][0]["sha256"] == rec.sha256
    assert (tmp_path / "tokens.json").stat().st_mode & 0o777 == 0o600
    assert store.verify(raw) is not None
    assert store.verify(raw + "x") is None
    assert store.verify("olano_mcp_deadbeef_nope") is None
    assert store.verify("Bearer whatever") is None
    assert rec.allows_agent("merli") and not rec.allows_agent("scout")
    assert rec.scope_list() == ["agent:Merli"]
    assert "sha256" not in rec.public()


def test_token_store_requires_label_and_agent(store):
    with pytest.raises(ValueError):
        store.mint(label="", agents=["merli"], ttl_days=1)
    with pytest.raises(ValueError):
        store.mint(label="x", agents=[], ttl_days=1)


def test_token_store_sees_external_edits(store, tmp_path):
    raw, rec = store.mint(label="x", agents=["merli"], ttl_days=None)
    assert rec.expires_at is None
    # A second store instance (another process, e.g. the CLI) revokes it.
    McpTokenStore(tmp_path / "tokens.json").revoke(rec.id)
    assert store.verify(raw) is None


# ── verifier: external JWTs ───────────────────────────────────────────────────


def _rsa_keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return pem, key.public_key()


async def test_jwt_verifier_binds_issuer_audience_and_expiry(store, monkeypatch):
    import time

    import jwt

    pem, public = _rsa_keypair()
    jwt_settings = ExternalAccessJwtSettings(
        issuer="https://idp.test/", jwks_url="https://idp.test/jwks"
    )
    verifier = OlanoTokenVerifier(
        resource_url=f"{ORIGIN}/api/mcp", jwt_settings=jwt_settings, store=store
    )
    monkeypatch.setattr(
        verifier,
        "_jwks",
        lambda: types.SimpleNamespace(
            get_signing_key_from_jwt=lambda tok: types.SimpleNamespace(key=public)
        ),
    )

    def mint(**over):
        claims = {
            "iss": "https://idp.test/",
            "aud": f"{ORIGIN}/api/mcp",
            "exp": int(time.time()) + 600,
            "sub": "user-1",
            "azp": "partner-app",
            "scope": "agent:merli openid",
        }
        claims.update(over)
        return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": "k1"})

    ok = await verifier.verify_token(mint())
    assert ok is not None
    assert ok.client_id == "partner-app" and ok.subject == "user-1"
    assert token_allows_agent(ok, "merli") and not token_allows_agent(ok, "scout")
    # Wrong audience (token minted for another API), wrong issuer, expired: all refused.
    assert await verifier.verify_token(mint(aud="https://other.test/api")) is None
    assert await verifier.verify_token(mint(iss="https://evil.test/")) is None
    assert (
        await verifier.verify_token(mint(exp=int(time.time()) - 120)) is None
    )  # beyond 30s leeway
    # Unsigned / alg=none never passes.
    assert await verifier.verify_token(jwt.encode({"iss": "x"}, "", algorithm="none")) is None
    # Required scopes are enforced when configured.
    verifier.jwt_settings = ExternalAccessJwtSettings(
        issuer="https://idp.test/", jwks_url="https://idp.test/jwks", required_scopes=["mcp:use"]
    )
    assert await verifier.verify_token(mint()) is None
    assert await verifier.verify_token(mint(scope="mcp:use agents:*")) is not None


async def test_jwt_path_is_off_unless_configured(store):
    verifier = OlanoTokenVerifier(resource_url=f"{ORIGIN}/api/mcp", store=store)
    assert await verifier.verify_token("aaa.bbb.ccc") is None
    assert await verifier.verify_token("") is None


# ── admin API + runtime wiring ────────────────────────────────────────────────


def test_admin_token_routes_are_admin_only_and_show_raw_token_once(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    runner = MagicMock()
    runner.config = MagicMock(model="fake:instant", description="d")
    runner.ainvoke = AsyncMock(return_value="hi")
    runtime = MagicMock()
    runtime._runners = {"merli": runner}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    runtime.scheduler = None
    runtime._self_learning = []
    app = create_api(runtime)
    assert runtime.mcp_server is app.state.olano_mcp_server
    paths = {getattr(r, "path", "") for r in app.router.routes}
    assert {"/api/mcp", "/api/mcp/.well-known/oauth-protected-resource"} <= paths

    client = TestClient(app)
    # Multi-user on: an anonymous caller is refused at the gate.
    client.post("/api/auth/bootstrap", json={"email": "a@x.com", "password": "pw", "name": "A"})
    anon = TestClient(app)
    assert anon.get("/api/mcp-server/status").status_code == 401
    # …while the (bootstrapped, logged-in) admin can mint and list.
    status = client.get("/api/mcp-server/status")
    assert status.status_code == 200 and status.json()["enabled"] is False
    minted = client.post(
        "/api/mcp-server/tokens", json={"label": "zapier", "agents": ["merli"], "ttl_days": 0}
    )
    assert minted.status_code == 200
    body = minted.json()
    assert body["token"].startswith("olano_mcp_") and body["record"]["expires_at"] is None
    assert "not exposed yet" in (body["warning"] or "")  # no runner registered it
    listed = client.get("/api/mcp-server/tokens").json()
    assert listed[0]["label"] == "zapier" and "sha256" not in listed[0]
    assert client.delete(f"/api/mcp-server/tokens/{body['record']['id']}").status_code == 200
    assert client.delete(f"/api/mcp-server/tokens/{body['record']['id']}").status_code == 404
    # The MCP endpoint itself is not behind the dashboard session gate: with
    # the server disabled it answers its own 404, not the gate's 401.
    assert anon.post("/api/mcp", json=_INIT, headers=_mcp_headers()).status_code == 404


def test_member_cannot_mint_tokens():
    from olano.runtime.auth import _ADMIN_ONLY_PREFIXES, is_mcp_server_path

    assert "/api/mcp-server" in _ADMIN_ONLY_PREFIXES
    assert is_mcp_server_path("/api/mcp") and not is_mcp_server_path("/api/mcp/directory")
    assert not is_mcp_server_path("/api/mcp/oauth/callback")


async def test_stop_is_idempotent(server):
    await server.stop()
    await server.stop()
    await asyncio.sleep(0)
