"""MCP OAuth client secrets live in the vault, never in agent.json5.

The sibling of ``test_channel_secrets.py``: a HubSpot-style server needs an
OAuth app of the user's own, and its client secret used to be written to the
server entry in plain text by every path that collected it. Now every config
writer moves it into the vault as a per-agent override under a key named after
the server, and the entry keeps a ``${vault:KEY}`` reference that every reader
resolves per agent.
"""

from __future__ import annotations

from typing import Any

import pytest

from olano.config.mcp_secrets import (
    MAX_KEY_LENGTH,
    absorb_mcp_client_secrets,
    client_secret_key,
    reference_for,
)


class FakeVault:
    """Records what was stored, and under which agent scope."""

    def __init__(self, fail: bool = False) -> None:
        self.stored: dict[tuple[str, str | None], str] = {}
        self.fail = fail

    def set(self, name: str, value: str, agent: str | None = None, **kw: Any) -> None:
        if self.fail:
            raise RuntimeError("vault write failed")
        self.stored[(name, agent)] = value

    def get(self, name: str, agent: str | None = None, *, touch: bool = True) -> str:
        for scope in (agent, None):
            if (name, scope) in self.stored:
                return self.stored[(name, scope)]
        raise KeyError(name)


HUBSPOT = "https://mcp.hubspot.com"
SECRET = "hs-secret-1234"


# ── Key derivation ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,expected",
    [
        (HUBSPOT, "MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM"),
        ("https://mcp.hubspot.com/", "MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM"),
        ("HTTPS://MCP.HUBSPOT.COM", "MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM"),
        ("https://mcp.notion.com/mcp", "MCP_OAUTH_CLIENT_SECRET_MCP_NOTION_COM_MCP"),
        # Two instances on one host never share a secret: the path is in the name.
        (
            "https://backend.composio.dev/v3/mcp/abc-123",
            "MCP_OAUTH_CLIENT_SECRET_BACKEND_COMPOSIO_DEV_V3_MCP_ABC_123",
        ),
    ],
)
def test_key_is_derived_from_host_and_path(url, expected):
    assert client_secret_key(url) == expected
    assert reference_for(url) == f"${{vault:{expected}}}"


def test_key_is_capped_and_still_unique():
    long_a = "https://mcp.example.com/" + "a" * 80 + "/one"
    long_b = "https://mcp.example.com/" + "a" * 80 + "/two"
    key_a, key_b = client_secret_key(long_a), client_secret_key(long_b)
    assert len(key_a) <= MAX_KEY_LENGTH and len(key_b) <= MAX_KEY_LENGTH
    assert key_a != key_b
    assert key_a.startswith("MCP_OAUTH_CLIENT_SECRET_MCP_EXAMPLE_COM")


# ── Absorbing ─────────────────────────────────────────────────────────────────


def _config(**entry: Any) -> dict[str, Any]:
    server = {"url": HUBSPOT, "transport": "http", "auth": "oauth", "oauth_client_id": "cid"}
    server.update(entry)
    return {"name": "merli", "mcp_servers": [server]}


def test_literal_secret_moves_to_a_per_agent_override_and_leaves_a_reference():
    data = _config(oauth_client_secret=SECRET)
    vault = FakeVault()

    report = absorb_mcp_client_secrets("merli", data, vault=vault)

    entry = data["mcp_servers"][0]
    assert entry["oauth_client_secret"] == "${vault:MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM}"
    assert vault.stored == {("MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM", "merli"): SECRET}
    assert report.vaulted == [(HUBSPOT, "MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM")]
    assert report.changed and report.vault_available
    assert entry["oauth_client_id"] == "cid"  # the id is not a secret — untouched


def test_two_agents_share_the_key_name_and_keep_separate_values():
    vault = FakeVault()
    a = _config(oauth_client_secret="secret-a")
    b = _config(oauth_client_secret="secret-b")
    absorb_mcp_client_secrets("marisol", a, vault=vault)
    absorb_mcp_client_secrets("andrew", b, vault=vault)
    key = "MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM"
    assert vault.stored == {(key, "marisol"): "secret-a", (key, "andrew"): "secret-b"}


def test_absorb_is_idempotent_and_leaves_any_reference_alone():
    vault = FakeVault()
    data = _config(oauth_client_secret=SECRET)
    absorb_mcp_client_secrets("merli", data, vault=vault)
    second = absorb_mcp_client_secrets("merli", data, vault=vault)
    assert not second.changed and len(vault.stored) == 1

    # A reference the operator pointed elsewhere is deliberate — never touched.
    custom = _config(oauth_client_secret="${vault:MY_HUBSPOT_APP_SECRET}")
    assert not absorb_mcp_client_secrets("merli", custom, vault=vault).changed
    assert custom["mcp_servers"][0]["oauth_client_secret"] == "${vault:MY_HUBSPOT_APP_SECRET}"


def test_entries_without_a_secret_are_skipped():
    vault = FakeVault()
    data = {
        "name": "merli",
        "mcp_servers": [
            {"url": "https://mcp.stripe.com", "auth": "oauth"},
            {"url": HUBSPOT, "auth": "oauth", "oauth_client_secret": ""},
            "not-a-dict",
        ],
    }
    assert not absorb_mcp_client_secrets("merli", data, vault=vault).changed
    assert vault.stored == {}
    assert not absorb_mcp_client_secrets("merli", {"name": "merli"}, vault=vault).changed


def test_locked_vault_leaves_the_literal_and_says_so(monkeypatch):
    monkeypatch.setattr("olano.config.channel_secrets._resolve_vault", lambda: None)
    data = _config(oauth_client_secret=SECRET)
    report = absorb_mcp_client_secrets("merli", data)
    assert data["mcp_servers"][0]["oauth_client_secret"] == SECRET  # nothing lost
    assert report.vault_available is False
    assert report.left_in_config == [HUBSPOT]


def test_refused_write_leaves_the_literal():
    data = _config(oauth_client_secret=SECRET)
    report = absorb_mcp_client_secrets("merli", data, vault=FakeVault(fail=True))
    assert data["mcp_servers"][0]["oauth_client_secret"] == SECRET
    assert report.left_in_config == [HUBSPOT] and not report.changed


def test_dry_run_reports_without_writing():
    vault = FakeVault()
    data = _config(oauth_client_secret=SECRET)
    report = absorb_mcp_client_secrets("merli", data, vault=vault, dry_run=True)
    assert report.vaulted == [(HUBSPOT, "MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM")]
    assert vault.stored == {}
    assert data["mcp_servers"][0]["oauth_client_secret"] == SECRET


# ── The save path ─────────────────────────────────────────────────────────────


def test_save_agent_config_never_writes_the_literal(monkeypatch, base_config):
    """Every model-based writer funnels through save_agent_config — one hook
    covers mutate_agent_config, admin.update_agent, add_mcp_server and the
    magic-link form."""
    from olano.config.agent_config import MCPServerConfig
    from olano.config.loader import load_agent_config, save_agent_config

    vault = FakeVault()
    monkeypatch.setattr("olano.config.channel_secrets._resolve_vault", lambda: vault)
    base_config.mcp_servers = [
        MCPServerConfig(url=HUBSPOT, transport="http", auth="oauth", oauth_client_secret=SECRET)
    ]
    path = save_agent_config(base_config)
    text = path.read_text(encoding="utf-8")
    assert SECRET not in text
    assert "${vault:MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM}" in text
    assert vault.stored == {("MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM", base_config.name): SECRET}
    # The reference survives a load → save round trip untouched.
    reloaded = load_agent_config(base_config.name)
    assert reloaded.mcp_servers[0].oauth_client_secret == (
        "${vault:MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM}"
    )
    save_agent_config(reloaded)
    assert len(vault.stored) == 1


def test_save_agent_config_keeps_the_literal_when_the_vault_is_locked(monkeypatch, base_config):
    """A locked vault must cost the vaulting, never the save."""
    from olano.config.agent_config import MCPServerConfig
    from olano.config.loader import save_agent_config

    monkeypatch.setattr("olano.config.channel_secrets._resolve_vault", lambda: None)
    base_config.mcp_servers = [
        MCPServerConfig(url=HUBSPOT, auth="oauth", oauth_client_secret=SECRET)
    ]
    path = save_agent_config(base_config)
    assert SECRET in path.read_text(encoding="utf-8")


# ── The read path ─────────────────────────────────────────────────────────────


@pytest.fixture
def live_vault(monkeypatch):
    from olano.vault import resolver

    vault = FakeVault()
    previous = resolver.get_vault()
    resolver.set_vault(vault)
    yield vault
    resolver.set_vault(previous)


def test_readers_resolve_the_reference_per_agent(live_vault):
    from olano.config.agent_config import MCPServerConfig
    from olano.core.mcp_setup import resolve_client_credentials

    key = "MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM"
    live_vault.stored[(key, "merli")] = "merli-secret"
    live_vault.stored[(key, None)] = "shared-secret"

    entry = {"oauth_client_id": "cid", "oauth_client_secret": f"${{vault:{key}}}"}
    assert resolve_client_credentials(entry, "merli") == ("cid", "merli-secret")
    assert resolve_client_credentials(entry, "other") == ("cid", "shared-secret")
    model = MCPServerConfig(
        url=HUBSPOT, oauth_client_id="cid", oauth_client_secret=f"${{vault:{key}}}"
    )
    assert resolve_client_credentials(model, "merli") == ("cid", "merli-secret")
    # A literal (pre-vaulting config, or a value just typed) passes through.
    assert resolve_client_credentials({"oauth_client_secret": "lit"}, "merli") == (None, "lit")
    assert resolve_client_credentials({}, "merli") == (None, None)


def test_readers_keep_an_unresolvable_reference_verbatim(live_vault, monkeypatch):
    """A blanked secret reads as a server bug; the literal reference 401s with
    a name to pull on — and a locked vault does the same."""
    from olano.core.mcp_setup import resolve_client_credentials
    from olano.vault import resolver

    ref = "${vault:MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM}"
    assert resolve_client_credentials({"oauth_client_secret": ref}, "merli") == (None, ref)
    resolver.set_vault(None)
    assert resolve_client_credentials({"oauth_client_secret": ref}, "merli") == (None, ref)


def test_runtime_connect_hands_the_provider_the_resolved_secret(live_vault, monkeypatch):
    from olano.config.agent_config import MCPServerConfig
    from olano.core import mcp_setup

    key = "MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM"
    live_vault.stored[(key, "merli")] = "merli-secret"
    seen: dict = {}

    def _capture(agent, url, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(mcp_setup, "make_oauth_provider", _capture)
    srv = MCPServerConfig(
        url=HUBSPOT,
        transport="http",
        auth="oauth",
        oauth_client_id="cid",
        oauth_client_secret=f"${{vault:{key}}}",
    )
    params = mcp_setup.build_connection_params(srv, "merli")
    assert params is not None and "auth" in params
    assert seen["client_id"] == "cid" and seen["client_secret"] == "merli-secret"
