"""Tests for the handshake agent tools and the ``/olano connect`` command.

The property that matters most here is that identity is a *runtime fact*: who
asked and which chat to answer in come from the bound request context, never
from a tool argument, so a prompt-injected "send this link somewhere else" has
nothing to grab. The rest is routing (a service word → the right flow) and the
owner gate.
"""

from __future__ import annotations

import json

import pytest

from olano.magic_links import service, store
from olano.security.request_context import push_request_context
from olano.tools import magic_link_tools


@pytest.fixture(autouse=True)
def public_origin(monkeypatch):
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://acme.olano.run")
    import olano.core.mcp_setup as mcp_setup

    monkeypatch.setattr(mcp_setup, "_LAST_SEEN_ORIGIN", None)


@pytest.fixture(autouse=True)
def bound_agent(monkeypatch):
    """Bind an executing agent, as the runner does for every real turn."""
    from olano.tools.talk_to import current_agent

    token = current_agent.set("merli")
    yield
    current_agent.reset(token)


@pytest.fixture
def owner_chat():
    """A trusted owner messaging from a Telegram DM."""
    ctx = dict(
        source="channel",
        channel="telegram",
        user_id="12345",
        session_id="12345",
        authenticated=True,
        is_owner=True,
        is_group=False,
        metadata={"chat_id": "12345", "username": "jrivera"},
    )
    with push_request_context(**ctx):
        yield


def _call(tool, **kwargs) -> dict:
    return json.loads(tool.invoke(kwargs))


# ── routing ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("service_name", "expected_flow"),
    [
        # No OAuth client on this box and GitHub declares a device recipe →
        # the sign-in code wins over an OAuth redirect nobody configured.
        ("github", "connector-device"),
        ("gh", "connector-device"),
        ("google", "google"),
        ("codex", "codex-device"),
        # No device recipe → OAuth anyway, so the broker names what's missing.
        ("microsoft365", "connector-oauth"),
        ("https://mcp.example.com/mcp", "mcp-oauth"),
    ],
)
def test_connect_service_routes_to_the_right_flow(owner_chat, service_name, expected_flow):
    out = _call(
        magic_link_tools.magic_link_connect_service, service_name=service_name, agent="merli"
    )
    assert "error" not in out, out
    record = service.resolve_token(out["url"].rsplit("/", 1)[-1])
    assert record.flow == expected_flow


def test_connect_service_honours_an_explicit_method(owner_chat, monkeypatch):
    """`method` overrides the ladder: a pasted key mints secret-entry under the
    provider's exposed name; an unavailable method is refused with the fix."""
    out = _call(
        magic_link_tools.magic_link_connect_service,
        service_name="github",
        agent="merli",
        method="key",
    )
    assert "error" not in out, out
    record = service.resolve_token(out["url"].rsplit("/", 1)[-1])
    assert record.flow == "secret-entry"
    assert record.params["name"] == "GITHUB_TOKEN"

    refused = _call(
        magic_link_tools.magic_link_connect_service,
        service_name="microsoft365",
        agent="merli",
        method="device",
    )
    assert "error" in refused
    assert "sign-in code" in refused["error"]


def test_connect_service_prefers_a_configured_oauth_client(owner_chat, monkeypatch):
    """With the operator's own client in place, GitHub mints the OAuth flow —
    BYO always beats the public device client."""
    import olano.core.connectors.base as base

    monkeypatch.setattr(base, "byo_client_available", lambda provider: True)
    out = _call(magic_link_tools.magic_link_connect_service, service_name="github", agent="merli")
    assert "error" not in out, out
    record = service.resolve_token(out["url"].rsplit("/", 1)[-1])
    assert record.flow == "connector-oauth"


def test_connect_service_needs_something_to_connect(owner_chat):
    assert "error" in _call(magic_link_tools.magic_link_connect_service, service_name="  ")


def test_request_secret_routes_by_name_or_provider(owner_chat):
    named = _call(
        magic_link_tools.magic_link_request_secret, name="stripe_api_key", reason="invoices"
    )
    assert service.resolve_token(named["url"].rsplit("/", 1)[-1]).flow == "secret-entry"

    provider = _call(magic_link_tools.magic_link_request_secret, provider="anthropic")
    assert service.resolve_token(provider["url"].rsplit("/", 1)[-1]).flow == "provider-key"

    assert "error" in _call(magic_link_tools.magic_link_request_secret)


# ── identity comes from the runtime, not the model ───────────────────────────


def test_notify_target_is_captured_from_the_live_context(owner_chat):
    out = _call(magic_link_tools.magic_link_request_secret, name="STRIPE_API_KEY")
    record = service.resolve_token(out["url"].rsplit("/", 1)[-1])
    assert record.notify == {
        "channel_type": "telegram",
        "target": "12345",
        "agent_name": "merli",
    }
    assert record.requested_by["username"] == "jrivera"
    assert record.requested_by["agent"] == "merli"


def test_tool_returns_the_link_and_tells_the_model_not_to_poll(owner_chat):
    out = _call(magic_link_tools.magic_link_request_secret, name="STRIPE_API_KEY")
    assert out["url"].startswith("https://acme.olano.run/api/connect/")
    assert len(out["pin"]) == 6
    assert "NOT poll" in out["agent_guidance"]


# ── gates ────────────────────────────────────────────────────────────────────


def test_untrusted_sender_is_refused_with_setup_guidance():
    with push_request_context(
        source="channel", channel="telegram", user_id="99", is_owner=False, metadata={}
    ):
        out = _call(magic_link_tools.magic_link_request_secret, name="STRIPE_API_KEY")
    assert "owner-only" in out["error"]
    assert "allowed_users" in out["error"]


def test_group_chat_is_refused(monkeypatch):
    with push_request_context(
        source="channel",
        channel="telegram",
        user_id="12345",
        is_owner=True,
        is_group=True,
        metadata={"chat_id": "-100"},
    ):
        out = _call(magic_link_tools.magic_link_request_secret, name="STRIPE_API_KEY")
    assert "group chat" in out["error"]


def test_missing_public_origin_returns_operator_remediation(owner_chat, monkeypatch):
    monkeypatch.delenv("LIONCLAW_PUBLIC_URL", raising=False)
    out = _call(magic_link_tools.magic_link_request_secret, name="STRIPE_API_KEY")
    assert "LIONCLAW_PUBLIC_URL" in out["error"]
    # And the model is told not to invent a workaround.
    assert "never ask them to paste a secret" in out["agent_guidance"].lower()


# ── magic_link_connect_channels / magic_link_edit_instructions ─────────────────────────────────────


class _FakeCfg:
    channels: list = []
    system_prompt = ""


@pytest.fixture
def fake_agent(monkeypatch):
    """Make ``load_agent_config`` resolve any name (channel-connect /
    edit-prompt validate the agent exists at mint time)."""
    import olano.config.loader as loader

    monkeypatch.setattr(loader, "load_agent_config", lambda name: _FakeCfg())


def test_connect_channels_mints_a_channel_connect_link(owner_chat, fake_agent):
    out = _call(
        magic_link_tools.magic_link_connect_channels, platforms="whatsapp, telegram", agent="marcus"
    )
    assert "error" not in out, out
    record = service.resolve_token(out["url"].rsplit("/", 1)[-1])
    assert record.flow == "channel-connect"
    assert record.params["agent"] == "marcus"
    assert record.params["platforms"] == ["whatsapp", "telegram"]
    assert "NOT poll" in out["agent_guidance"]


def test_connect_channels_defaults_to_the_sending_agent(owner_chat, fake_agent):
    out = _call(magic_link_tools.magic_link_connect_channels, platforms="whatsapp")
    record = service.resolve_token(out["url"].rsplit("/", 1)[-1])
    assert record.params["agent"] == "merli"  # the bound executing agent


def test_connect_channels_needs_a_platform(owner_chat, fake_agent):
    assert "error" in _call(magic_link_tools.magic_link_connect_channels, platforms="   ")


def test_connect_channels_rejects_an_unknown_platform(owner_chat, fake_agent):
    out = _call(magic_link_tools.magic_link_connect_channels, platforms="icq", agent="marcus")
    assert "error" in out
    assert "icq" in out["error"].lower()


def test_connect_channels_is_owner_gated(fake_agent):
    with push_request_context(
        source="channel", channel="telegram", user_id="99", is_owner=False, metadata={}
    ):
        out = _call(
            magic_link_tools.magic_link_connect_channels, platforms="whatsapp", agent="marcus"
        )
    assert "owner-only" in out["error"]


def test_edit_instructions_mints_an_edit_prompt_link(owner_chat, fake_agent):
    out = _call(magic_link_tools.magic_link_edit_instructions, agent="marcus")
    record = service.resolve_token(out["url"].rsplit("/", 1)[-1])
    assert record.flow == "edit-prompt"
    assert record.params["agent"] == "marcus"


# ── file edits ───────────────────────────────────────────────────────────────


def test_request_file_edit_mints_a_workspace_link(owner_chat, tmp_path, monkeypatch):
    import olano.admin.workspace_files as wf

    root = tmp_path / "ws"
    root.mkdir()
    (root / "config.yaml").write_text("mode: fast\n", encoding="utf-8")
    monkeypatch.setattr(wf, "_workspace_root", lambda agent: root)

    out = _call(magic_link_tools.magic_link_request_file_edit, path="config.yaml")
    assert "error" not in out, out
    record = service.resolve_token(out["url"].rsplit("/", 1)[-1])
    assert record.flow == "edit-file"
    # The agent defaults to the one executing the turn.
    assert record.params == {
        "agent": "merli",
        "path": "config.yaml",
        "scope": "workspace",
        "service": "edit-file",
    }


def test_request_file_edit_needs_a_path(owner_chat):
    assert "error" in _call(magic_link_tools.magic_link_request_file_edit, path="   ")


def test_request_file_edit_refuses_an_absolute_path_for_a_plain_agent(
    owner_chat, tmp_path, fake_agent
):
    target = tmp_path / "olano.env"
    target.write_text("TOKEN=abc\n", encoding="utf-8")
    out = _call(magic_link_tools.magic_link_request_file_edit, path=str(target))
    assert "error" in out
    assert "admin agent" in out["error"]


# ── status ───────────────────────────────────────────────────────────────────


def test_magic_link_status_reports_one_link_and_lists_pending(owner_chat):
    out = _call(magic_link_tools.magic_link_request_secret, name="STRIPE_API_KEY")
    one = _call(magic_link_tools.magic_link_status, link_id=out["id"])
    assert one["status"] == store.STATUS_PENDING
    assert "token_hash" not in one

    listing = _call(magic_link_tools.magic_link_status)
    assert any(link["id"] == out["id"] for link in listing["pending"])

    assert "error" in _call(magic_link_tools.magic_link_status, link_id="hs_missing")


# ── /olano connect ───────────────────────────────────────────────────────────


def _command_ctx(agent_name: str = "merli"):
    from types import SimpleNamespace

    return SimpleNamespace(
        config=SimpleNamespace(name=agent_name),
        thread_id="t1",
        sender=SimpleNamespace(
            user_id="12345",
            username="jrivera",
            chat_id="12345",
            channel="telegram",
            is_trusted=True,
            allowlist_configured=True,
        ),
    )


async def test_olano_connect_mints_a_link(owner_chat):
    from olano.platform_control.commands import handle_platform_command

    result = await handle_platform_command(["connect", "secret", "STRIPE_API_KEY"], _command_ctx())
    assert "https://acme.olano.run/api/connect/" in result.output
    assert "PIN:" in result.output


async def test_olano_connect_lists_what_is_connectable(owner_chat):
    from olano.platform_control.commands import handle_platform_command

    result = await handle_platform_command(["connect", "list"], _command_ctx())
    assert "microsoft365" in result.output
    assert "secret <NAME>" in result.output


async def test_olano_connect_without_args_shows_usage(owner_chat):
    from olano.platform_control.commands import handle_platform_command

    result = await handle_platform_command(["connect"], _command_ctx())
    assert "Usage: /olano connect" in result.output


async def test_olano_connect_mcp_requires_an_agent(owner_chat):
    from olano.platform_control.commands import handle_platform_command

    result = await handle_platform_command(
        ["connect", "https://mcp.example.com/mcp"], _command_ctx()
    )
    assert "which" in result.output.lower()


async def test_olano_connect_is_owner_gated(owner_chat):
    from types import SimpleNamespace

    from olano.platform_control.commands import handle_platform_command

    ctx = _command_ctx()
    ctx.sender = SimpleNamespace(
        user_id="99",
        username="stranger",
        chat_id="99",
        channel="telegram",
        is_trusted=False,
        allowlist_configured=True,
    )
    result = await handle_platform_command(["connect", "github"], ctx)
    assert "owner" in result.output.lower()
    assert "api/connect" not in result.output


def test_dashboard_notify_target_is_the_thread(bound_agent):
    """The dashboard chat binds `channel="api"` and — since this fix — the
    thread id as `session_id`, exactly as the platform path does.

    Without the thread id the link was minted with `target: "owner"`, and the
    completion notice had no conversation to land in: the user finished the
    sign-in on their phone and the chat stayed silent.
    """
    ctx = dict(
        source="api",
        channel="api",
        session_id="thread-42",
        metadata={"agent_admin": True},
    )
    with push_request_context(**ctx):
        out = _call(magic_link_tools.magic_link_request_secret, name="STRIPE_API_KEY")

    record = service.resolve_token(out["url"].rsplit("/", 1)[-1])
    assert record.notify == {
        "channel_type": "api",
        "target": "thread-42",
        "agent_name": "merli",
    }
    # And the model is told the chat will report it — a promise that is only
    # true because the watcher now has somewhere to deliver.
    assert "post the result to this chat" in out["agent_guidance"]


def test_dashboard_secret_request_is_described_as_an_inline_card(bound_agent):
    """In the dashboard chat and the app a secret request renders as a form
    card under the reply, so the model must not hand over a url or a PIN
    there; a sign-in link (an OAuth redirect) keeps the Connect-button wording,
    and a messenger keeps the url + pin."""
    dashboard = dict(source="api", channel="api", session_id="thread-42", metadata={})
    with push_request_context(**dashboard):
        secret = _call(magic_link_tools.magic_link_request_secret, name="STRIPE_API_KEY")
        provider = _call(magic_link_tools.magic_link_request_secret, provider="anthropic")
        signin = _call(
            magic_link_tools.magic_link_connect_service, service_name="microsoft365", agent="merli"
        )
    for out in (secret, provider):
        assert "form card" in out["agent_guidance"]
        assert "do NOT paste the url or the pin" in out["agent_guidance"]
        assert "NOT poll" in out["agent_guidance"]
    assert "Connect button card" in signin["agent_guidance"]
    assert "form card" not in signin["agent_guidance"]


def test_messenger_secret_request_still_sends_the_url_and_pin(owner_chat):
    out = _call(magic_link_tools.magic_link_request_secret, name="STRIPE_API_KEY")
    assert "Send the user the url AND the pin" in out["agent_guidance"]
    assert "form card" not in out["agent_guidance"]


# ── the magic_links autonomy capability ──────────────────────────────────────


def _write_autonomy_block(block: dict) -> None:
    import yaml

    from olano.config.home import get_olano_paths, write_home_config_text

    paths = get_olano_paths()
    paths.root.mkdir(parents=True, exist_ok=True)
    raw: dict = {}
    if paths.config_path.exists():
        raw = yaml.safe_load(paths.config_path.read_text()) or {}
    raw["agent_autonomy"] = block
    write_home_config_text(paths.config_path, yaml.safe_dump(raw, sort_keys=False))


def test_capability_off_refuses_every_mint_with_guidance(owner_chat):
    """Binding is visibility only: the capability is re-read on every call,
    so a switch flipped after boot refuses immediately — and names the knob."""
    _write_autonomy_block({"magic_links": False})
    out = _call(magic_link_tools.magic_link_request_secret, name="stripe_api_key")
    assert "turned off" in out["error"]
    assert "Send magic links" in out["how_to_enable"]
    status = _call(magic_link_tools.magic_link_status)
    assert "turned off" in status["error"]


def test_capability_refusal_wins_over_ownership():
    """A non-owner on a deployment with links off gets the same answer as an
    owner — the refusal order never leaks the capability state."""
    _write_autonomy_block({"magic_links": False})
    ctx = dict(
        source="channel",
        channel="telegram",
        user_id="999",
        session_id="999",
        authenticated=True,
        is_owner=False,
        is_group=False,
        metadata={"chat_id": "999"},
    )
    with push_request_context(**ctx):
        out = _call(magic_link_tools.magic_link_request_secret, name="stripe_api_key")
    assert "turned off" in out["error"]


def test_capability_on_by_default_still_mints(owner_chat):
    out = _call(magic_link_tools.magic_link_request_secret, name="stripe_api_key")
    assert "error" not in out, out
    assert out["url"] and out["pin"]
