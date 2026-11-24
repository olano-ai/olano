"""External access, inbound side: Olano's own OAuth 2.1 authorization server and
the A2A server, plus the consent/admin API.

Everything runs against the real SDK components in-process: the ``mcp`` SDK's
authorization-server handlers (DCR, /authorize, /token, metadata) driven by
Olano's provider, the ``mcp`` client SDK against the MCP endpoint, and Olano's
own outbound A2A client (``olano.a2a.client``) against the inbound A2A server —
so a token minted through the OAuth flow is exercised over both protocols.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import types
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from starlette.applications import Starlette

from olano.a2a.server import OlanoA2AServer, register_a2a_server_routes
from olano.config.system_settings import ExternalAccessSettings
from olano.mcp_server.oauth import OAuthClientStore, agents_from_scopes
from olano.mcp_server.server import OlanoMcpServer, register_mcp_server_routes
from olano.mcp_server.tokens import McpTokenStore

ORIGIN = "https://box.test"
CALLBACK = "https://client.test/callback"


class _Runner:
    def __init__(self, name: str) -> None:
        self.config = types.SimpleNamespace(
            name=name,
            description=f"{name} roster description",
            display_name=name.title(),
            external_access=types.SimpleNamespace(
                mcp=True, a2a=True, description=f"Answers questions as {name}. Read-only."
            ),
        )
        self.calls: list[tuple] = []

    async def ainvoke(self, message: str, thread_id: str | None = None) -> str:
        from olano.security.request_context import current_origin, get_request_context

        ctx = get_request_context()
        self.calls.append(
            (
                message,
                thread_id,
                ctx.source,
                ctx.user_id,
                ctx.is_owner,
                current_origin()["root_source"],
            )
        )
        return f"pong: {message}"


@pytest.fixture
def stack(tmp_path: Path, monkeypatch):
    """MCP + A2A servers sharing one token store, mounted on one ASGI app."""
    state = {
        "settings": ExternalAccessSettings(mcp={"enabled": True}, a2a={"enabled": True}),
        "origin": ORIGIN,
    }
    store = McpTokenStore(tmp_path / "tokens.json")
    runtime = types.SimpleNamespace(get_runner=lambda n: None, a2a_server=None)
    a2a = OlanoA2AServer(
        runtime,
        settings_loader=lambda: state["settings"],
        origin_resolver=lambda: state["origin"],
        token_store=store,
    )
    runtime.a2a_server = a2a
    mcp = OlanoMcpServer(
        runtime,
        settings_loader=lambda: state["settings"],
        origin_resolver=lambda: state["origin"],
        token_store=store,
    )
    mcp.oauth.client_store = OAuthClientStore(tmp_path / "clients.json")
    merli, scout = _Runner("merli"), _Runner("scout")
    mcp.register_agent("merli", merli)
    a2a.register_agent("merli", merli)
    a2a.register_agent("scout", scout)
    app = Starlette()
    register_mcp_server_routes(app, mcp)
    register_a2a_server_routes(app, a2a)
    import olano.tools.talk_to as tt

    events: list = []
    monkeypatch.setattr(tt, "_PUBLISHER", lambda t, p: events.append((t, p)))
    yield types.SimpleNamespace(
        app=app, mcp=mcp, a2a=a2a, store=store, state=state, merli=merli, scout=scout, events=events
    )
    import asyncio

    asyncio.get_event_loop().run_until_complete(mcp.stop()) if False else None


def _http(stack) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack.app), base_url=ORIGIN, headers={"Host": "box.test"}
    )


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode()
    return verifier, challenge.rstrip("=")


async def _register(c: httpx.AsyncClient, **over) -> dict:
    body = {
        "redirect_uris": [CALLBACK],
        "client_name": "Claude",
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        **over,
    }
    r = await c.post("/api/oauth/register", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _authorize(c: httpx.AsyncClient, client_id: str, challenge: str, **over) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CALLBACK,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "st-1",
        "scope": "agents:*",
        "resource": f"{ORIGIN}/api/mcp",
        **over,
    }
    r = await c.get("/api/oauth/authorize", params=params)
    assert r.status_code == 302, r.text
    location = r.headers["location"]
    assert location.startswith(f"{ORIGIN}/oauth/consent?request=")
    return parse_qs(urlparse(location).query)["request"][0]


async def _exchange(
    c: httpx.AsyncClient, client_id: str, code: str, verifier: str
) -> httpx.Response:
    return await c.post(
        "/api/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )


# ── discovery ─────────────────────────────────────────────────────────────────


async def test_authorization_server_is_discoverable_everywhere(stack):
    async with _http(stack) as c:
        prm = (await c.get("/api/mcp/.well-known/oauth-protected-resource")).json()
        assert prm["authorization_servers"] == [f"{ORIGIN}/api/oauth"]
        docs = []
        for path in (
            "/.well-known/oauth-authorization-server/api/oauth",
            "/.well-known/openid-configuration/api/oauth",
            "/api/oauth/.well-known/oauth-authorization-server",
            "/api/oauth/.well-known/openid-configuration",
        ):
            r = await c.get(path)
            assert r.status_code == 200, path
            docs.append(r.json())
    meta = docs[0]
    assert all(d == meta for d in docs)
    assert meta["issuer"] == f"{ORIGIN}/api/oauth"
    assert meta["authorization_endpoint"] == f"{ORIGIN}/api/oauth/authorize"
    assert meta["token_endpoint"] == f"{ORIGIN}/api/oauth/token"
    assert meta["registration_endpoint"] == f"{ORIGIN}/api/oauth/register"
    assert meta["revocation_endpoint"] == f"{ORIGIN}/api/oauth/revoke"
    assert meta["code_challenge_methods_supported"] == ["S256"]
    assert set(meta["scopes_supported"]) == {"agents:*", "agent:merli", "agent:scout"}
    # Public PKCE clients (no secret) are served, so the metadata must say so:
    # the SDK default lists only the two secret-based methods.
    for key in (
        "token_endpoint_auth_methods_supported",
        "revocation_endpoint_auth_methods_supported",
    ):
        assert set(meta[key]) == {"client_secret_post", "client_secret_basic", "none"}, key


async def test_oauth_endpoints_404_while_both_surfaces_are_off(stack):
    stack.state["settings"] = ExternalAccessSettings()
    async with _http(stack) as c:
        assert (await c.get("/api/oauth/.well-known/oauth-authorization-server")).status_code == 404
        assert (await c.post("/api/oauth/register", json={})).status_code == 404


async def test_oauth_can_be_switched_off_independently(stack):
    stack.state["settings"] = ExternalAccessSettings(
        mcp={"enabled": True}, oauth={"enabled": False}
    )
    async with _http(stack) as c:
        prm = (await c.get("/api/mcp/.well-known/oauth-protected-resource")).json()
        assert prm["authorization_servers"] == [f"{ORIGIN}/"]
        assert (await c.get("/api/oauth/.well-known/oauth-authorization-server")).status_code == 404


# ── the flow ──────────────────────────────────────────────────────────────────


async def test_full_authorization_code_flow_with_consent(stack):
    async with _http(stack) as c:
        client = await _register(c)
        assert client.get("client_secret") is None  # public client, as it asked
        assert client["scope"] == "agents:*"  # default scope filled in
        verifier, challenge = _pkce()
        request_id = await _authorize(c, client["client_id"], challenge)

        pending = stack.mcp.oauth.pending(request_id)
        assert pending["client_name"] == "Claude"
        assert pending["requested_all"] is True
        assert pending["offered_agents"] == ["merli", "scout"]  # both surfaces count
        assert pending["redirect_host"] == "client.test"

        # Nothing is issued until the admin says so; a code cannot be traded yet.
        redirect = stack.mcp.oauth.approve(request_id, agents=["merli"], subject="admin@x.test")
        q = parse_qs(urlparse(redirect).query)
        assert urlparse(redirect).netloc == "client.test" and q["state"] == ["st-1"]
        code = q["code"][0]

        # Wrong verifier is refused; the right one is exchanged once.
        bad = await _exchange(c, client["client_id"], code, "not-the-verifier")
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_grant"
        ok = await _exchange(c, client["client_id"], code, verifier)
        assert ok.status_code == 200, ok.text
        tokens = ok.json()
        assert tokens["token_type"] == "Bearer"
        assert tokens["scope"] == "agent:merli"  # narrowed to what the admin granted
        assert tokens["expires_in"] == 3600
        assert tokens["access_token"].startswith("olano_mcp_")
        assert tokens["refresh_token"].startswith("olano_rt_")
        replay = await _exchange(c, client["client_id"], code, verifier)
        assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"

    # The issued token is an ordinary record: OAuth kind, bound to client + admin.
    (rec,) = [t for t in stack.store.list() if t.active]
    assert rec.kind == "oauth" and rec.client_id == client["client_id"]
    assert rec.subject == "admin@x.test" and rec.agents == ["merli"] and rec.label == "Claude"


async def test_issued_token_works_on_mcp_and_a2a_and_scope_is_enforced(stack):
    async with _http(stack) as c:
        client = await _register(c)
        verifier, challenge = _pkce()
        request_id = await _authorize(c, client["client_id"], challenge)
        code = parse_qs(
            urlparse(stack.mcp.oauth.approve(request_id, agents=["merli"], subject="a")).query
        )["code"][0]
        tokens = (await _exchange(c, client["client_id"], code, verifier)).json()
    access = tokens["access_token"]

    # MCP, via the official client.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    def factory(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stack.app),
            base_url=ORIGIN,
            headers=headers,
            timeout=timeout,
            auth=auth,
        )

    async with (
        streamablehttp_client(
            f"{ORIGIN}/api/mcp",
            headers={"Authorization": f"Bearer {access}"},
            httpx_client_factory=factory,
        ) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        res = await session.call_tool("ask_merli", {"message": "hi"})
        assert res.isError is False and res.content[0].text == "pong: hi"

    # A2A, via Olano's own outbound client — token scoped to merli only.
    from olano.a2a import client as a2a_client
    from olano.config.agent_config import AgentConfig

    cfg = AgentConfig(
        name="caller",
        remote_agents=[
            {
                "name": "merli",
                "url": f"{ORIGIN}/api/a2a/agents/merli",
                "auth": "bearer",
                "credential": access,
            },
            {
                "name": "scout",
                "url": f"{ORIGIN}/api/a2a/agents/scout",
                "auth": "bearer",
                "credential": access,
            },
        ],
    )
    a2a_client.reset_caches()
    a2a_client._load_agent_config = lambda name: cfg  # type: ignore[assignment]
    a2a_client._HTTPX_CLIENT_FACTORY = lambda entry: httpx.AsyncClient(  # type: ignore[assignment]
        transport=httpx.ASGITransport(app=stack.app), base_url=ORIGIN, headers={"Host": "box.test"}
    )
    try:
        reply = await a2a_client.send_remote_message("caller", "merli", "hello over a2a")
        assert reply.text == "pong: hello over a2a" and reply.state == "TASK_STATE_COMPLETED"
        with pytest.raises(a2a_client.RemoteAgentError, match="403"):
            await a2a_client.send_remote_message("caller", "scout", "hello")
    finally:
        a2a_client._HTTPX_CLIENT_FACTORY = None  # type: ignore[assignment]
        a2a_client.reset_caches()

    # Both turns ran as outside, non-owner callers rooted at nobody.
    (mcp_call, a2a_call) = stack.merli.calls
    assert mcp_call[2:] == ("mcp", "mcp:Claude", False, "external")
    assert a2a_call[2] == "a2a" and a2a_call[3] == "a2a:Claude" and a2a_call[4] is False
    assert a2a_call[1].startswith("a2a::Claude::")
    # (the outbound client used above logs its own `remote` frames as the caller;
    # only the inbound half is under test here)
    kinds = [
        (p["kind"], p["phase"], p["from"])
        for t, p in stack.events
        if t == "agent_message" and p["kind"] == "inbound"
    ]
    assert kinds == [
        ("inbound", "send", "mcp:Claude"),
        ("inbound", "reply", "mcp:Claude"),
        ("inbound", "send", "a2a:Claude"),
        ("inbound", "reply", "a2a:Claude"),
    ]


async def test_refresh_rotates_and_revocation_kills(stack):
    async with _http(stack) as c:
        client = await _register(c)
        verifier, challenge = _pkce()
        request_id = await _authorize(c, client["client_id"], challenge)
        code = parse_qs(
            urlparse(
                stack.mcp.oauth.approve(request_id, agents=["merli", "scout"], subject="a")
            ).query
        )["code"][0]
        first = (await _exchange(c, client["client_id"], code, verifier)).json()
        assert first["scope"] == "agent:merli agent:scout"

        r = await c.post(
            "/api/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": first["refresh_token"],
                "client_id": client["client_id"],
                "scope": "agent:merli",  # narrowing on refresh is allowed
            },
        )
        assert r.status_code == 200, r.text
        second = r.json()
        assert second["scope"] == "agent:merli"
        assert second["access_token"] != first["access_token"]
        # Rotation: the previous access AND refresh tokens are dead.
        assert stack.store.verify(first["access_token"]) is None
        assert stack.store.verify_refresh(first["refresh_token"]) is None
        again = await c.post(
            "/api/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": first["refresh_token"],
                "client_id": client["client_id"],
            },
        )
        assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
        # Widening beyond the grant is refused.
        widen = await c.post(
            "/api/oauth/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": second["refresh_token"],
                "client_id": client["client_id"],
                "scope": "agent:merli agents:*",
            },
        )
        assert widen.status_code == 400 and widen.json()["error"] == "invalid_scope"

        # RFC 7009 revocation by a public client; unknown tokens still 200.
        r = await c.post(
            "/api/oauth/revoke",
            data={"token": second["access_token"], "client_id": client["client_id"]},
        )
        assert r.status_code == 200
        assert stack.store.verify(second["access_token"]) is None
        r = await c.post(
            "/api/oauth/revoke",
            data={"token": "olano_mcp_nope_x", "client_id": client["client_id"]},
        )
        assert r.status_code == 200
        # …but only the client's own tokens: a different client cannot revoke them.
        other = await _register(c, client_name="Other")
        raw, _ = stack.store.mint(
            label="x", agents=["merli"], ttl_days=1, kind="oauth", client_id=client["client_id"]
        )
        r = await c.post("/api/oauth/revoke", data={"token": raw, "client_id": other["client_id"]})
        assert r.status_code == 200 and stack.store.verify(raw) is not None


async def test_consent_can_be_declined_and_requests_expire(stack, monkeypatch):
    async with _http(stack) as c:
        client = await _register(c)
        _, challenge = _pkce()
        request_id = await _authorize(c, client["client_id"], challenge)
    redirect = stack.mcp.oauth.deny(request_id)
    q = parse_qs(urlparse(redirect).query)
    assert q["error"] == ["access_denied"] and q["state"] == ["st-1"]
    assert stack.mcp.oauth.pending(request_id) is None
    with pytest.raises(KeyError):
        stack.mcp.oauth.approve(request_id, agents=["merli"], subject="a")
    # Granting an agent that is not exposed is refused.
    async with _http(stack) as c:
        _, challenge = _pkce()
        request_id = await _authorize(c, client["client_id"], challenge)
    with pytest.raises(ValueError, match="at least one exposed agent"):
        stack.mcp.oauth.approve(request_id, agents=["nobody"], subject="a")


async def test_registration_refuses_plain_http_redirects_and_can_be_disabled(stack):
    async with _http(stack) as c:
        r = await c.post(
            "/api/oauth/register",
            json={"redirect_uris": ["http://evil.test/cb"], "client_name": "x"},
        )
        assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"
        ok = await c.post(
            "/api/oauth/register",
            json={"redirect_uris": ["http://127.0.0.1:8123/cb"], "client_name": "local dev"},
        )
        assert ok.status_code == 201
        stack.state["settings"] = ExternalAccessSettings(
            mcp={"enabled": True}, oauth={"dynamic_registration": False}
        )
        stack.mcp._invalidate_build()
        r = await c.post("/api/oauth/register", json={"redirect_uris": [CALLBACK]})
        assert r.status_code == 404  # the SDK omits the route entirely
        meta = (await c.get("/api/oauth/.well-known/oauth-authorization-server")).json()
        assert "registration_endpoint" not in meta


async def test_authorize_refuses_foreign_resource_and_unknown_scopes(stack):
    async with _http(stack) as c:
        client = await _register(c, scope="agents:* agent:merli")
        _, challenge = _pkce()
        r = await c.get(
            "/api/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client["client_id"],
                "redirect_uri": CALLBACK,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": "https://other.test/api",
            },
        )
        # Errors go back to the client's redirect URI, per RFC 6749.
        assert r.status_code == 302
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["error"] == ["invalid_request"]
        r = await c.get(
            "/api/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client["client_id"],
                "redirect_uri": CALLBACK,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "scope": "agent:nobody",
            },
        )
        assert parse_qs(urlparse(r.headers["location"]).query)["error"] == ["invalid_scope"]


def test_agents_from_scopes():
    assert agents_from_scopes(["agent:merli", "agents:*", "openid", "agent:scout"]) == [
        "*",
        "merli",
        "scout",
    ]
    assert agents_from_scopes(None) == []


# ── A2A inbound specifics ─────────────────────────────────────────────────────


async def test_agent_card_is_public_and_describes_the_exposure(stack):
    async with _http(stack) as c:
        r = await c.get("/api/a2a/agents/merli/.well-known/agent-card.json")
        assert r.status_code == 200
        card = r.json()
        assert card["name"] == "Merli"
        assert (
            card["description"] == "Answers questions as merli. Read-only."
        )  # never the system prompt
        assert card["supportedInterfaces"][0]["url"] == f"{ORIGIN}/api/a2a/agents/merli"
        assert set(card["securitySchemes"]) == {"bearer", "oauth2"}
        flow = card["securitySchemes"]["oauth2"]["oauth2SecurityScheme"]["flows"][
            "authorizationCode"
        ]
        assert flow["authorizationUrl"] == f"{ORIGIN}/api/oauth/authorize"
        assert flow["tokenUrl"] == f"{ORIGIN}/api/oauth/token"
        assert flow["pkceRequired"] is True
        assert card["skills"][0]["id"] == "chat"
        # Unknown agent: 404; disabled home-wide: 404 for everything.
        assert (
            await c.get("/api/a2a/agents/nobody/.well-known/agent-card.json")
        ).status_code == 404
        stack.state["settings"] = ExternalAccessSettings(mcp={"enabled": True})
        assert (await c.get("/api/a2a/agents/merli/.well-known/agent-card.json")).status_code == 404


async def test_a2a_rpc_requires_a_token_and_checks_host(stack):
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}}
    async with _http(stack) as c:
        r = await c.post("/api/a2a/agents/merli", json=rpc)
        assert r.status_code == 401
        assert 'error="invalid_token"' in r.headers["www-authenticate"]
        raw, _ = stack.store.mint(label="t", agents=["scout"], ttl_days=1)
        r = await c.post(
            "/api/a2a/agents/merli", json=rpc, headers={"Authorization": f"Bearer {raw}"}
        )
        assert r.status_code == 403 and "insufficient_scope" in r.headers["www-authenticate"]
        r = await c.post(
            "/api/a2a/agents/merli",
            json=rpc,
            headers={"Authorization": f"Bearer {raw}", "Host": "evil.test"},
        )
        assert r.status_code == 421
        assert (await c.get("/api/a2a/agents/merli")).status_code == 405


def _www_param(header: str, key: str) -> str:
    import re

    m = re.search(rf'{key}="([^"]*)"', header)
    assert m, f"{key} missing from {header!r}"
    return m.group(1)


async def test_a2a_401_points_at_the_agents_own_protected_resource_metadata(stack):
    """RFC 9728 §3.3: metadata reached through ``WWW-Authenticate`` must name the
    URL the client called as its ``resource``, else the client discards it. The
    A2A 401 used to point at the MCP endpoint's document (``/api/mcp``)."""
    rpc = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}}
    agent_url = f"{ORIGIN}/api/a2a/agents/merli"
    async with _http(stack) as c:
        r = await c.post("/api/a2a/agents/merli", json=rpc)
        assert r.status_code == 401
        metadata_url = _www_param(r.headers["www-authenticate"], "resource_metadata")
        assert metadata_url == f"{agent_url}/.well-known/oauth-protected-resource"

        followed = await c.get(metadata_url)
        assert followed.status_code == 200
        doc = followed.json()
        assert doc["resource"] == agent_url
        assert doc["scopes_supported"] == ["agent:merli"]  # what the card's oauth2 flow asks
        assert doc["resource_name"] == "Olano agent Merli"
        mcp_doc = (await c.get("/api/mcp/.well-known/oauth-protected-resource")).json()
        assert doc["authorization_servers"] == mcp_doc["authorization_servers"]
        # The RFC-derived location serves the same document.
        derived = await c.get("/.well-known/oauth-protected-resource/api/a2a/agents/merli")
        assert derived.status_code == 200 and derived.json() == doc
        # Each agent is its own resource.
        scout = (await c.get("/api/a2a/agents/scout/.well-known/oauth-protected-resource")).json()
        assert scout["resource"] == f"{ORIGIN}/api/a2a/agents/scout"

        # A token for another agent: the 403 carries the same pointer.
        raw, _ = stack.store.mint(label="t", agents=["scout"], ttl_days=1)
        r = await c.post(
            "/api/a2a/agents/merli", json=rpc, headers={"Authorization": f"Bearer {raw}"}
        )
        assert r.status_code == 403
        assert _www_param(r.headers["www-authenticate"], "resource_metadata") == metadata_url

        # Same gates as the card: unknown agent, wrong method, disabled home-wide.
        for path in (
            "/api/a2a/agents/nobody/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/api/a2a/agents/nobody",
        ):
            assert (await c.get(path)).status_code == 404, path
        assert (await c.post(metadata_url, json={})).status_code == 405
        stack.state["settings"] = ExternalAccessSettings(
            a2a={"enabled": True}, oauth={"enabled": False}
        )
        assert (await c.get(metadata_url)).json()["authorization_servers"] == [f"{ORIGIN}/"]
        stack.state["settings"] = ExternalAccessSettings(mcp={"enabled": True})
        assert (await c.get(metadata_url)).status_code == 404


async def test_a2a_accepts_external_jwts_bound_to_the_agents_resource(stack, monkeypatch):
    """With an external issuer and no explicit audience, a JWT is bound to the
    resource the metadata names (the agent's URL); the shared ``/api/a2a/agents``
    identifier an earlier release bound them to still works; a token minted for
    the MCP endpoint does not."""
    import time

    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from olano.config.system_settings import ExternalAccessJwtSettings
    from olano.mcp_server.auth import OlanoTokenVerifier

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    monkeypatch.setattr(
        OlanoTokenVerifier,
        "_jwks",
        lambda self: types.SimpleNamespace(
            get_signing_key_from_jwt=lambda tok: types.SimpleNamespace(key=key.public_key())
        ),
    )
    stack.state["settings"] = ExternalAccessSettings(
        a2a={"enabled": True},
        jwt=ExternalAccessJwtSettings(issuer="https://idp.test/", jwks_url="https://idp.test/jwks"),
    )

    def mint(aud: str) -> str:
        claims = {
            "iss": "https://idp.test/",
            "aud": aud,
            "exp": int(time.time()) + 600,
            "sub": "user-1",
            "azp": "partner-app",
            "scope": "agent:merli",
        }
        return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": "k1"})

    rpc = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}}
    async with _http(stack) as c:
        doc = (await c.get("/api/a2a/agents/merli/.well-known/oauth-protected-resource")).json()
        assert doc["authorization_servers"] == [f"{ORIGIN}/api/oauth", "https://idp.test/"]
        for aud, status in (
            (doc["resource"], 200),
            (f"{ORIGIN}/api/a2a/agents", 200),
            (f"{ORIGIN}/api/mcp", 401),
        ):
            r = await c.post(
                "/api/a2a/agents/merli",
                json=rpc,
                headers={"Authorization": f"Bearer {mint(aud)}"},
            )
            assert r.status_code == status, (aud, r.text)


async def test_a2a_turn_failure_is_a_failed_task_not_a_500(stack):
    async def boom(message, thread_id=None):
        raise RuntimeError("model exploded")

    stack.scout.ainvoke = boom  # type: ignore[assignment]
    raw, _ = stack.store.mint(label="t", agents=["scout"], ttl_days=1)
    from olano.a2a import client as a2a_client
    from olano.config.agent_config import AgentConfig

    cfg = AgentConfig(
        name="caller",
        remote_agents=[
            {
                "name": "scout",
                "url": f"{ORIGIN}/api/a2a/agents/scout",
                "auth": "bearer",
                "credential": raw,
            }
        ],
    )
    a2a_client.reset_caches()
    a2a_client._load_agent_config = lambda name: cfg  # type: ignore[assignment]
    a2a_client._HTTPX_CLIENT_FACTORY = lambda entry: httpx.AsyncClient(  # type: ignore[assignment]
        transport=httpx.ASGITransport(app=stack.app), base_url=ORIGIN, headers={"Host": "box.test"}
    )
    try:
        reply = await a2a_client.send_remote_message("caller", "scout", "hello")
    finally:
        a2a_client._HTTPX_CLIENT_FACTORY = None  # type: ignore[assignment]
        a2a_client.reset_caches()
    assert reply.state == "TASK_STATE_FAILED"
    assert "could not complete" in reply.text
    error_frames = [p for t, p in stack.events if t == "agent_message" and p.get("error")]
    assert error_frames and error_frames[0]["from"] == "a2a:t"


# ── admin + consent API through the real app ──────────────────────────────────


def test_consent_and_client_admin_routes(monkeypatch):
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
    assert runtime.a2a_server is app.state.olano_a2a_server
    paths = {getattr(r, "path", "") for r in app.router.routes}
    assert "/api/a2a/agents/{name}" in paths and "/api/oauth/authorize" in paths
    assert "/.well-known/oauth-protected-resource/api/a2a/agents/{name}" in paths
    # No prefix catch-all: it would shadow the consent API registered after it.
    assert not any(p.startswith("/api/oauth/{") for p in paths)

    admin = TestClient(app)
    admin.post("/api/auth/bootstrap", json={"email": "a@x.com", "password": "pw", "name": "A"})
    anon = TestClient(app)
    # Consent API is a dashboard surface: anonymous callers are gated out…
    assert anon.get("/api/oauth/consent/whatever").status_code == 401
    # …the AS endpoints themselves are not (they answer for themselves).
    assert anon.get("/api/oauth/.well-known/oauth-authorization-server").status_code in (404, 503)
    # Unknown / expired request ids 404 for the admin.
    assert admin.get("/api/oauth/consent/nope").status_code == 404
    r = admin.post("/api/oauth/consent/nope", json={"approve": True, "agents": ["merli"]})
    assert r.status_code == 404
    # OAuth client listing exists and is empty; deleting an unknown client 404s.
    assert admin.get("/api/mcp-server/oauth/clients").json() == []
    assert admin.delete("/api/mcp-server/oauth/clients/none").status_code == 404
    status = admin.get("/api/mcp-server/status").json()
    assert status["a2a"]["enabled"] is False and status["oauth_enabled"] is True
    # A member (non-admin) is refused everywhere on this surface.
    from olano.runtime.auth import _ADMIN_ONLY_PREFIXES, is_external_access_path

    assert "/api/oauth/consent" in _ADMIN_ONLY_PREFIXES
    assert is_external_access_path("/api/oauth/token") and not is_external_access_path(
        "/api/oauth/consent/x"
    )
    assert is_external_access_path("/api/a2a/agents/merli") and not is_external_access_path(
        "/api/a2a/threads"
    )
    assert is_external_access_path("/.well-known/oauth-authorization-server/api/oauth")


def test_oauth_sign_in_is_approvable_through_the_real_app():
    """register, authorize, consent page, Allow, token: all through create_api's
    route table. The consent API shares the /api/oauth prefix with the
    authorization server, and a prefix catch-all registered ahead of it answered
    both the lookup and the Allow click with the AS router's bare 404, so a live
    pending request read as "unknown or expired" and no sign-in could complete.
    The ``stack`` fixture mounts the AS alone, so only the real app shows it."""
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
    mcp = app.state.olano_mcp_server
    mcp._settings_loader = lambda: ExternalAccessSettings(mcp={"enabled": True})
    mcp._origin_resolver = lambda: ORIGIN
    mcp.register_agent("merli", _Runner("merli"))

    c = TestClient(app, base_url=ORIGIN)
    c.post("/api/auth/bootstrap", json={"email": "a@x.com", "password": "pw", "name": "A"})
    reg = c.post(
        "/api/oauth/register",
        json={
            "redirect_uris": [CALLBACK],
            "client_name": "Claude",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier, challenge = _pkce()
    r = c.get(
        "/api/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st-1",
            "resource": f"{ORIGIN}/api/mcp",
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    request_id = parse_qs(urlparse(r.headers["location"]).query)["request"][0]

    pending = c.get(f"/api/oauth/consent/{request_id}")
    assert pending.status_code == 200, pending.text
    assert pending.json()["client_name"] == "Claude"
    assert pending.json()["offered_agents"] == ["merli"]

    decided = c.post(
        f"/api/oauth/consent/{request_id}", json={"approve": True, "agents": ["merli"]}
    )
    assert decided.status_code == 200, decided.text
    back = urlparse(decided.json()["redirect_to"])
    assert f"{back.scheme}://{back.netloc}{back.path}" == CALLBACK
    assert parse_qs(back.query)["state"] == ["st-1"]

    token = c.post(
        "/api/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": parse_qs(back.query)["code"][0],
            "redirect_uri": CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    assert agents_from_scopes(token.json()["scope"].split()) == ["merli"]


def test_app_routes_exactly_the_paths_the_authorization_server_serves(stack):
    """``register_mcp_server_routes`` mounts ``oauth.ENDPOINT_PATHS`` instead of a
    prefix catch-all, so the list must match what ``build_routes`` serves under
    the prefix: a new AS endpoint missing from it would be unreachable."""
    from olano.mcp_server.oauth import ENDPOINT_PATHS, OAUTH_PREFIX

    served = {r.path for r in stack.mcp.oauth.build_routes() if r.path.startswith(OAUTH_PREFIX)}
    assert served == set(ENDPOINT_PATHS)


# ── client secrets at rest ────────────────────────────────────────────────────


async def test_confidential_client_secret_is_hashed_at_rest_and_verified_both_ways(stack):
    """DCR hands a confidential client its secret once; the store keeps only a
    digest, and the token + revocation endpoints verify it in either RFC 6749
    style (``client_secret_post`` form field, ``client_secret_basic`` header)."""
    from olano.mcp_server.oauth import hash_client_secret

    async with _http(stack) as c:
        for method in ("client_secret_post", "client_secret_basic"):
            client = await _register(
                c, token_endpoint_auth_method=method, client_name=f"svc-{method}"
            )
            cid, secret = client["client_id"], client["client_secret"]
            assert isinstance(secret, str) and len(secret) == 64  # issued in the clear, once
            assert secret not in stack.mcp.oauth.client_store.path.read_text()
            stored = stack.mcp.oauth.client_store.get(cid)
            assert stored is not None and stored.client_secret == hash_client_secret(secret)
            listed = next(r for r in stack.mcp.oauth.client_store.list() if r["client_id"] == cid)
            assert listed["has_secret"] is True and "client_secret" not in listed

            def creds(form: dict, value: str) -> dict:
                if method == "client_secret_post":
                    return {"data": {**form, "client_secret": value}}
                return {"data": form, "auth": (cid, value)}

            verifier, challenge = _pkce()
            rid = await _authorize(c, cid, challenge)
            redirect = stack.mcp.oauth.approve(rid, agents=["merli"], subject="admin@x.test")
            code = parse_qs(urlparse(redirect).query)["code"][0]
            form = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": CALLBACK,
                "client_id": cid,
                "code_verifier": verifier,
            }
            missing = await c.post("/api/oauth/token", data=form)
            assert missing.status_code == 401 and missing.json()["error"] == "unauthorized_client"
            wrong = await c.post("/api/oauth/token", **creds(form, "not-the-secret"))
            assert wrong.status_code == 401, wrong.text
            ok = await c.post("/api/oauth/token", **creds(form, secret))
            assert ok.status_code == 200, ok.text
            tokens = ok.json()

            refresh = {
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": cid,
            }
            assert (await c.post("/api/oauth/token", data=refresh)).status_code == 401
            rotated = await c.post("/api/oauth/token", **creds(refresh, secret))
            assert rotated.status_code == 200, rotated.text
            access = rotated.json()["access_token"]
            assert stack.store.verify(access) is not None

            revoke = {"token": access, "client_id": cid}
            assert (await c.post("/api/oauth/revoke", **creds(revoke, "nope"))).status_code == 401
            assert (await c.post("/api/oauth/revoke", **creds(revoke, secret))).status_code == 200
            assert stack.store.verify(access) is None


def test_legacy_plaintext_client_secrets_are_hashed_on_first_load(tmp_path: Path):
    from olano.mcp_server.oauth import client_secret_matches, hash_client_secret

    path = tmp_path / "clients.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "clients": [
                    {
                        "client_id": "legacy",
                        "client_secret": "plain-as-issued",
                        "redirect_uris": [CALLBACK],
                        "token_endpoint_auth_method": "client_secret_post",
                        "grant_types": ["authorization_code"],
                        "response_types": ["code"],
                        "registered_at": 1,
                        "registered_by": "dcr",
                    }
                ],
            }
        )
    )
    store = OAuthClientStore(path)
    rec = store.get("legacy")
    assert rec is not None and rec.client_secret == hash_client_secret("plain-as-issued")
    assert "plain-as-issued" not in path.read_text()  # rewritten on that first read
    assert client_secret_matches(rec.client_secret, "plain-as-issued")
    assert not client_secret_matches(rec.client_secret, "plain-as-issued ")
    assert not client_secret_matches(None, "x") and not client_secret_matches(
        rec.client_secret, None
    )
    # A record that still holds plaintext verifies too, so an upgrade never locks a client out.
    assert client_secret_matches("plain-as-issued", "plain-as-issued")


# ── task records outlive the process ──────────────────────────────────────────


async def test_a2a_task_records_survive_a_server_restart(stack, monkeypatch):
    from olano.a2a import client as a2a_client
    from olano.a2a.task_store import default_task_store_path
    from olano.config.agent_config import AgentConfig

    raw, _ = stack.store.mint(label="poller", agents=["scout"], ttl_days=1)
    cfg = AgentConfig(
        name="caller",
        remote_agents=[
            {
                "name": "scout",
                "url": f"{ORIGIN}/api/a2a/agents/scout",
                "auth": "bearer",
                "credential": raw,
            }
        ],
    )

    def factory(app):
        return lambda entry: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN, headers={"Host": "box.test"}
        )

    monkeypatch.setattr(a2a_client, "_load_agent_config", lambda name: cfg)
    monkeypatch.setattr(a2a_client, "_HTTPX_CLIENT_FACTORY", factory(stack.app))
    a2a_client.reset_caches()
    try:
        reply = await a2a_client.send_remote_message("caller", "scout", "hello")
        assert reply.state == "TASK_STATE_COMPLETED" and reply.task_id
        assert stack.a2a.task_store.path == default_task_store_path()

        # "Restart": drop the server (which closes the store) and bring up a fresh
        # one over the same home and token store. The caller's task id still resolves.
        await stack.a2a.stop()
        runtime2 = types.SimpleNamespace(get_runner=lambda n: None, a2a_server=None)
        a2a2 = OlanoA2AServer(
            runtime2,
            settings_loader=lambda: stack.state["settings"],
            origin_resolver=lambda: stack.state["origin"],
            token_store=stack.store,
        )
        runtime2.a2a_server = a2a2
        a2a2.register_agent("scout", stack.scout)
        app2 = Starlette()
        register_a2a_server_routes(app2, a2a2)
        monkeypatch.setattr(a2a_client, "_HTTPX_CLIENT_FACTORY", factory(app2))
        a2a_client.reset_caches()

        again = await a2a_client.get_remote_task("caller", "scout", reply.task_id)
        assert again.task_id == reply.task_id
        assert again.state == "TASK_STATE_COMPLETED" and "pong: hello" in again.text
        await a2a2.stop()
    finally:
        a2a_client.reset_caches()
