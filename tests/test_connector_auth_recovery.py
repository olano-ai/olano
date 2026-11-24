"""The generic refusal repair every tool passes through (``tools/_connector_auth.py``).

A tool that returns or raises "401" for a service it is signed in to has told
us the saved connection is dead while the dashboard still says *Connected*.
The central error boundary hands that outcome to the repair layer, which:

* recognises the refusal for the service behind the tool (a connector
  toolkit via the name index and the credentials it reads, the Google
  toolkit, an OAuth MCP server via the builder's tool tags);
* attempts the service's own recovery (a forced connector refresh, a Google
  token refresh, an MCP token refresh) and retries the call once;
* otherwise marks the connection (Connections card / MCP tab) and returns the
  ``[connector-auth]`` result that tells the model to offer the reconnect
  routes this box has, plus the alternatives.

These drive real ``guard_tool``-wrapped tools against throwaway records in
the autouse tmp home, with the broker's token-endpoint seam stubbed.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from langchain_core.tools import tool

from olano.config.home import get_olano_paths
from olano.core.connectors import base, registry
from olano.core.connectors.base import connector_auth_status
from olano.tools import _connector_auth as ca
from olano.tools._error_boundary import TOOL_ERROR_MARKER, guard_tool


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    from olano.vault import resolver

    saved_providers = dict(registry._PROVIDERS)
    saved_exposed = dict(registry._EXPOSED)
    saved_names = registry._EXPOSED_NAMES
    resolver.set_vault(None)
    base._RECORD_CACHE.clear()
    for name in ("GITHUB_TOKEN", "GITHUB_PERSONAL_ACCESS_TOKEN", "GITHUB_PAT"):
        monkeypatch.delenv(name, raising=False)
    yield
    base._RECORD_CACHE.clear()
    registry._PROVIDERS.clear()
    registry._PROVIDERS.update(saved_providers)
    registry._EXPOSED.clear()
    registry._EXPOSED.update(saved_exposed)
    registry._EXPOSED_NAMES = saved_names
    resolver.set_vault(None)


def _github():
    provider = registry.get_provider("github")
    assert provider is not None
    return provider


def _connect_github(access_token: str = "old", *, refresh_token: str | None = "rt") -> None:
    record: dict[str, Any] = {
        "service": "github",
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_at": time.time() + 3600,
        "scopes": ["repo"],
        "client_id": "cid",
        "client_secret": "cs",
        "params": {},
        "extras": {},
        "obtained_at": time.time(),
    }
    if refresh_token:
        record["refresh_token"] = refresh_token
    base._save_record(record, get_olano_paths().connector_token_path("github"))


def _stub_refresh(monkeypatch, payload: dict[str, Any] | None):
    calls: list[dict[str, Any]] = []

    def fake_post(url, data, *, headers=None, auth=None):
        calls.append({"url": url, "data": dict(data)})
        if payload is None:
            raise RuntimeError('Token endpoint returned 400: {"error":"invalid_grant"}')
        return dict(payload)

    monkeypatch.setattr(base, "_post_token", fake_post)
    return calls


class _GitHub:
    """A GitHub toolkit stand-in: refuses every token but the ones in ``valid``.

    Named like the real tool so the name index maps it to the github toolkit,
    and shaped like the real toolkit: it catches its own errors and returns a
    ``[tool-error]`` string, exactly what the repair layer has to see through.
    """

    def __init__(self, valid: set[str]):
        self.valid = valid
        self.tokens: list[str] = []

    def tool(self):
        holder = self

        @tool
        def github_list_repos(query: str = "") -> str:
            """List repositories."""
            from olano.tools._credentials import get_credential

            token = get_credential("GITHUB_TOKEN")
            holder.tokens.append(token)
            if token not in holder.valid:
                return '[tool-error] github_list_repos: 401 {"message": "Bad credentials"}'
            return "octo/repo\nocto/other"

        return github_list_repos


# ── connector toolkits ─────────────────────────────────────────────────────


def test_a_refused_token_is_refreshed_and_the_call_retried_once(monkeypatch):
    _connect_github("old")
    refreshes = _stub_refresh(monkeypatch, {"access_token": "new", "expires_in": 1800})
    gh = _GitHub(valid={"new"})

    out = guard_tool(gh.tool()).invoke({"query": ""})

    assert out == "octo/repo\nocto/other"
    assert gh.tokens == ["old", "new"]
    assert len(refreshes) == 1
    assert connector_auth_status(_github())["connected"] is True


def test_a_refusal_the_refresh_cannot_cure_marks_the_connection_and_lists_every_route(monkeypatch):
    _connect_github("old")
    _stub_refresh(monkeypatch, {"access_token": "still-dead"})
    gh = _GitHub(valid=set())

    out = guard_tool(gh.tool()).invoke({"query": ""})

    assert out.startswith(ca.CONNECTOR_AUTH_MARKER)
    assert "GitHub refused the saved sign-in" in out
    assert "Bad credentials" in out
    # Every route this box has, in order: the link, the device code GitHub
    # declares, a pasted token under the name the toolkit reads, the dashboard.
    assert 'magic_link_connect_service("github")' in out
    assert 'magic_link_connect_service("github", method="device")' in out
    assert 'magic_link_request_secret("GITHUB_TOKEN")' in out
    assert "Disconnect, then Connect" in out
    # And the alternatives when they cannot reconnect now.
    assert 'self_find_integration("GitHub")' in out
    assert "Do not stop at reporting the error" in out
    assert gh.tokens == ["old", "still-dead"]  # one retry, never a loop
    status = connector_auth_status(_github())
    assert status["connected"] is False and status["needs_reauth"] is True
    assert "github_list_repos" in status["auth_error"]


def test_the_exception_path_takes_the_same_repair(monkeypatch):
    _connect_github("old")
    _stub_refresh(monkeypatch, {"access_token": "new", "expires_in": 1800})
    seen: list[str] = []

    @tool
    def github_create_issue(title: str) -> str:
        """Create an issue; raises on a refused token like an SDK would."""
        from olano.tools._credentials import get_credential

        token = get_credential("GITHUB_TOKEN")
        seen.append(token)
        if token != "new":
            raise RuntimeError("401 {'message': 'Bad credentials'}")
        return "created #1"

    assert guard_tool(github_create_issue).invoke({"title": "t"}) == "created #1"
    assert seen == ["old", "new"]


def test_a_hand_set_token_gets_the_vault_story(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_pasted")
    gh = _GitHub(valid=set())

    out = guard_tool(gh.tool()).invoke({"query": ""})

    assert out.startswith(ca.CONNECTOR_AUTH_MARKER)
    assert "hand-set token" in out
    assert 'magic_link_connect_service("github")' in out
    assert 'magic_link_request_secret("GITHUB_TOKEN")' in out
    assert "Needs reconnecting" not in out
    assert gh.tokens == ["ghp_pasted"]  # nothing to refresh, no retry


def test_no_connection_at_all_is_a_connect_story_not_a_reconnect_story(monkeypatch):
    gh = _GitHub(valid=set())
    out = guard_tool(gh.tool()).invoke({"query": ""})
    assert out.startswith(ca.CONNECTOR_AUTH_MARKER)
    assert "No GitHub connection is stored" in out
    assert 'magic_link_connect_service("github")' in out
    assert "Needs reconnecting" not in out


@pytest.mark.parametrize(
    "result",
    [
        "octo/repo\nocto/other",
        "[tool-error] github_list_repos: 403 Forbidden: resource not accessible by integration",
        "[tool-error] github_list_repos: 404 Not Found",
        "[tool-error] github_list_repos: rate limit exceeded (429)",
        "[missing-credentials] GITHUB_TOKEN or GITHUB_PAT not set in vault",
        "[connector-auth] already handled by the toolkit itself",
        # A real payload that happens to mention the number is not an error.
        "Issue #401: Unauthorized users can see the admin page\n" + "x" * 800,
    ],
)
def test_anything_but_a_refusal_passes_through_untouched(monkeypatch, result):
    _connect_github("old")

    def no_refresh(*a, **k):  # pragma: no cover - the assertion is that it never runs
        raise AssertionError("no refresh for a non-refusal")

    monkeypatch.setattr(base, "_post_token", no_refresh)

    @tool
    def github_list_repos(query: str = "") -> str:
        """Returns whatever the case says."""
        return result

    assert guard_tool(github_list_repos).invoke({"query": ""}) == result


def test_a_tool_with_no_connected_service_keeps_the_ordinary_boundary(monkeypatch):
    @tool
    def local_thing(x: str) -> str:
        """Not a connector toolkit."""
        raise RuntimeError("401 Unauthorized")

    out = guard_tool(local_thing).invoke({"x": "a"})
    assert out.startswith(TOOL_ERROR_MARKER)
    assert "auth/permission problem" in out


def test_a_retry_that_fails_some_other_way_is_reported_as_that_failure(monkeypatch):
    _connect_github("old")
    _stub_refresh(monkeypatch, {"access_token": "new", "expires_in": 1800})
    calls = {"n": 0}

    @tool
    def github_list_repos(query: str = "") -> str:
        """First a 401, then a server error on the retry."""
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("401 Bad credentials")
        raise RuntimeError("502 Bad Gateway")

    out = guard_tool(github_list_repos).invoke({"query": ""})
    assert out.startswith(TOOL_ERROR_MARKER)
    assert "502" in out
    assert calls["n"] == 2


# ── Google ──────────────────────────────────────────────────────────────────


def _google_token_file():
    from olano.tools import google_tools

    path = google_tools._resolve_token_path(None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "token": "dead",
                "refresh_token": "rt",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_id": "c",
                "client_secret": "s",
                "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                "expiry": "2999-01-01T00:00:00Z",
            }
        )
    )
    return path


def test_google_refusal_marks_the_card_and_names_the_google_link(monkeypatch):
    from olano.tools import google_tools

    _google_token_file()
    assert google_tools.google_auth_status()["connected"] is True
    monkeypatch.setattr(
        google_tools,
        "refresh_credentials_now",
        lambda: (False, "token refresh failed: invalid_grant"),
    )

    @tool
    def gmail_search(query: str) -> str:
        """Like the real one: catches and returns an error string."""
        return 'Error fetching Gmail messages: <HttpError 401 when requesting ... returned "Invalid Credentials">'

    out = guard_tool(gmail_search).invoke({"query": "from:me"})

    assert out.startswith(ca.CONNECTOR_AUTH_MARKER)
    assert "Google refused the saved sign-in" in out
    assert 'magic_link_connect_service("google")' in out
    assert "invalid_grant" in out
    status = google_tools.google_auth_status()
    assert status["connected"] is False and status["needs_reauth"] is True
    assert "gmail_search" in status["auth_error"]


def test_google_refresh_that_works_retries_the_call(monkeypatch):
    from olano.tools import google_tools

    _google_token_file()
    monkeypatch.setattr(google_tools, "refresh_credentials_now", lambda: (True, ""))
    calls = {"n": 0}

    @tool
    def gmail_search(query: str) -> str:
        """First refused, then fine."""
        calls["n"] += 1
        return (
            "Error fetching Gmail messages: 401 Invalid Credentials"
            if calls["n"] == 1
            else "3 messages"
        )

    assert guard_tool(gmail_search).invoke({"query": "x"}) == "3 messages"
    assert calls["n"] == 2


# ── OAuth MCP servers ───────────────────────────────────────────────────────

MCP_URL = "https://mcp.example.com/mcp"


def _mcp_tool(outcomes: list[Any]):
    """An async, server-tagged tool (as the builder tags them) that pops one outcome per call."""
    from olano.core.mcp_setup import McpAuthorizationRequired

    calls: list[int] = []

    @tool
    async def crm_lookup(name: str) -> str:
        """An MCP tool."""
        calls.append(1)
        outcome = outcomes.pop(0)
        if outcome == "refuse":
            raise McpAuthorizationRequired(MCP_URL)
        return outcome

    crm_lookup.metadata = {"trusted": False, "mcp_server_url": MCP_URL, "mcp_auth": "oauth"}
    return crm_lookup, calls


async def test_mcp_refusal_that_a_refresh_cures_retries_the_call(monkeypatch):
    from olano.core import mcp_setup

    async def fake_refresh(agent, url, **kw):
        return {"ok": True, "status": "refreshed", "message": "Access token renewed."}

    monkeypatch.setattr(mcp_setup, "refresh_oauth_token", fake_refresh)
    t, calls = _mcp_tool(["refuse", "Acme Corp"])

    assert await guard_tool(t).ainvoke({"name": "acme"}) == "Acme Corp"
    assert len(calls) == 2


async def test_mcp_refusal_marks_the_server_and_names_the_sign_in_routes(monkeypatch):
    from olano.core import builder, mcp_setup
    from olano.tools.talk_to import current_agent

    async def fake_refresh(agent, url, **kw):
        return {
            "ok": False,
            "status": "rejected",
            "message": "Token endpoint returned 400: invalid_grant.",
        }

    monkeypatch.setattr(mcp_setup, "refresh_oauth_token", fake_refresh)
    builder._MCP_SUMMARY["iris"] = [
        {
            "url": MCP_URL,
            "transport": "http",
            "ok": True,
            "status": "connected",
            "tools": ["crm_lookup"],
            "message": "1 tool",
        }
    ]
    t, calls = _mcp_tool(["refuse"])
    token = current_agent.set("iris")
    try:
        out = await guard_tool(t).ainvoke({"name": "acme"})
        # The summary row is honest now, so the MCP tab and the health sweep see it.
        (row,) = builder.get_mcp_summary("iris")
        assert row["status"] == "auth_required" and row["ok"] is False
        assert "crm_lookup" in row["message"]
    finally:
        current_agent.reset(token)
        builder._MCP_SUMMARY.pop("iris", None)

    assert out.startswith(ca.CONNECTOR_AUTH_MARKER)
    assert "mcp.example.com" in out and "refused the saved authorization" in out
    assert f'magic_link_connect_service("{MCP_URL}")' in out
    assert f'self_authorize_mcp_server("{MCP_URL}")' in out
    assert "MCP tab" in out and "Authorize" in out
    assert "invalid_grant" in out
    assert len(calls) == 1


async def test_mark_mcp_auth_required_flips_the_summary_row():
    from olano.core import builder

    builder._MCP_SUMMARY["iris2"] = [
        {
            "url": MCP_URL,
            "transport": "http",
            "ok": True,
            "status": "connected",
            "tools": ["x"],
            "message": "1 tool",
        },
        {
            "url": "https://other/mcp",
            "transport": "http",
            "ok": True,
            "status": "connected",
            "tools": [],
            "message": "",
        },
    ]
    try:
        assert builder.mark_mcp_auth_required("iris2", MCP_URL, "crm_lookup: 401") is True
        rows = {r["url"]: r for r in builder.get_mcp_summary("iris2")}
        assert rows[MCP_URL]["status"] == "auth_required" and rows[MCP_URL]["ok"] is False
        assert "crm_lookup" in rows[MCP_URL]["message"]
        assert rows["https://other/mcp"]["status"] == "connected"
        assert builder.mark_mcp_auth_required("iris2", "https://nope/mcp", "x") is False
    finally:
        builder._MCP_SUMMARY.pop("iris2", None)


async def test_a_header_authenticated_mcp_server_is_not_a_reconnect_story():
    t, _ = _mcp_tool(["refuse"])
    t.metadata = {"trusted": False, "mcp_server_url": MCP_URL, "mcp_auth": "none"}
    out = await guard_tool(t).ainvoke({"name": "acme"})
    assert out.startswith(TOOL_ERROR_MARKER)


# ── the pieces ──────────────────────────────────────────────────────────────


def test_connected_service_resolution():
    svc = ca.connected_service_for_tool("github_list_repos", None)
    assert svc is not None and (svc.kind, svc.id, svc.name) == ("connector", "github", "GitHub")
    assert "GITHUB_TOKEN" in svc.credentials
    # Slack and Notion have no entry in the finder's toolkit map; the
    # credentials their toolkits read are what a registered provider exposes.
    svc = ca.connected_service_for_tool("slack_send_message", None)
    assert svc is not None and svc.id == "slack"
    svc = ca.connected_service_for_tool("notion_search", None)
    assert svc is not None and svc.id == "notion"
    svc = ca.connected_service_for_tool("gmail_search", None)
    assert svc is not None and svc.kind == "google"
    assert ca.connected_service_for_tool("youtube_search", None) is None
    assert ca.connected_service_for_tool("web_search", None) is None
    svc = ca.connected_service_for_tool(
        "anything", {"mcp_server_url": MCP_URL, "mcp_auth": "oauth"}
    )
    assert svc is not None and (svc.kind, svc.id, svc.name) == ("mcp", MCP_URL, "mcp.example.com")


@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        ("401 Unauthorized", True),
        ("Bad credentials", True),
        ("InvalidAuthenticationToken: Access token has expired", True),
        ("[tool-error] slack_send_message: invalid_auth", True),
        ("OAuthException (#190) Error validating access token: Session has expired", True),
        ("403 Forbidden", False),
        ("429 Too Many Requests", False),
        ("[missing-credentials] GITHUB_TOKEN not set in vault", False),
        ("Google credentials not found. Run: olano auth google", False),
        ("", False),
    ],
)
def test_refusal_markers(text, refusal):
    assert ca.is_auth_refusal(text) is refusal


def test_refusal_text_reads_content_blocks_and_ignores_payloads():
    assert ca.refusal_text([{"type": "text", "text": "Error: 401 Unauthorized"}]) is not None
    assert ca.refusal_text(("Error: token_expired", None)) is not None
    assert ca.refusal_text({"data": "401"}) is None
    assert ca.refusal_text("[connector-auth] handled") is None
    assert ca.refusal_text("x" * 5000 + " 401") is None
