"""Tests for the MCP setup helpers: transport normalization, connection params,
OAuth token storage, the probe diagnostics, and the interactive OAuth flow state
machine. Network-free — the connect step is monkeypatched."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

from olano.config.agent_config import MCPServerConfig
from olano.core import mcp_setup


@pytest.fixture(autouse=True)
def _tmp_home(monkeypatch, tmp_path):
    """Point LIONCLAW_HOME at a temp dir so token files are isolated, and reset
    the in-process OAuth session registries between tests."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    mcp_setup._OAUTH_SESSIONS.clear()
    mcp_setup._OAUTH_BY_STATE.clear()
    yield


# ── Transport normalization ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        ("http", "streamable_http"),
        ("streamable_http", "streamable_http"),
        ("streamable-http", "streamable_http"),
        ("SSE", "sse"),
        ("stdio", "stdio"),
        ("ws", "websocket"),
        ("auto", "streamable_http"),
    ],
)
def test_normalize_transport(value, expected):
    assert mcp_setup.normalize_transport(value) == expected


def test_normalize_transport_infers_from_command():
    assert mcp_setup.normalize_transport(None, command="uvx") == "stdio"
    assert mcp_setup.normalize_transport(None) == "sse"


@pytest.mark.parametrize(
    "raw,normalized,expected",
    [
        ("auto", "streamable_http", ["streamable_http", "sse"]),
        ("", "sse", ["streamable_http", "sse"]),
        ("sse", "sse", ["sse", "streamable_http"]),
        ("http", "streamable_http", ["streamable_http", "sse"]),
        ("websocket", "websocket", ["websocket"]),
    ],
)
def test_remote_transport_candidates(raw, normalized, expected):
    assert mcp_setup._remote_transport_candidates(raw, normalized) == expected


def test_readable_exc_dcr_unsupported_is_actionable():
    """HubSpot-style DCR rejection → actionable guidance, never the raw HTML."""

    class OAuthRegistrationError(Exception):
        pass

    exc = OAuthRegistrationError(
        "Registration failed: 404 <!DOCTYPE html><html><body><h4>Not found</h4></body></html>"
    )
    msg = mcp_setup._readable_exc(exc)
    assert "does not support automatic client registration" in msg
    assert "<" not in msg  # no HTML leaked


def test_readable_exc_strips_html_and_truncates():
    exc = RuntimeError("<html><head><style>x{color:red}</style></head><body>Boom</body></html>")
    msg = mcp_setup._readable_exc(exc)
    assert "<" not in msg and "Boom" in msg
    assert len(mcp_setup._readable_exc(RuntimeError("z" * 1000))) <= 320


def test_is_remote_transport():
    assert mcp_setup.is_remote_transport("http")
    assert mcp_setup.is_remote_transport("sse")
    assert not mcp_setup.is_remote_transport("stdio")


# ── Connection params (runtime build path) ──────────────────────────────────


def test_build_connection_params_http_maps_to_streamable_http():
    srv = MCPServerConfig(url="https://example.com/mcp", transport="http")
    params = mcp_setup.build_connection_params(srv, "merli")
    assert params["transport"] == "streamable_http"
    assert params["url"] == "https://example.com/mcp"
    assert "auth" not in params


def test_build_connection_params_passes_headers():
    srv = MCPServerConfig(
        url="https://example.com/mcp",
        transport="http",
        headers={"Authorization": "Bearer abc"},
    )
    params = mcp_setup.build_connection_params(srv, "merli")
    assert params["headers"] == {"Authorization": "Bearer abc"}


def test_build_connection_params_oauth_attaches_provider():
    from mcp.client.auth import OAuthClientProvider

    srv = MCPServerConfig(url="https://mcp.stripe.com", transport="http", auth="oauth")
    params = mcp_setup.build_connection_params(srv, "merli")
    # An OAuthClientProvider (our expiry-aware subclass) is an httpx.Auth —
    # attached under "auth".
    assert params.get("auth") is not None
    assert isinstance(params["auth"], OAuthClientProvider)


def test_build_connection_params_stdio_without_command_is_none():
    srv = MCPServerConfig(url="echo", transport="stdio")
    assert mcp_setup.build_connection_params(srv, "merli") is None


def test_build_connection_params_stdio_with_command():
    srv = MCPServerConfig(url="echo", transport="stdio", command="uvx", args=["x"])
    params = mcp_setup.build_connection_params(srv, "merli")
    assert params["transport"] == "stdio"
    assert params["command"] == "uvx"
    assert params["args"] == ["x"]


# ── Builder resilience (boot must not crash on an unauthorized OAuth server) ──


async def test_unauthorized_oauth_server_is_skipped_without_crash(base_config):
    """An OAuth server with no stored tokens must be skipped, not raise — a single
    unauthorized server must never abort the agent boot."""
    from olano.core.builder import _build_mcp_tools

    base_config.mcp_servers = [
        MCPServerConfig(url="https://mcp.stripe.com", transport="http", auth="oauth"),
    ]
    # No tokens stored for this agent → skipped silently, no exception, no tools.
    tools, summary = await _build_mcp_tools(base_config)
    assert tools == []
    # Summary still records the server with an actionable "auth_required" status.
    assert [s["status"] for s in summary] == ["auth_required"]


async def test_failing_remote_server_does_not_crash_boot(base_config, monkeypatch):
    """A remote server that fails to connect is isolated — boot continues."""
    from olano.core import builder

    base_config.mcp_servers = [
        MCPServerConfig(url="https://unreachable.invalid/mcp", transport="http"),
    ]

    class _BoomClient:
        def __init__(self, *_a, **_k):
            pass

        async def get_tools(self):
            raise RuntimeError("connection refused")

    monkeypatch.setattr("langchain_mcp_adapters.client.MultiServerMCPClient", _BoomClient)
    tools, summary = await builder._build_mcp_tools(base_config)
    assert tools == []
    # The failure is isolated and recorded as an error row, not raised.
    assert [s["status"] for s in summary] == ["error"]


# ── Token storage ────────────────────────────────────────────────────────────


async def test_file_token_storage_round_trip():
    from mcp.shared.auth import OAuthToken

    path = mcp_setup.token_store_path("merli", "https://mcp.stripe.com")
    storage = mcp_setup.FileTokenStorage(path)

    assert await storage.get_tokens() is None
    assert not mcp_setup.has_oauth_tokens("merli", "https://mcp.stripe.com")

    await storage.set_tokens(OAuthToken(access_token="tok", token_type="Bearer"))
    assert mcp_setup.has_oauth_tokens("merli", "https://mcp.stripe.com")

    loaded = await storage.get_tokens()
    assert loaded is not None and loaded.access_token == "tok"

    assert mcp_setup.clear_oauth_tokens("merli", "https://mcp.stripe.com")
    assert not mcp_setup.has_oauth_tokens("merli", "https://mcp.stripe.com")


async def test_file_token_storage_static_client_skips_dcr():
    """A configured client_id makes get_client_info return static creds (so the
    SDK skips Dynamic Client Registration) and set_client_info a no-op."""
    path = mcp_setup.token_store_path("merli", "https://mcp.hubspot.com")
    storage = mcp_setup.FileTokenStorage(
        path,
        static_client_id="cid",
        static_client_secret="sec",
        redirect_uris=["https://app/api/mcp/oauth/callback"],
        scope="crm",
    )
    info = await storage.get_client_info()
    assert info.client_id == "cid"
    assert info.client_secret == "sec"
    # Secret present → must POST it on the token exchange.
    assert info.token_endpoint_auth_method == "client_secret_post"

    # Nothing dynamic to persist — the file is never written for static clients.
    await storage.set_client_info(info)
    assert not path.exists()

    # Public (PKCE-only) client → no secret, auth method "none".
    pub = mcp_setup.FileTokenStorage(path, static_client_id="cid2")
    assert (await pub.get_client_info()).token_endpoint_auth_method == "none"


def test_client_metadata_registers_public_pkce_client():
    """DCR must request a public (PKCE-only) client. Omitting
    token_endpoint_auth_method lets strict servers (Notion) default to
    client_secret_basic, which the mcp<=1.26.0 token exchange sends alongside
    client_id in the body → 400 "must not use multiple authentication methods"."""
    meta = mcp_setup._client_metadata("https://auth.olano.ai/mcp/callback", scope=None)
    assert meta.token_endpoint_auth_method == "none"


async def test_get_client_info_drops_stuck_basic_client_for_reregistration():
    """A DCR client stored as client_secret_basic with no tokens (the failed
    Notion state) is dropped so the SDK re-registers as a public client. One that
    already has tokens is preserved."""
    import json

    path = mcp_setup.token_store_path("merli", "https://mcp.notion.com/mcp")
    try:
        # Stuck: registered client_secret_basic, never got tokens → drop it.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "client_info": {
                        "client_id": "oacli_stuck",
                        "client_secret": "sek",
                        "redirect_uris": ["https://auth.olano.ai/mcp/callback"],
                        "token_endpoint_auth_method": "client_secret_basic",
                    }
                }
            )
        )
        assert await mcp_setup.FileTokenStorage(path).get_client_info() is None

        # Same client, but it did obtain tokens once → keep it untouched.
        path.write_text(
            json.dumps(
                {
                    "client_info": {
                        "client_id": "oacli_ok",
                        "client_secret": "sek",
                        "redirect_uris": ["https://auth.olano.ai/mcp/callback"],
                        "token_endpoint_auth_method": "client_secret_basic",
                    },
                    "tokens": {"access_token": "tok", "token_type": "Bearer"},
                }
            )
        )
        info = await mcp_setup.FileTokenStorage(path).get_client_info()
        assert info is not None and info.client_id == "oacli_ok"
    finally:
        path.unlink(missing_ok=True)


def test_clear_stale_oauth_client_on_redirect_change():
    """A cached DCR client registered against a different redirect URI (e.g. the
    dashboard moved ports) is wiped so the flow re-registers; a matching one is
    kept; a static-client file (no client_info) is left untouched."""
    import json

    url = "https://mcp.stripe.com"
    path = mcp_setup.token_store_path("merli", url)
    old = "http://localhost:3201/api/mcp/oauth/callback"
    new = "http://localhost:3200/api/mcp/oauth/callback"

    # Mismatched redirect → cleared, returns the stale URIs.
    path.write_text(
        json.dumps(
            {
                "client_info": {"client_id": "oacli_X", "redirect_uris": [old]},
                "tokens": {"access_token": "old"},
            }
        )
    )
    assert mcp_setup.clear_stale_oauth_client("merli", url, new) == [old]
    assert not path.exists()

    # Matching redirect → kept.
    path.write_text(json.dumps({"client_info": {"client_id": "oacli_Y", "redirect_uris": [new]}}))
    assert mcp_setup.clear_stale_oauth_client("merli", url, new) is None
    assert path.exists()

    # Static client (no client_info) → untouched.
    path.write_text(json.dumps({"tokens": {"access_token": "t"}}))
    assert mcp_setup.clear_stale_oauth_client("merli", url, new) is None
    assert path.exists()

    # Missing file → no-op.
    mcp_setup.clear_oauth_tokens("merli", url)
    assert mcp_setup.clear_stale_oauth_client("merli", url, new) is None


# ── Token-expiry restore (refresh-on-load) ──────────────────────────────────


async def _store_token(agent, url, *, expires_in, age_seconds, refresh_token="rt"):
    """Replace the stored token with one issued *age_seconds* ago.

    Clears any previous file first: ``set_tokens`` deliberately carries a
    previous refresh token forward (a renewal that omits one means "keep it"),
    so writing ``refresh_token=None`` over an existing grant would otherwise
    inherit the old one instead of expressing "no refresh token stored".
    """
    import json

    from mcp.shared.auth import OAuthToken

    mcp_setup.clear_oauth_tokens(agent, url)
    storage = mcp_setup.FileTokenStorage(
        mcp_setup.token_store_path(agent, url),
        static_client_id="cid",
        static_client_secret="sec",
    )
    await storage.set_tokens(
        OAuthToken(
            access_token="stored-access",
            token_type="Bearer",
            expires_in=expires_in,
            refresh_token=refresh_token,
        )
    )
    # Backdate the on-disk write time so the stored token's real expiry is
    # (now - age_seconds) + expires_in.
    path = mcp_setup.token_store_path(agent, url)
    data = json.loads(path.read_text())
    data["updated_at"] = mcp_setup.time.time() - age_seconds
    path.write_text(json.dumps(data))


async def test_oauth_provider_marks_expired_stored_token_invalid(monkeypatch):
    """Regression: a stored access token that has already expired must report
    is_token_valid() == False after the provider initializes, so the MCP SDK
    takes its refresh path instead of a (non-interactive) full re-auth.

    The bare SDK never restores token_expiry_time on load, so it would treat the
    expired token as valid, send it, get a 401, and fail. make_oauth_provider
    must restore the real expiry from the on-disk timestamp.
    """
    agent, url = "merli", "https://mcp.hubspot.com"
    # 30-minute token issued an hour ago → long expired.
    await _store_token(agent, url, expires_in=1800, age_seconds=3600)
    # Keep the unit test network-free: skip live AS-metadata discovery.

    async def _no_metadata(_url):
        return None

    monkeypatch.setattr(mcp_setup, "_discover_oauth_metadata", _no_metadata, raising=False)

    provider = mcp_setup.make_oauth_provider(
        agent, url, client_id="cid", client_secret="sec", interactive=False
    )
    await provider._initialize()
    ctx = provider.context
    assert ctx.current_tokens is not None
    assert ctx.token_expiry_time is not None  # expiry was restored from disk
    assert ctx.is_token_valid() is False  # expired → SDK will refresh
    assert ctx.can_refresh_token() is True  # refresh token + static client present


async def test_oauth_provider_keeps_unexpired_stored_token_valid(monkeypatch):
    """A still-valid stored token must NOT be marked expired (no over-eager
    refresh that would burn the refresh token on every connect)."""
    agent, url = "merli", "https://mcp.hubspot.com"
    # 30-minute token issued seconds ago → still valid.
    await _store_token(agent, url, expires_in=1800, age_seconds=5)

    async def _no_metadata(_url):
        return None

    monkeypatch.setattr(mcp_setup, "_discover_oauth_metadata", _no_metadata, raising=False)

    provider = mcp_setup.make_oauth_provider(
        agent, url, client_id="cid", client_secret="sec", interactive=False
    )
    await provider._initialize()
    assert provider.context.is_token_valid() is True


# ── Expiry-aware token state (dashboard pill / boot skip) ────────────────────


async def test_oauth_token_state_classifies_tokens():
    agent, url = "merli", "https://mcp.example.com"

    # No file at all.
    assert mcp_setup.oauth_token_state(agent, url) == {
        "state": "none",
        "authorized": False,
        "expires_at": None,
        "has_refresh_token": False,
    }

    # Fresh token → live + authorized.
    await _store_token(agent, url, expires_in=3600, age_seconds=5)
    state = mcp_setup.oauth_token_state(agent, url)
    assert state["state"] == "live"
    assert state["authorized"] is True
    assert state["has_refresh_token"] is True
    assert state["expires_at"] is not None

    # Expired access token but refresh token stored → renews on next connect,
    # still authorized.
    await _store_token(agent, url, expires_in=3600, age_seconds=7200)
    state = mcp_setup.oauth_token_state(agent, url)
    assert state["state"] == "refreshable"
    assert state["authorized"] is True

    # Expired with NO refresh token → dead. This is the Zoho access-token-only
    # case that used to render a green "Authorized" pill forever.
    await _store_token(agent, url, expires_in=3600, age_seconds=7200, refresh_token=None)
    state = mcp_setup.oauth_token_state(agent, url)
    assert state["state"] == "expired"
    assert state["authorized"] is False
    assert state["has_refresh_token"] is False


async def test_oauth_token_state_unknown_expiry_counts_live():
    """Providers that omit expires_in must stay authorized — an unknown expiry
    is not evidence the token is dead."""
    agent, url = "merli", "https://mcp.example.com"
    await _store_token(agent, url, expires_in=None, age_seconds=7200, refresh_token=None)
    state = mcp_setup.oauth_token_state(agent, url)
    assert state["state"] == "live"
    assert state["authorized"] is True
    assert state["expires_at"] is None


async def test_expired_dead_oauth_server_is_skipped_at_boot(base_config):
    """An OAuth server whose stored access token expired with no refresh token
    must be skipped cleanly at boot (like a never-authorized one) instead of
    failing mid-handshake with an opaque TaskGroup error."""
    from olano.core.builder import _build_mcp_tools

    url = "https://mcp.stripe.com"
    base_config.mcp_servers = [MCPServerConfig(url=url, transport="http", auth="oauth")]
    await _store_token(base_config.name, url, expires_in=3600, age_seconds=7200, refresh_token=None)

    tools, summary = await _build_mcp_tools(base_config)
    assert tools == []
    assert [s["status"] for s in summary] == ["auth_required"]
    assert summary[0]["message"] == "OAuth token expired"


# ── Offline-access authorize params (Zoho refresh token) ──────────────────────


ZOHO_URL = "https://olano-929869942.zohomcp.com/mcp/ef7cd47702676f2ff229548cb78aebe9/message"
_AUTHORIZE = (
    "https://mcp.zoho.com/authorize?response_type=code&client_id=abc"
    "&redirect_uri=https%3A%2F%2Fauth.olano.ai%2Fmcp%2Fcallback"
    "&state=XYZ&code_challenge=CC&code_challenge_method=S256&scope=ZohoMail.messages.READ"
)


def test_offline_authorize_params_matches_zoho_only():
    assert mcp_setup._offline_authorize_params(ZOHO_URL) == {
        "access_type": "offline",
        "prompt": "consent",
    }
    assert mcp_setup._offline_authorize_params("https://mcp.hubspot.com/mcp") == {}


def test_augment_authorize_url_adds_offline_consent_for_zoho():
    """Zoho only mints a refresh token when the authorize request asks for
    offline access + forced consent; the MCP SDK never sends these."""
    out = mcp_setup._augment_authorize_url(_AUTHORIZE, ZOHO_URL)
    qs = parse_qs(urlparse(out).query)
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]
    # state + PKCE + scope survive untouched.
    assert qs["state"] == ["XYZ"]
    assert qs["code_challenge"] == ["CC"]
    assert qs["scope"] == ["ZohoMail.messages.READ"]


def test_augment_authorize_url_leaves_non_zoho_untouched():
    out = mcp_setup._augment_authorize_url(_AUTHORIZE, "https://mcp.hubspot.com/mcp")
    assert out == _AUTHORIZE


def test_augment_authorize_url_does_not_clobber_explicit_prompt():
    out = mcp_setup._augment_authorize_url(_AUTHORIZE + "&prompt=login", ZOHO_URL)
    qs = parse_qs(urlparse(out).query)
    assert qs["prompt"] == ["login"]
    assert qs["access_type"] == ["offline"]


async def test_make_oauth_provider_injects_zoho_params_into_redirect():
    """The interactive redirect handler must receive the augmented URL so the
    browser sends access_type=offline + prompt=consent to Zoho."""
    captured: list[str] = []

    async def _capture(auth_url: str) -> None:
        captured.append(auth_url)

    async def _cb() -> tuple[str, str | None]:
        return ("code", "XYZ")

    provider = mcp_setup.make_oauth_provider(
        "emma",
        ZOHO_URL,
        interactive=True,
        redirect_handler=_capture,
        callback_handler=_cb,
    )
    await provider.context.redirect_handler(_AUTHORIZE)
    assert captured, "redirect handler was not invoked"
    qs = parse_qs(urlparse(captured[0]).query)
    assert qs["access_type"] == ["offline"]
    assert qs["prompt"] == ["consent"]


# ── Probe diagnostics ────────────────────────────────────────────────────────


async def test_probe_server_connected(monkeypatch):
    class _Tool:
        def __init__(self, name):
            self.name = name
            self.description = f"desc {name}"

    async def _fake_connect(url, transport, **kwargs):
        return [_Tool("alpha"), _Tool("beta")]

    monkeypatch.setattr(mcp_setup, "_connect_and_list_tools", _fake_connect)
    result = await mcp_setup.probe_server(
        "merli", {"url": "https://example.com/mcp", "transport": "http"}
    )
    assert result["ok"] is True
    assert result["status"] == "connected"
    assert result["transport"] == "streamable_http"
    assert {t["name"] for t in result["tools"]} == {"alpha", "beta"}
    assert any("Transport" in entry["message"] for entry in result["logs"])


async def test_probe_with_stored_tokens_passes_static_client_creds(monkeypatch):
    """Regression: with stored tokens, the probe MUST hand the static
    oauth_client_id/secret to the OAuth provider — otherwise a static-client
    server (HubSpot) can't refresh an expired access token and the dashboard
    wrongly shows auth_required on every revisit."""
    captured: dict = {}
    real_make = mcp_setup.make_oauth_provider

    def _capture_make(agent, url, **kwargs):
        captured.update(kwargs)
        return real_make(agent, url, **kwargs)

    async def _fake_connect(url, transport, **kwargs):
        return []

    await _store_token("merli", "https://mcp.hubspot.com", expires_in=3600, age_seconds=0)
    monkeypatch.setattr(mcp_setup, "make_oauth_provider", _capture_make)
    monkeypatch.setattr(mcp_setup, "_connect_and_list_tools", _fake_connect)

    result = await mcp_setup.probe_server(
        "merli",
        {
            "url": "https://mcp.hubspot.com",
            "transport": "streamable_http",
            "auth": "oauth",
            "oauth_client_id": "cid-123",
            "oauth_client_secret": "sec-456",
        },
    )
    assert result["ok"] is True
    assert captured.get("client_id") == "cid-123"
    assert captured.get("client_secret") == "sec-456"


async def test_probe_oauth_without_tokens_is_auth_required_no_probe(monkeypatch):
    """auth=oauth + no stored tokens → auth_required immediately, WITHOUT probing
    (detection is unreliable and a raw connect would fail opaquely)."""

    async def _should_not_run(*_a, **_k):
        raise AssertionError("probe must not run when OAuth is selected and unauthorized")

    monkeypatch.setattr(mcp_setup, "_connect_and_list_tools", _should_not_run)
    monkeypatch.setattr(mcp_setup, "_detect_oauth_required", _should_not_run)
    result = await mcp_setup.probe_server(
        "merli", {"url": "https://mcp.stripe.com", "transport": "http", "auth": "oauth"}
    )
    assert result["ok"] is False
    assert result["status"] == "auth_required"
    assert result["needs_oauth"] is True
    assert result["authorized"] is False


async def test_probe_autodetect_unwraps_exceptiongroup_401(monkeypatch):
    """Auto-detect (auth=none): a 401 buried in an anyio ExceptionGroup must be
    recognized as auth_required, with a readable message (not 'ExceptionGroup …')."""
    import httpx

    request = httpx.Request("POST", "https://mcp.stripe.com")
    response = httpx.Response(401, request=request)
    http_err = httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)
    group = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [http_err])

    async def _raise_group(url, transport, **kwargs):
        raise group

    async def _no_raw_signal(url, transport, headers):
        return False, None  # raw probe misses it; exc-based detection must catch it

    monkeypatch.setattr(mcp_setup, "_connect_and_list_tools", _raise_group)
    monkeypatch.setattr(mcp_setup, "_detect_oauth_required", _no_raw_signal)
    result = await mcp_setup.probe_server(
        "merli", {"url": "https://mcp.stripe.com", "transport": "http"}
    )
    assert result["status"] == "auth_required"
    assert result["needs_oauth"] is True
    # The opaque TaskGroup wrapper must not leak into the user-facing ERROR line
    # (DEBUG lines legitimately carry the raw traceback).
    error_lines = [e["message"] for e in result["logs"] if e["level"] == "error"]
    assert error_lines and all("ExceptionGroup" not in m for m in error_lines)


def test_flatten_and_auth_detection_helpers():
    import httpx

    request = httpx.Request("GET", "https://x/mcp")
    err401 = httpx.HTTPStatusError(
        "Client error '401 Unauthorized' for url 'https://x/mcp'",
        request=request,
        response=httpx.Response(401, request=request),
    )
    group = ExceptionGroup("grp", [ExceptionGroup("inner", [err401])])
    leaves = mcp_setup._flatten_exc(group)
    assert leaves == [err401]
    assert mcp_setup._exc_indicates_auth(group) is True
    assert mcp_setup._exc_indicates_auth(RuntimeError("connection refused")) is False
    readable = mcp_setup._readable_exc(group)
    assert "401" in readable
    assert "ExceptionGroup" not in readable


async def test_probe_server_error_includes_logs(monkeypatch):
    async def _boom(url, transport, **kwargs):
        raise RuntimeError("connection refused")

    async def _no_oauth(url, transport, headers):
        return False, None

    monkeypatch.setattr(mcp_setup, "_connect_and_list_tools", _boom)
    monkeypatch.setattr(mcp_setup, "_detect_oauth_required", _no_oauth)
    result = await mcp_setup.probe_server(
        "merli", {"url": "https://example.com/mcp", "transport": "http"}
    )
    assert result["ok"] is False
    assert result["status"] == "error"
    assert "connection refused" in result["message"]
    assert result["logs"]  # traceback + error captured


async def test_probe_timeout_env_override(monkeypatch):
    """LIONCLAW_MCP_PROBE_TIMEOUT overrides the 20s default (slow hosts — e.g. a
    cloud VM whose first npx run must download the package)."""
    monkeypatch.setenv("LIONCLAW_MCP_PROBE_TIMEOUT", "0.2")

    async def _hang(url, transport, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(mcp_setup, "_connect_and_list_tools", _hang)
    result = await mcp_setup.probe_server(
        "merli", {"url": "https://example.com/mcp", "transport": "http"}
    )
    assert result["ok"] is False
    assert "0.2s" in result["message"]


def test_probe_timeout_resolution(monkeypatch):
    monkeypatch.delenv("LIONCLAW_MCP_PROBE_TIMEOUT", raising=False)
    assert mcp_setup._probe_timeout() == mcp_setup.PROBE_TIMEOUT
    assert mcp_setup._probe_timeout(5.0) == 5.0
    monkeypatch.setenv("LIONCLAW_MCP_PROBE_TIMEOUT", "45")
    assert mcp_setup._probe_timeout() == 45.0
    assert mcp_setup._probe_timeout(5.0) == 5.0  # explicit arg wins
    monkeypatch.setenv("LIONCLAW_MCP_PROBE_TIMEOUT", "not-a-number")
    assert mcp_setup._probe_timeout() == mcp_setup.PROBE_TIMEOUT
    monkeypatch.setenv("LIONCLAW_MCP_PROBE_TIMEOUT", "-3")
    assert mcp_setup._probe_timeout() == mcp_setup.PROBE_TIMEOUT


async def test_probe_stdio_timeout_echoes_child_stderr():
    """A stdio child that hangs after printing to stderr → the probe result must
    carry that stderr in its logs (it's where npx/mcp-remote print the real
    error) plus the handshake-timeout diagnosis."""
    import sys

    entry = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [
            "-c",
            "import sys,time; print('ERR_FROM_CHILD', file=sys.stderr, flush=True); time.sleep(30)",
        ],
    }
    result = await mcp_setup.probe_server("merli", entry, timeout=2.0)
    assert result["ok"] is False
    assert "timed out" in result["message"]
    messages = [e["message"] for e in result["logs"]]
    assert any("ERR_FROM_CHILD" in m for m in messages)
    assert any("never completed the MCP handshake" in m for m in messages)


async def test_probe_stdio_timeout_mcp_remote_hint(monkeypatch):
    """Timing out on an `npx mcp-remote <url>` bridge must point at the native
    remote transport (with the URL) instead of leaving an opaque timeout."""

    async def _hang(url, transport, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(mcp_setup, "_connect_and_list_tools", _hang)
    entry = {
        "transport": "stdio",
        "command": "npx",
        "args": [
            "mcp-remote",
            "https://acme.zohomcp.com/mcp/key/message",
            "--transport",
            "http-only",
        ],
    }
    result = await mcp_setup.probe_server("merli", entry, timeout=0.2)
    assert result["status"] == "error"
    joined = " ".join(e["message"] for e in result["logs"])
    assert "streamable HTTP natively" in joined
    assert "https://acme.zohomcp.com/mcp/key/message" in joined


def test_stdio_remote_wrapper_hint_matching():
    hint = mcp_setup._stdio_remote_wrapper_hint("npx", ["-y", "mcp-remote@0.1.0", "https://x/mcp"])
    assert hint and "https://x/mcp" in hint
    assert mcp_setup._stdio_remote_wrapper_hint("uvx", ["mcp-server-time"]) is None


# ── Interactive OAuth flow state machine ─────────────────────────────────────


async def test_oauth_flow_completes_on_callback(monkeypatch):
    """start_oauth_flow → the connect step blocks on the callback handler; feeding
    the redirect via resolve_oauth_callback completes the flow as authorized."""

    class _Tool:
        name = "charges.list"

    captured = {}

    # Connect step that exercises the redirect + callback handlers the way a live
    # OAuthClientProvider handshake would.
    async def _drive(url, transport, *, auth=None, headers=None, log=None):
        # The real OAuthClientProvider would call these; here we simulate it.
        await captured["redirect"]("https://auth.example.com/authorize?state=xyz123")
        code, state = await captured["callback"]()
        assert code == "the-code" and state == "xyz123"
        return [_Tool()]

    # Intercept make_oauth_provider to capture the handlers passed in.
    real_make = mcp_setup.make_oauth_provider

    def _capture_make(
        agent,
        url,
        *,
        redirect_uri=None,
        scope=None,
        client_id=None,
        client_secret=None,
        redirect_handler=None,
        callback_handler=None,
        interactive=False,
    ):
        captured["redirect"] = redirect_handler
        captured["callback"] = callback_handler
        captured["client_id"] = client_id
        captured["client_secret"] = client_secret
        return object()  # provider is unused by our fake connect

    monkeypatch.setattr(mcp_setup, "make_oauth_provider", _capture_make)
    monkeypatch.setattr(mcp_setup, "_connect_and_list_tools", _drive)

    session = mcp_setup.start_oauth_flow(
        "merli",
        {"url": "https://mcp.stripe.com", "transport": "http"},
        redirect_uri="http://localhost:3200/api/mcp/oauth/callback",
    )

    # Wait until the flow has surfaced the authorization URL + state.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if session.state:
            break
    assert session.status == "awaiting_authorization"
    assert session.state == "xyz123"
    assert mcp_setup.poll_oauth_flow(session.session_id) is session

    # Feed the redirect callback → flow completes.
    resolved = mcp_setup.resolve_oauth_callback("xyz123", "the-code")
    assert resolved is session

    await asyncio.wait_for(session._task, timeout=2)
    assert session.status == "authorized"
    assert session.tool_count == 1


async def test_probe_uses_stored_tokens_even_when_auth_is_none(monkeypatch):
    """Regression: tokens stored for (agent, url) must be attached to the probe
    even when the entry still says auth:none — the freshly-authorized Add-modal
    re-probe would otherwise re-hit the 401 the Authorize flow just fixed, and
    the modal never advanced past 'Connect & Authorize'."""
    sentinel = object()
    seen = {}

    def _fake_provider(agent, url, **kwargs):
        return sentinel

    async def _connect(url, transport, *, auth=None, **kwargs):
        seen["auth"] = auth
        return []

    await _store_token(
        "merli", "https://acme.zohomcp.com/mcp/key/message", expires_in=3600, age_seconds=0
    )
    monkeypatch.setattr(mcp_setup, "make_oauth_provider", _fake_provider)
    monkeypatch.setattr(mcp_setup, "_connect_and_list_tools", _connect)

    result = await mcp_setup.probe_server(
        "merli", {"url": "https://acme.zohomcp.com/mcp/key/message", "transport": "http"}
    )
    assert result["ok"] is True
    assert result["authorized"] is True
    assert result["needs_oauth"] is True
    assert seen["auth"] is sentinel


def test_build_connection_params_attaches_stored_tokens_for_auth_none(monkeypatch):
    """Boot/reload must also use stored tokens for an auth:none entry, so the
    agent binds the just-authorized server's tools instead of getting a 401."""
    monkeypatch.setattr(mcp_setup, "has_oauth_tokens", lambda agent, url: True)
    srv = MCPServerConfig(url="https://acme.zohomcp.com/mcp/key/message", transport="http")
    params = mcp_setup.build_connection_params(srv, "merli")
    assert params.get("auth") is not None


def test_persist_oauth_auth_mode_updates_config():
    """The persist helper flips a stored auth:none server to auth:oauth (and is
    an idempotent no-op on re-run / unknown url / unknown agent)."""
    import json

    from olano.config.home import get_olano_paths

    url = "https://acme.zohomcp.com/mcp/key/message"
    agent_dir = get_olano_paths().agents_dir / "merli"
    agent_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = agent_dir / "agent.json5"
    cfg_path.write_text(
        json.dumps({"name": "merli", "mcp_servers": [{"url": url, "transport": "http"}]})
    )

    assert mcp_setup._persist_oauth_auth_mode("merli", url, client_id="cid-1") is True
    entry = json.loads(cfg_path.read_text())["mcp_servers"][0]
    assert entry["auth"] == "oauth"
    assert entry["oauth_client_id"] == "cid-1"

    assert mcp_setup._persist_oauth_auth_mode("merli", url, client_id="cid-1") is False
    assert mcp_setup._persist_oauth_auth_mode("merli", "https://other/mcp") is False
    assert mcp_setup._persist_oauth_auth_mode("ghost-agent", url) is False


async def test_oauth_flow_persists_auth_mode_to_config(monkeypatch):
    """Regression: a server stored with auth:none that completes the Authorize
    flow must be flipped to auth:oauth in the agent config. Otherwise every
    later path (boot, hot reload, probe, cheap status) reads `none`, connects
    unauthenticated, and reports the server toolless/unauthorized even though
    valid tokens sit on disk — the dashboard showed '0/0 tools' right after
    'Authorized · 182 tools'."""
    import json

    from olano.config.home import get_olano_paths

    url = "https://acme.zohomcp.com/mcp/key/message"
    agent_dir = get_olano_paths().agents_dir / "zoho-agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = agent_dir / "agent.json5"
    cfg_path.write_text(
        json.dumps({"name": "zoho-agent", "mcp_servers": [{"url": url, "transport": "http"}]})
    )

    class _Tool:
        name = "crm.search"

    captured = {}

    async def _drive(url_, transport, *, auth=None, headers=None, log=None):
        await captured["redirect"]("https://auth.example.com/authorize?state=st1")
        await captured["callback"]()
        return [_Tool()]

    def _capture_make(agent, url_, **kwargs):
        captured["redirect"] = kwargs.get("redirect_handler")
        captured["callback"] = kwargs.get("callback_handler")
        return object()

    monkeypatch.setattr(mcp_setup, "make_oauth_provider", _capture_make)
    monkeypatch.setattr(mcp_setup, "_connect_and_list_tools", _drive)

    session = mcp_setup.start_oauth_flow(
        "zoho-agent",
        {"url": url, "transport": "http", "oauth_client_id": "cid-1"},
        redirect_uri="http://localhost:3200/api/mcp/oauth/callback",
    )
    for _ in range(50):
        await asyncio.sleep(0.01)
        if session.state:
            break
    assert mcp_setup.resolve_oauth_callback("st1", "the-code") is session
    await asyncio.wait_for(session._task, timeout=2)
    assert session.status == "authorized"

    entry = json.loads(cfg_path.read_text())["mcp_servers"][0]
    assert entry["auth"] == "oauth"
    assert entry["oauth_client_id"] == "cid-1"
    assert any("auth: oauth" in log["message"] for log in session.logs)


async def test_resolve_callback_unknown_state_returns_none():
    assert mcp_setup.resolve_oauth_callback("nope", "code") is None


async def test_start_oauth_flow_rejects_stdio():
    with pytest.raises(ValueError):
        mcp_setup.start_oauth_flow(
            "merli", {"url": "echo", "transport": "stdio"}, redirect_uri="http://x/cb"
        )


async def test_probe_result_includes_error_code_key():
    """probe_server always returns an error_code key (None unless DCR-unsupported)."""
    result = await mcp_setup.probe_server(
        "merli",
        {"url": "", "transport": "stdio"},  # stdio without command → fast error path
        timeout=1.0,
    )
    assert "error_code" in result
    assert result["error_code"] is None


def test_oauth_session_reports_dcr_unsupported_error_code():
    """A DCR-rejection failure is exposed as a structured error_code so the UI
    can prompt for a pre-registered client instead of string-matching."""
    session = mcp_setup.OAuthFlowSession(
        session_id="s1", agent="merli", url="https://mcp.hubspot.com", transport="streamable_http"
    )
    assert session.error_code is None
    assert session.public()["error_code"] is None

    class OAuthRegistrationError(Exception):
        pass

    session.error_code = (
        "dcr_unsupported"
        if mcp_setup._exc_indicates_dcr_unsupported(
            OAuthRegistrationError("Registration failed: 404 <html>...</html>")
        )
        else None
    )
    assert session.public()["error_code"] == "dcr_unsupported"


async def test_file_token_storage_is_owner_only_from_the_first_byte():
    import os

    from mcp.shared.auth import OAuthToken

    if os.name != "posix":
        return
    path = mcp_setup.token_store_path("merli", "https://mcp.stripe.com")
    storage = mcp_setup.FileTokenStorage(path)
    await storage.set_tokens(OAuthToken(access_token="tok", token_type="Bearer"))
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert not [p for p in path.parent.iterdir() if p.suffix == ".tmp"]
