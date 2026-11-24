"""Tests for MCP connection health: token renewal that survives a refresh, the
proactive maintenance helpers, and the recovery planner.

These cover the failure the health layer exists to prevent — a connection that
works right up until its first successful token refresh and is then dead until
someone re-authorizes it by hand. Network-free: the token endpoint is faked.
"""

from __future__ import annotations

import json

import pytest

from olano.core import mcp_health, mcp_setup


@pytest.fixture(autouse=True)
def _tmp_home(monkeypatch, tmp_path):
    """Isolate token files and the module-level recovery backoff per test."""
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    mcp_health.reset_recovery()
    yield
    mcp_health.reset_recovery()


AGENT = "emma"
URL = "https://mcp.zoho.com/mcp"


async def _store(
    *,
    access_token: str = "at-1",
    refresh_token: str | None = "rt-1",
    expires_in: int | None = 3600,
    age_seconds: float = 0.0,
    client_info: dict | None = None,
    agent: str = AGENT,
    url: str = URL,
) -> object:
    """Write a stored grant whose access token was issued *age_seconds* ago.

    Writes the file directly rather than through :class:`FileTokenStorage` — the
    storage layer deliberately carries a previous refresh token forward, which
    would make "stored without a refresh token" impossible to set up here.
    """
    path = mcp_setup.token_store_path(agent, url)
    tokens: dict = {"access_token": access_token, "token_type": "Bearer"}
    if expires_in is not None:
        tokens["expires_in"] = expires_in
    if refresh_token is not None:
        tokens["refresh_token"] = refresh_token
    data: dict = {"tokens": tokens, "updated_at": mcp_setup.time.time() - age_seconds}
    if client_info is not None:
        data["client_info"] = client_info
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def _stored(agent: str = AGENT, url: str = URL) -> dict:
    return json.loads(mcp_setup.token_store_path(agent, url).read_text())


# ── The killer: a renewal must not disarm the next one ──────────────────────


async def test_renewal_without_refresh_token_keeps_the_stored_one():
    """Regression: Zoho (and every provider following RFC 6749 §5.1) omits
    ``refresh_token`` on refresh, meaning "keep the previous one". The MCP SDK
    hands that response to storage verbatim, so a blind overwrite dropped the
    refresh token on the *first* successful renewal — the connection then died
    for good an hour later with nothing to renew from."""
    from mcp.shared.auth import OAuthToken

    await _store(access_token="at-1", refresh_token="rt-1", expires_in=3600)
    storage = mcp_setup.FileTokenStorage(mcp_setup.token_store_path(AGENT, URL))

    # A Zoho-shaped renewal: new access token, no refresh token, no scope.
    await storage.set_tokens(OAuthToken(access_token="at-2", token_type="Bearer", expires_in=3600))

    tokens = _stored()["tokens"]
    assert tokens["access_token"] == "at-2"
    assert tokens["refresh_token"] == "rt-1"  # carried forward
    state = mcp_setup.oauth_token_state(AGENT, URL)
    assert state["state"] == "live"
    assert state["has_refresh_token"] is True


async def test_renewal_with_a_rotated_refresh_token_replaces_it():
    """A provider that *does* rotate must not be pinned to the old token."""
    from mcp.shared.auth import OAuthToken

    await _store(refresh_token="rt-1")
    storage = mcp_setup.FileTokenStorage(mcp_setup.token_store_path(AGENT, URL))
    await storage.set_tokens(OAuthToken(access_token="at-2", expires_in=3600, refresh_token="rt-2"))
    assert _stored()["tokens"]["refresh_token"] == "rt-2"


async def test_expired_token_with_carried_refresh_stays_refreshable():
    """The state the boot path reads: expired access token + surviving refresh
    token is `refreshable` (bootable), not `expired` (skipped)."""
    await _store(expires_in=3600, age_seconds=7200, refresh_token="rt-1")
    assert mcp_setup.oauth_token_state(AGENT, URL)["state"] == "refreshable"
    await _store(expires_in=3600, age_seconds=7200, refresh_token=None)
    assert mcp_setup.oauth_token_state(AGENT, URL)["state"] == "expired"


# ── token_needs_refresh ─────────────────────────────────────────────────────


async def test_token_needs_refresh_window():
    # Fresh token, far from expiry → nothing to do.
    await _store(expires_in=3600, age_seconds=0)
    assert mcp_setup.token_needs_refresh(AGENT, URL, window=900) is False
    # Inside the window → renew now.
    await _store(expires_in=3600, age_seconds=3000)
    assert mcp_setup.token_needs_refresh(AGENT, URL, window=900) is True
    # Already expired but renewable → renew now.
    await _store(expires_in=3600, age_seconds=7200)
    assert mcp_setup.token_needs_refresh(AGENT, URL, window=900) is True
    # Nothing to renew from → never.
    await _store(expires_in=3600, age_seconds=7200, refresh_token=None)
    assert mcp_setup.token_needs_refresh(AGENT, URL, window=900) is False
    # Unknown expiry is not an expiry to schedule against.
    await _store(expires_in=None, age_seconds=7200)
    assert mcp_setup.token_needs_refresh(AGENT, URL, window=900) is False


async def test_token_needs_refresh_without_stored_tokens():
    assert mcp_setup.token_needs_refresh(AGENT, "https://nothing.example/mcp") is False


# ── refresh_oauth_token ─────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)
        self.content = self.text.encode()

    async def aread(self) -> bytes:
        return self.content


class _FakeClient:
    """Minimal stand-in for httpx.AsyncClient used as an async context manager."""

    def __init__(self, response: _FakeResponse, calls: list[dict]):
        self._response = response
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, headers=None):
        self._calls.append({"url": url, "data": dict(data or {}), "headers": headers})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _fake_token_endpoint(monkeypatch, response, calls: list[dict]) -> None:
    import httpx

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: _FakeClient(response, calls), raising=True
    )

    async def _no_metadata(_url):
        return None

    monkeypatch.setattr(mcp_setup, "_discover_oauth_metadata", _no_metadata, raising=False)


async def test_refresh_oauth_token_renews_and_preserves_refresh_token(monkeypatch):
    await _store(
        access_token="at-1",
        refresh_token="rt-1",
        expires_in=3600,
        age_seconds=3500,
        client_info={"client_id": "cid", "client_secret": "sec"},
    )
    calls: list[dict] = []
    _fake_token_endpoint(
        monkeypatch,
        _FakeResponse(200, {"access_token": "at-2", "token_type": "Bearer", "expires_in": 3600}),
        calls,
    )

    result = await mcp_setup.refresh_oauth_token(AGENT, URL)

    assert result["ok"] is True and result["status"] == "refreshed"
    assert calls[0]["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "rt-1",
        "client_id": "cid",
        "client_secret": "sec",
    }
    tokens = _stored()["tokens"]
    assert tokens["access_token"] == "at-2"
    assert tokens["refresh_token"] == "rt-1"
    assert mcp_setup.oauth_token_state(AGENT, URL)["state"] == "live"


async def test_refresh_oauth_token_public_client_sends_no_secret(monkeypatch):
    await _store(client_info={"client_id": "cid"})
    calls: list[dict] = []
    _fake_token_endpoint(monkeypatch, _FakeResponse(200, {"access_token": "at-2"}), calls)
    await mcp_setup.refresh_oauth_token(AGENT, URL)
    assert "client_secret" not in calls[0]["data"]


async def test_refresh_oauth_token_rejection_leaves_the_grant_alone(monkeypatch):
    """A rejected renewal must not delete anything: the dashboard keeps showing
    what is stored and the user decides whether to re-authorize."""
    await _store(access_token="at-1", refresh_token="rt-1", client_info={"client_id": "cid"})
    _fake_token_endpoint(monkeypatch, _FakeResponse(400, {"error": "invalid_grant"}), [])

    result = await mcp_setup.refresh_oauth_token(AGENT, URL)

    assert result["ok"] is False and result["status"] == "rejected"
    assert "invalid_grant" in result["message"]
    assert _stored()["tokens"]["access_token"] == "at-1"
    assert _stored()["tokens"]["refresh_token"] == "rt-1"


async def test_refresh_oauth_token_unreachable_endpoint_is_transient(monkeypatch):
    await _store(client_info={"client_id": "cid"})
    _fake_token_endpoint(monkeypatch, ConnectionError("dns"), [])
    result = await mcp_setup.refresh_oauth_token(AGENT, URL)
    assert result["ok"] is False and result["status"] == "unreachable"
    assert _stored()["tokens"]["access_token"] == "at-1"


async def test_refresh_oauth_token_without_refresh_token(monkeypatch):
    await _store(refresh_token=None, client_info={"client_id": "cid"})
    result = await mcp_setup.refresh_oauth_token(AGENT, URL)
    assert result["status"] == "no_refresh_token"


async def test_refresh_oauth_token_without_a_client(monkeypatch):
    await _store()
    result = await mcp_setup.refresh_oauth_token(AGENT, URL)
    assert result["status"] == "no_client"


async def test_refresh_oauth_token_caches_the_discovered_endpoint(monkeypatch):
    """Discovery is a network round trip; a periodic renewal must not repeat it."""
    await _store(client_info={"client_id": "cid"})
    calls: list[dict] = []
    _fake_token_endpoint(monkeypatch, _FakeResponse(200, {"access_token": "at-2"}), calls)

    discoveries = {"n": 0}

    async def _metadata(_url):
        discoveries["n"] += 1

        class _Meta:
            token_endpoint = "https://accounts.zoho.com/oauth/v2/token"

        return _Meta()

    monkeypatch.setattr(mcp_setup, "_discover_oauth_metadata", _metadata, raising=False)

    await mcp_setup.refresh_oauth_token(AGENT, URL)
    assert _stored()["token_endpoint"] == "https://accounts.zoho.com/oauth/v2/token"
    await mcp_setup.refresh_oauth_token(AGENT, URL)
    assert discoveries["n"] == 1
    assert calls[1]["url"] == "https://accounts.zoho.com/oauth/v2/token"


# ── The provider's own refresh handling ─────────────────────────────────────


def _provider(monkeypatch, agent: str = AGENT, url: str = URL):
    async def _no_metadata(_url):
        return None

    monkeypatch.setattr(mcp_setup, "_discover_oauth_metadata", _no_metadata, raising=False)
    return mcp_setup.make_oauth_provider(
        agent, url, client_id="cid", client_secret="sec", interactive=False
    )


async def test_provider_carries_refresh_token_forward_in_memory(monkeypatch):
    """The on-disk copy is repaired by the storage layer, but nothing re-reads
    it while the process lives — so the in-memory context must be repaired too,
    or this provider can never renew again."""
    await _store(refresh_token="rt-1")
    provider = _provider(monkeypatch)
    await provider._initialize()

    ok = await provider._handle_refresh_response(
        _FakeResponse(200, {"access_token": "at-2", "token_type": "Bearer", "expires_in": 3600})
    )

    assert ok is True
    assert provider.context.current_tokens.refresh_token == "rt-1"
    assert provider.context.can_refresh_token() is True


async def test_provider_keeps_tokens_on_a_transient_refresh_failure(monkeypatch):
    """A 502 from the token endpoint is not a revoked grant. Clearing on it (the
    SDK's behaviour) forces an interactive re-auth a runtime agent cannot do."""
    await _store(refresh_token="rt-1")
    provider = _provider(monkeypatch)
    await provider._initialize()

    ok = await provider._handle_refresh_response(_FakeResponse(502, text="bad gateway"))

    assert ok is False
    assert provider.context.current_tokens is not None
    assert provider._initialized is True  # caller flips this, re-reading storage


async def test_provider_keeps_tokens_when_refresh_rotated_underneath(monkeypatch):
    """Lost race with another refresher: disk already holds a newer token, so
    the rejection is ours to retry, not the user's to re-authorize."""
    await _store(refresh_token="rt-1")
    provider = _provider(monkeypatch)
    await provider._initialize()
    await _store(access_token="at-9", refresh_token="rt-2")  # someone else rotated

    ok = await provider._handle_refresh_response(_FakeResponse(400, {"error": "invalid_grant"}))

    assert ok is False
    assert provider.context.current_tokens is not None


async def test_provider_clears_tokens_on_a_genuine_rejection(monkeypatch):
    """Nothing changed underneath and the grant was refused → really dead."""
    await _store(refresh_token="rt-1")
    provider = _provider(monkeypatch)
    await provider._initialize()

    ok = await provider._handle_refresh_response(_FakeResponse(400, {"error": "invalid_grant"}))

    assert ok is False
    assert provider.context.current_tokens is None


async def test_provider_keeps_tokens_when_the_url_has_no_token_endpoint(monkeypatch):
    """A 404 means the POST never reached a token endpoint — not that the grant
    was refused. RFC 6749 §5.2 spells a refusal ``400`` with an ``error`` body,
    so treating "not found" as one destroys a refresh token nobody rejected.

    This is the Stripe failure: metadata discovery came back empty, the SDK fell
    back to guessing ``<origin>/token``, and that guess 404s on every provider
    that doesn't serve one there. The grant was fine; the URL was wrong.
    """
    await _store(refresh_token="rt-1")
    provider = _provider(monkeypatch)
    await provider._initialize()

    ok = await provider._handle_refresh_response(_FakeResponse(404, text="Not Found"))

    assert ok is False
    assert provider.context.current_tokens is not None
    assert provider.context.current_tokens.refresh_token == "rt-1"
    assert _stored()["tokens"]["refresh_token"] == "rt-1"


async def test_provider_rediscovers_the_endpoint_it_mis_sent_to(monkeypatch):
    """Keeping the tokens is only half the recovery: the cached endpoint that
    404'd must go too, or every later renewal re-sends to the same wrong URL and
    the grant stays dead for a reason nothing on the box can see."""
    await _store(refresh_token="rt-1")
    path = mcp_setup.token_store_path(AGENT, URL)
    data = json.loads(path.read_text())
    data["token_endpoint"] = "https://mcp.example.com/token"
    path.write_text(json.dumps(data))

    provider = _provider(monkeypatch)
    await provider._initialize()
    provider.context.oauth_metadata = object()  # a stale endpoint, however cached

    await provider._handle_refresh_response(_FakeResponse(404, text="Not Found"))

    assert "token_endpoint" not in _stored()
    assert provider.context.oauth_metadata is None


async def test_provider_still_clears_on_a_refused_grant_from_a_real_endpoint(monkeypatch):
    """The 404 carve-out must not swallow a genuine revocation: a 401 from a
    token endpoint is that endpoint refusing this client, and a grant that is
    really dead has to fail fast rather than retry forever."""
    await _store(refresh_token="rt-1")
    provider = _provider(monkeypatch)
    await provider._initialize()

    ok = await provider._handle_refresh_response(_FakeResponse(401, {"error": "invalid_client"}))

    assert ok is False
    assert provider.context.current_tokens is None


async def test_provider_reloads_tokens_that_changed_on_disk(monkeypatch):
    """The health loop renews on a timer; a long-lived provider must notice."""
    await _store(access_token="at-1", refresh_token="rt-1", expires_in=3600, age_seconds=7200)
    provider = _provider(monkeypatch)
    await provider._initialize()
    assert provider.context.is_token_valid() is False  # expired copy in memory

    await _store(access_token="at-2", refresh_token="rt-1", expires_in=3600)
    request = type("_Req", (), {"headers": {}})()
    flow = provider.async_auth_flow(request)
    await flow.__anext__()  # drives the reload before the first outgoing request
    await flow.aclose()

    assert provider.context.current_tokens.access_token == "at-2"
    assert provider.context.is_token_valid() is True


async def test_provider_does_not_reload_an_unchanged_dead_token(monkeypatch):
    """Without the mtime guard a revoked grant would be re-read and re-refreshed
    on every single request."""
    await _store(access_token="at-1", refresh_token="rt-1", expires_in=3600, age_seconds=7200)
    provider = _provider(monkeypatch)
    await provider._initialize()
    provider.context.current_tokens = None  # as a genuine rejection leaves it

    request = type("_Req", (), {"headers": {}})()
    flow = provider.async_auth_flow(request)
    try:
        await flow.__anext__()
    finally:
        await flow.aclose()

    # Never re-read from disk: the file is unchanged, so the dead token stays
    # dead and the SDK fails fast instead of re-refreshing on every request.
    assert provider.context.current_tokens is None
    assert provider._initialized is True


# ── The health sweep ────────────────────────────────────────────────────────


def _entry(url: str = URL, **over) -> dict:
    entry = {"url": url, "transport": "http", "enabled": True, "auth": "oauth"}
    entry.update(over)
    return entry


def _summary(url: str = URL, *, ok: bool, status: str, message: str = "") -> list[dict]:
    return [{"url": url, "ok": ok, "status": status, "tools": [], "message": message}]


async def test_check_agent_renews_a_token_close_to_expiry(monkeypatch):
    await _store(expires_in=3600, age_seconds=3400, client_info={"client_id": "cid"})
    _fake_token_endpoint(monkeypatch, _FakeResponse(200, {"access_token": "at-2"}), [])

    report = await mcp_health.check_agent(
        AGENT, [_entry()], _summary(ok=True, status="connected"), refresh_window=900
    )

    server = report.servers[0]
    assert server.action == "refreshed"
    assert server.connected is True
    assert report.needs_reload is False
    assert _stored()["tokens"]["access_token"] == "at-2"


async def test_check_agent_leaves_a_healthy_token_alone(monkeypatch):
    await _store(expires_in=3600, age_seconds=10, client_info={"client_id": "cid"})
    calls: list[dict] = []
    _fake_token_endpoint(monkeypatch, _FakeResponse(200, {"access_token": "at-2"}), calls)

    report = await mcp_health.check_agent(
        AGENT, [_entry()], _summary(ok=True, status="connected"), refresh_window=900
    )

    assert report.servers[0].action == "none"
    assert calls == []


async def test_check_agent_records_a_failed_renewal(monkeypatch):
    await _store(expires_in=3600, age_seconds=3400, client_info={"client_id": "cid"})
    _fake_token_endpoint(monkeypatch, _FakeResponse(400, {"error": "invalid_grant"}), [])

    report = await mcp_health.check_agent(
        AGENT, [_entry()], _summary(ok=True, status="connected"), refresh_window=900
    )

    assert report.servers[0].action == "refresh_failed"
    assert "invalid_grant" in report.servers[0].detail


async def test_check_agent_plans_recovery_for_a_timed_out_server():
    report = await mcp_health.check_agent(
        AGENT, [_entry()], _summary(ok=False, status="timeout"), refresh_tokens=False
    )
    assert report.reload_urls == [URL]
    assert report.servers[0].action == "reload_requested"


async def test_check_agent_backs_off_between_recovery_attempts():
    args = (AGENT, [_entry()], _summary(ok=False, status="error"))
    first = await mcp_health.check_agent(*args, refresh_tokens=False, now=1000.0)
    assert first.reload_urls == [URL]

    # Same tick + one minute: still inside the first backoff window.
    soon = await mcp_health.check_agent(*args, refresh_tokens=False, now=1060.0)
    assert soon.reload_urls == []
    assert soon.servers[0].action == "backoff"

    # Past the base delay → retried, and the window doubles from there.
    later = await mcp_health.check_agent(
        *args, refresh_tokens=False, now=1000.0 + mcp_health.RECOVERY_BASE_DELAY + 1
    )
    assert later.reload_urls == [URL]
    mid = await mcp_health.check_agent(
        *args, refresh_tokens=False, now=1000.0 + mcp_health.RECOVERY_BASE_DELAY * 2.5
    )
    assert mid.reload_urls == []


async def test_recovery_backoff_resets_once_the_server_is_back():
    args = (AGENT, [_entry()], _summary(ok=False, status="timeout"))
    await mcp_health.check_agent(*args, refresh_tokens=False, now=1000.0)
    # It connected on the next build → forget the attempt history.
    await mcp_health.check_agent(
        AGENT, [_entry()], _summary(ok=True, status="connected"), refresh_tokens=False, now=1100.0
    )
    # A later outage is retried immediately rather than inheriting the backoff.
    again = await mcp_health.check_agent(*args, refresh_tokens=False, now=1200.0)
    assert again.reload_urls == [URL]


async def test_check_agent_does_not_retry_a_server_that_needs_authorization():
    """Reconnecting cannot invent consent — retrying would rebuild the graph on
    a timer for nothing."""
    report = await mcp_health.check_agent(
        AGENT,
        [_entry()],
        _summary(ok=False, status="auth_required"),
        refresh_tokens=False,
    )
    assert report.reload_urls == []
    assert report.servers[0].action == "needs_authorization"


async def test_check_agent_retries_auth_required_once_tokens_exist(monkeypatch):
    """The other half: tokens arrived out of band (dashboard/CLI authorize), so
    the stale `auth_required` verdict is worth re-testing."""
    await _store(expires_in=3600, age_seconds=10, client_info={"client_id": "cid"})
    report = await mcp_health.check_agent(
        AGENT, [_entry()], _summary(ok=False, status="auth_required"), refresh_tokens=False
    )
    assert report.reload_urls == [URL]


async def test_check_agent_skips_disabled_and_misconfigured_servers():
    report = await mcp_health.check_agent(
        AGENT,
        [_entry(enabled=False), _entry(url="stdio://x", transport="stdio")],
        [
            {"url": URL, "ok": False, "status": "disabled", "tools": [], "message": ""},
            {"url": "stdio://x", "ok": False, "status": "misconfigured", "tools": []},
        ],
        refresh_tokens=False,
    )
    assert report.reload_urls == []


async def test_maintain_survives_a_bad_agent(monkeypatch):
    """One agent's failure must not take down the fleet sweep."""
    calls: list[str] = []
    real = mcp_health.check_agent

    async def _flaky(agent, *args, **kwargs):
        calls.append(agent)
        if agent == "broken":
            raise RuntimeError("boom")
        return await real(agent, *args, **kwargs)

    monkeypatch.setattr(mcp_health, "check_agent", _flaky)
    reports = await mcp_health.maintain(
        {"broken": [_entry()], AGENT: [_entry()]},
        {AGENT: _summary(ok=True, status="connected")},
        refresh_tokens=False,
    )
    assert set(calls) == {"broken", AGENT}
    assert set(reports) == {AGENT}


def test_agent_report_serializes_counts():
    report = mcp_health.AgentReport(agent=AGENT, checked_at=1.0)
    report.servers = [
        mcp_health.ServerReport(
            url=URL,
            transport="streamable_http",
            enabled=True,
            auth="oauth",
            connected=True,
            status="connected",
            message="3 tools",
        ),
        mcp_health.ServerReport(
            url="https://b",
            transport="sse",
            enabled=True,
            auth="none",
            connected=False,
            status="timeout",
            message="Connection timed out",
        ),
    ]
    data = report.to_dict()
    assert data["connected"] == 1 and data["degraded"] == 1
    assert data["servers"][0]["status"] == "connected"


# ── Settings ────────────────────────────────────────────────────────────────


def test_mcp_health_settings_defaults_and_clamps(tmp_path, monkeypatch):
    from olano.config.system_settings import load_mcp_health_settings

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    defaults = load_mcp_health_settings()
    assert defaults.enabled is True
    assert defaults.refresh_tokens is True
    assert defaults.auto_reconnect is True
    # The window must stay above the tick or a token is first noticed late.
    assert defaults.refresh_window_minutes > defaults.check_interval_minutes

    (tmp_path / "config.yaml").write_text(
        "mcp_health:\n"
        "  enabled: false\n"
        "  check_interval_minutes: 0\n"
        "  refresh_window_minutes: 99999\n"
    )
    loaded = load_mcp_health_settings()
    assert loaded.enabled is False
    assert loaded.check_interval_minutes == 1  # clamped up
    assert loaded.refresh_window_minutes == 12 * 60  # clamped down


def test_mcp_health_settings_survive_a_broken_config(tmp_path, monkeypatch):
    from olano.config.system_settings import load_mcp_health_settings

    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("mcp_health: [not, a, mapping]\n")
    assert load_mcp_health_settings().enabled is True


# ── API surface (dashboard Config card + diagnostics) ───────────────────────


@pytest.fixture
def api_client(tmp_workspace, monkeypatch):
    """TestClient over a mock runtime carrying an MCP health snapshot."""
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from olano.admin.memory import MemoryMonitor
    from olano.runtime import disk_monitor as dm
    from olano.runtime.api import create_api

    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    rt = MagicMock()
    rt._runners = {}
    rt._channels = []
    rt._memory_monitor = MemoryMonitor()
    rt._disk_monitor = dm.DiskMonitor()
    rt.mcp_health_snapshot = MagicMock(
        return_value={
            "checked_at": 1000.0,
            "agents": {
                AGENT: {
                    "agent": AGENT,
                    "checked_at": 1000.0,
                    "servers": [
                        {
                            "url": URL,
                            "enabled": True,
                            "connected": True,
                            "status": "connected",
                            "token_state": "live",
                            "action": "refreshed",
                        }
                    ],
                    "reload_urls": [],
                    "connected": 1,
                    "degraded": 0,
                }
            },
        }
    )
    return TestClient(create_api(rt)), rt


def test_system_config_includes_mcp_health_block(api_client):
    client, _rt = api_client
    data = client.get("/api/system/config").json()
    assert data["mcp_health"]["enabled"] is True
    assert data["mcp_health"]["check_interval_minutes"] == 10
    assert data["mcp_health"]["refresh_window_minutes"] == 20


def test_system_config_put_persists_mcp_health(api_client):
    client, _rt = api_client
    resp = client.put(
        "/api/system/config",
        json={
            "mcp_health": {
                "enabled": False,
                "check_interval_minutes": 30,
                "refresh_window_minutes": 45,
                "auto_reconnect": False,
            }
        },
    )
    assert resp.status_code == 200
    block = resp.json()["mcp_health"]
    assert block["enabled"] is False
    assert block["check_interval_minutes"] == 30
    assert block["refresh_window_minutes"] == 45
    assert block["auto_reconnect"] is False
    assert block["refresh_tokens"] is True  # untouched key survives the merge


def test_system_config_put_validates_mcp_health(api_client):
    """The PUT rejects loudly where the YAML loader clamps silently — a
    dashboard save must never persist something other than what was typed."""
    client, _rt = api_client
    assert (
        client.put(
            "/api/system/config", json={"mcp_health": {"check_interval_minutes": 0}}
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/api/system/config", json={"mcp_health": {"refresh_window_minutes": 99999}}
        ).status_code
        == 400
    )
    # A non-empty non-object is malformed. (An empty/falsy value means "no
    # changes to this block" — same convention as disk_monitor.)
    assert client.put("/api/system/config", json={"mcp_health": ["nope"]}).status_code == 400
    # A window shorter than the tick would let a token be first noticed late.
    assert (
        client.put(
            "/api/system/config",
            json={"mcp_health": {"check_interval_minutes": 60, "refresh_window_minutes": 5}},
        ).status_code
        == 400
    )


def test_mcp_diagnostics_endpoint(api_client):
    client, _rt = api_client
    data = client.get("/api/diagnostics/mcp").json()
    assert data["ok"] is True
    assert data["checked_at"] == 1000.0
    assert data["settings"]["enabled"] is True
    assert data["agents"][AGENT]["servers"][0]["action"] == "refreshed"


def test_mcp_diagnostics_endpoint_filters_by_agent(api_client):
    client, _rt = api_client
    assert list(client.get(f"/api/diagnostics/mcp?agent={AGENT}").json()["agents"]) == [AGENT]
    assert client.get("/api/diagnostics/mcp?agent=ghost").json()["agents"] == {}


async def test_a_disabled_entry_reports_disabled_over_a_stale_live_row() -> None:
    """A disable that did not rebuild leaves the last build's `auth_required`
    row behind; the config is newer, and it is what the bell must follow."""
    report = await mcp_health.check_agent(
        "maria",
        [{"url": URL, "transport": "http", "enabled": False, "auth": "oauth"}],
        [{"url": URL, "ok": False, "status": "auth_required", "message": "expired"}],
        refresh_tokens=False,
    )

    (server,) = report.servers
    assert server.status == "disabled"
    assert server.enabled is False
