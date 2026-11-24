"""Coverage for the ``olano mcp`` CLI group and its OAuth driver.

Two layers:

1. The click commands (``olano mcp add|list|remove``) round-tripping MCP
   server config on a per-agent basis via :mod:`olano.admin`.
2. The headless OAuth driver :func:`olano.core.mcp_setup.authorize_via_local_callback`
   — a real localhost redirect capture wired to a faked provider handshake.
"""

from __future__ import annotations

import asyncio

import click
import pytest
from click.testing import CliRunner

from olano import admin
from olano.cli.main import cli
from olano.cli.mcp_cmd import parse_kv_pairs
from olano.config import mcp_secrets
from olano.config.channel_secrets import _resolve_vault

# ── CLI: add / list / remove round-trip ──────────────────────────────────────


def test_mcp_add_list_remove_cli() -> None:
    admin.create_agent("climcp", description="cli mcp test")
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["mcp", "add", "https://mcp.example.com/sse", "--agent", "climcp", "--transport", "sse"],
    )
    assert result.exit_code == 0, result.output
    assert "Added" in result.output
    servers = admin.list_mcp_servers("climcp")
    assert any(s["url"] == "https://mcp.example.com/sse" for s in servers)

    listed = runner.invoke(cli, ["mcp", "list", "--agent", "climcp"])
    assert listed.exit_code == 0, listed.output
    assert "mcp.example.com" in listed.output

    removed = runner.invoke(
        cli, ["mcp", "remove", "https://mcp.example.com/sse", "--agent", "climcp"]
    )
    assert removed.exit_code == 0, removed.output
    assert admin.list_mcp_servers("climcp") == []


def test_mcp_add_scopes_to_specific_agent() -> None:
    admin.create_agent("agent_a", description="a")
    admin.create_agent("agent_b", description="b")
    runner = CliRunner()

    result = runner.invoke(
        cli, ["mcp", "add", "https://only-a.example.com/sse", "--agent", "agent_a"]
    )
    assert result.exit_code == 0, result.output
    assert any(
        s["url"] == "https://only-a.example.com/sse" for s in admin.list_mcp_servers("agent_a")
    )
    # The other agent is untouched — servers are per-agent, not global.
    assert admin.list_mcp_servers("agent_b") == []


def test_mcp_add_replace_reports_updated() -> None:
    admin.create_agent("climcp_upd")
    runner = CliRunner()
    url = "https://mcp.example.com/sse"
    runner.invoke(cli, ["mcp", "add", url, "--agent", "climcp_upd"])
    again = runner.invoke(cli, ["mcp", "add", url, "--agent", "climcp_upd", "--transport", "http"])
    assert again.exit_code == 0, again.output
    assert "Updated" in again.output
    servers = admin.list_mcp_servers("climcp_upd")
    assert len(servers) == 1
    assert servers[0]["transport"] == "http"


def test_mcp_add_oauth_flags_stored() -> None:
    url = "https://mcp.example.com/mcp"
    admin.create_agent("clioauth")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "mcp",
            "add",
            url,
            "--agent",
            "clioauth",
            "--transport",
            "http",
            "--auth",
            "oauth",
            "--oauth-scope",
            "read:all",
            "--oauth-client-id",
            "cid",
            "--oauth-client-secret",
            "sekret",
        ],
    )
    assert result.exit_code == 0, result.output
    srv = admin.list_mcp_servers("clioauth")[0]
    assert srv["auth"] == "oauth"
    assert srv["oauth_scope"] == "read:all"
    assert srv["oauth_client_id"] == "cid"
    assert srv["transport"] == "http"

    # The client secret is the one field the writer does not store verbatim: it
    # moves into the vault as a per-agent override, leaving a ${vault:…}
    # reference behind — and a vault that is locked or absent keeps the literal
    # and says so. BOTH are by design (tests/test_mcp_secrets.py owns that
    # contract), so which one a run produces depends on the machine it runs on:
    # asserting either spelling makes this test pass on a box without a usable
    # vault and fail on one with it. What is invariant, and what every reader
    # actually does, is resolving the stored value back to the secret.
    stored = srv["oauth_client_secret"]
    if stored == "sekret":
        return  # no vault here — the literal stayed, as reported
    assert stored == mcp_secrets.reference_for(url)
    vault = _resolve_vault()
    assert vault is not None, "a ${vault:…} reference means the vault took the write"
    assert vault.get(mcp_secrets.client_secret_key(url), agent="clioauth") == "sekret"


def test_mcp_add_stdio_requires_command() -> None:
    admin.create_agent("clistdio")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["mcp", "add", "echo", "--agent", "clistdio", "--transport", "stdio"]
    )
    assert result.exit_code != 0
    assert "requires --command" in result.output


def test_mcp_add_stdio_with_args_and_env() -> None:
    admin.create_agent("clistdio2")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "mcp",
            "add",
            "fs",
            "--agent",
            "clistdio2",
            "--transport",
            "stdio",
            "--command",
            "npx",
            "--arg",
            "-y",
            "--arg",
            "server-filesystem",
            "--env",
            "TOKEN=abc",
        ],
    )
    assert result.exit_code == 0, result.output
    srv = admin.list_mcp_servers("clistdio2")[0]
    assert srv["command"] == "npx"
    assert srv["args"] == ["-y", "server-filesystem"]
    assert srv["env"] == {"TOKEN": "abc"}


def test_mcp_add_disabled_flag() -> None:
    admin.create_agent("clidisabled")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["mcp", "add", "https://mcp.example.com/sse", "--agent", "clidisabled", "--disabled"]
    )
    assert result.exit_code == 0, result.output
    assert admin.list_mcp_servers("clidisabled")[0]["enabled"] is False


def test_mcp_add_unknown_agent_errors() -> None:
    runner = CliRunner()
    result = runner.invoke(
        cli, ["mcp", "add", "https://x.example.com", "--agent", "does-not-exist"]
    )
    assert result.exit_code != 0
    assert "not found" in result.output


def test_mcp_authorize_stdio_is_rejected() -> None:
    admin.create_agent("cliauthstdio")
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "mcp",
            "add",
            "echo",
            "--agent",
            "cliauthstdio",
            "--transport",
            "stdio",
            "--command",
            "echo",
            "--authorize",
        ],
    )
    assert result.exit_code != 0
    assert "only supported for http/sse" in result.output


def test_parse_kv_pairs_roundtrip_and_error() -> None:
    assert parse_kv_pairs(("A=1", "B=x=y"), label="--env") == {"A": "1", "B": "x=y"}
    with pytest.raises(click.BadParameter):
        parse_kv_pairs(("noequals",), label="--env")


# ── Headless OAuth driver (localhost redirect capture) ───────────────────────


async def test_authorize_via_local_callback_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from olano.core import mcp_setup

    class _Tool:
        name = "widget.list"

    captured: dict[str, object] = {}

    async def _drive(url, transport, *, auth=None, headers=None, log=None):
        # Mimic the OAuthClientProvider handshake: publish an authorization URL
        # (carrying the state) then block on the callback future.
        await captured["redirect"]("https://auth.example.com/authorize?state=st-42")  # type: ignore[operator]
        code, state = await captured["callback"]()  # type: ignore[operator]
        assert code == "code-99" and state == "st-42"
        return [_Tool()]

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
        captured["redirect_uri"] = redirect_uri
        return object()

    monkeypatch.setattr(mcp_setup, "make_oauth_provider", _capture_make)
    monkeypatch.setattr(mcp_setup, "_connect_and_list_tools", _drive)

    announced: dict[str, str] = {}

    task = asyncio.create_task(
        mcp_setup.authorize_via_local_callback(
            "merli",
            {"url": "https://mcp.example.com/mcp", "transport": "http"},
            open_browser=False,
            on_authorization_url=lambda u: announced.__setitem__("url", u),
        )
    )

    # Wait for the flow to surface the authorization URL and the bound redirect URI.
    for _ in range(300):
        await asyncio.sleep(0.01)
        if "url" in announced and captured.get("redirect_uri"):
            break
    assert announced["url"].startswith("https://auth.example.com")
    redirect_uri = str(captured["redirect_uri"])
    assert redirect_uri.startswith("http://127.0.0.1:")

    # Hit the localhost callback the way a real browser redirect would.
    async with httpx.AsyncClient() as client:
        resp = await client.get(redirect_uri, params={"code": "code-99", "state": "st-42"})
    assert resp.status_code == 200
    assert "Authorization received" in resp.text

    session = await asyncio.wait_for(task, timeout=5)
    assert session.status == "authorized"
    assert session.tool_count == 1


async def test_authorize_via_local_callback_rejects_stdio() -> None:
    from olano.core import mcp_setup

    with pytest.raises(ValueError):
        await mcp_setup.authorize_via_local_callback(
            "merli", {"url": "echo", "transport": "stdio"}, open_browser=False
        )
