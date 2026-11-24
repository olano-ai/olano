"""MCP wiring an agent can drive on its own: directory lookup, API-key servers,
and the config entry a completed sign-in leaves behind.

Three holes this covers, each of which failed silently before:

* AgentFather recommended MCP servers from model memory because nothing
  exposed the directory as a tool — a subtly wrong endpoint saves happily and
  fails only when someone tries to use it.
* ``olano_add_mcp_server`` could not express ``env`` or ``headers``, so the
  usual way to authenticate a remote MCP server (an API key in a header) was
  reachable only from the dashboard, and a ``${vault:...}`` ref in a header was
  never resolved anyway — forcing the key to sit as a literal in agent.json5.
* The ``mcp-oauth`` magic link stored tokens without ever writing the server
  into the agent's config, so a user could complete a sign-in for a server
  their agent would never load.
"""

from __future__ import annotations

import pytest

from olano import admin
from olano.core import mcp_setup
from olano.magic_links import flows
from olano.magic_links.store import LinkRecord

# ── directory lookup ─────────────────────────────────────────────────────────


def test_search_returns_the_fields_an_agent_decides_on() -> None:
    hits = admin.search_mcp_directory("hubspot")
    assert hits, "the bundled directory ships a hubspot preset"
    entry = hits[0]
    assert entry["slug"] == "hubspot"
    # The point of the directory: a verified endpoint and how to authenticate.
    assert entry["url"].startswith("http")
    assert entry["auth"]
    assert entry["transport"]


def test_search_trims_dashboard_chrome_out_of_the_payload() -> None:
    """Logos, badges and example JSON are for the dashboard; they cost an agent
    prompt tokens and inform nothing."""
    entry = admin.search_mcp_directory("hubspot")[0]
    for noise in ("logoUrl", "badge", "exampleJson", "runCommand"):
        assert noise not in entry


def test_search_with_no_query_lists_the_directory() -> None:
    assert len(admin.search_mcp_directory("", limit=5)) == 5


def test_get_entry_by_slug_and_a_useful_error_for_an_unknown_one() -> None:
    assert admin.get_mcp_directory_entry("stripe")["slug"] == "stripe"
    with pytest.raises(ValueError, match="search_mcp_directory"):
        admin.get_mcp_directory_entry("no-such-server")


def test_both_directory_tools_resolve_by_name() -> None:
    """They are useless to an agent unless the registry can hand them over."""
    from olano.tools.registry import resolve_tools

    names = ["olano_search_mcp_directory", "olano_get_mcp_directory_entry"]
    assert [t.name for t in resolve_tools(names)] == names


def test_agentfather_ships_the_directory_tools() -> None:
    from olano.archetypes.installer import find_archetype_path
    from olano.config.json5_fast import loads

    data = loads(find_archetype_path("agentfather").read_text(encoding="utf-8"))
    assert "olano_search_mcp_directory" in data["tools"]
    assert "olano_get_mcp_directory_entry" in data["tools"]


# ── API-key servers: env / headers / args ────────────────────────────────────


def test_add_mcp_server_persists_env_headers_and_args() -> None:
    admin.create_agent("keyagent", description="api-key mcp")
    admin.add_mcp_server(
        "keyagent",
        "https://mcp.acme.test/mcp",
        transport="http",
        headers={"Authorization": "Bearer ${vault:ACME_API_KEY}"},
    )
    admin.add_mcp_server(
        "keyagent",
        "local-notion",
        transport="stdio",
        command="npx",
        args=["-y", "notion-mcp"],
        env={"NOTION_API_KEY": "${vault:NOTION_API_KEY}"},
    )
    servers = {s["url"]: s for s in admin.list_mcp_servers("keyagent")}
    assert servers["https://mcp.acme.test/mcp"]["headers"] == {
        "Authorization": "Bearer ${vault:ACME_API_KEY}"
    }
    assert servers["local-notion"]["args"] == ["-y", "notion-mcp"]
    assert servers["local-notion"]["env"] == {"NOTION_API_KEY": "${vault:NOTION_API_KEY}"}


def test_the_agent_tool_can_express_headers_and_env() -> None:
    """The admin layer always accepted these; the tool signature did not, which
    is what stranded every API-key MCP server outside the dashboard."""
    import json

    from olano.tools.registry import resolve_tools

    admin.create_agent("toolagent", description="via the tool")
    (add,) = resolve_tools(["olano_add_mcp_server"])
    add.invoke(
        {
            "agent": "toolagent",
            "url": "https://mcp.acme.test/mcp",
            "transport": "http",
            "headers": {"X-Api-Key": "${vault:ACME_API_KEY}"},
        }
    )
    servers = admin.list_mcp_servers("toolagent")
    assert servers[0]["headers"] == {"X-Api-Key": "${vault:ACME_API_KEY}"}
    # `args` is reserved by langchain's schema generation, so the tool spells it
    # `command_args` and maps it onto the config's `args`.
    add.invoke(
        {
            "agent": "toolagent",
            "url": "local-stdio",
            "transport": "stdio",
            "command": "npx",
            "command_args": ["-y", "some-mcp"],
            "env": {"K": "${vault:K}"},
        }
    )
    stdio = next(s for s in admin.list_mcp_servers("toolagent") if s["url"] == "local-stdio")
    assert stdio["args"] == ["-y", "some-mcp"]
    assert stdio["env"] == {"K": "${vault:K}"}
    # and the tool reports what it did rather than raising into the turn
    assert json.loads(add.invoke({"agent": "toolagent", "url": "https://b.test"}))["added"]


class _Vault:
    """Fake vault that keys on (name, agent) so per-agent precedence is real."""

    def __init__(self, values: dict[tuple[str, str | None], str]) -> None:
        self.values = values
        self.calls: list[tuple[str, str | None]] = []

    def get(self, name: str, agent: str | None = None, touch: bool = True) -> str:
        self.calls.append((name, agent))
        if (name, agent) in self.values:
            return self.values[(name, agent)]
        return self.values[(name, None)]  # common value fallback, like the real store


def test_vault_refs_in_headers_resolve_per_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An API key in an Authorization header used to have to be a literal in
    agent.json5 — headers were passed through verbatim while env was resolved.
    The fake keys on (name, agent): the override must win for the named agent."""
    vault = _Vault(
        {
            ("ACME_API_KEY", None): "sk-common",
            ("ACME_API_KEY", "merli"): "sk-merli-override",
        }
    )
    monkeypatch.setattr("olano.vault.resolver.get_vault", lambda: vault)

    resolved = mcp_setup.agent_secret_headers(
        {"Authorization": "Bearer ${vault:ACME_API_KEY}", "X-Plain": "literal"}, "merli"
    )
    assert resolved == {"Authorization": "Bearer sk-merli-override", "X-Plain": "literal"}
    assert ("ACME_API_KEY", "merli") in vault.calls

    # Another agent with no override gets the common value, not merli's.
    other = mcp_setup.agent_secret_headers(
        {"Authorization": "Bearer ${vault:ACME_API_KEY}"}, "sales"
    )
    assert other == {"Authorization": "Bearer sk-common"}


def test_headers_without_a_ref_never_open_the_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom():
        raise AssertionError("the vault was opened for headers that hold no refs")

    monkeypatch.setattr("olano.vault.resolver.get_vault", _boom)
    assert mcp_setup.agent_secret_headers({"X-Plain": "literal"}, "merli") == {"X-Plain": "literal"}
    assert mcp_setup.agent_secret_headers(None, "merli") == {}


def test_an_unresolvable_ref_is_left_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blanking it would send an empty credential and read as a server bug;
    leaving it reaches the server as itself and names the header in the error."""

    class _Empty:
        def get(self, name, agent=None, touch=True):
            raise KeyError(name)

    monkeypatch.setattr("olano.vault.resolver.get_vault", lambda: _Empty())
    out = mcp_setup.agent_secret_headers({"Authorization": "Bearer ${vault:MISSING}"}, "merli")
    assert out == {"Authorization": "Bearer ${vault:MISSING}"}


def test_connection_params_carry_the_resolved_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    vault = _Vault({("ACME_API_KEY", None): "sk-live-42"})
    monkeypatch.setattr("olano.vault.resolver.get_vault", lambda: vault)
    monkeypatch.setattr(mcp_setup, "has_oauth_tokens", lambda *a, **k: False)

    from olano.config.agent_config import MCPServerConfig

    srv = MCPServerConfig(
        url="https://mcp.acme.test/mcp",
        transport="http",
        headers={"Authorization": "Bearer ${vault:ACME_API_KEY}"},
    )
    params = mcp_setup.build_connection_params(srv, "merli")
    assert params is not None
    assert params["headers"] == {"Authorization": "Bearer sk-live-42"}


def test_a_vault_that_raises_degrades_to_verbatim_not_a_boot_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_connection_params runs on the agent-boot path with no guard at its
    call site — a vault hiccup must cost one server a 401, never the boot."""

    def _broken():
        raise RuntimeError("vault backend down")

    monkeypatch.setattr("olano.vault.resolver.get_vault", _broken)
    out = mcp_setup.agent_secret_headers({"Authorization": "Bearer ${vault:K}"}, "merli")
    assert out == {"Authorization": "Bearer ${vault:K}"}


async def test_probe_sends_resolved_headers_not_the_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe used to send ${vault:...} verbatim: the resulting 401 tripped
    the OAuth sniff and olano_test_mcp_server reported auth_required for an
    API-key server that works at runtime — a diagnostic that lies, in exactly
    the workflow the AgentFather prompt steers into."""
    vault = _Vault({("ACME_API_KEY", None): "sk-live-42"})
    monkeypatch.setattr("olano.vault.resolver.get_vault", lambda: vault)

    seen: dict[str, object] = {}

    async def _fake_connect(url, transport, *, headers=None, log=None, **kwargs):
        seen["headers"] = headers
        return []

    monkeypatch.setattr(mcp_setup, "_connect_and_list_tools", _fake_connect)
    result = await mcp_setup.probe_server(
        "merli",
        {
            "url": "https://mcp.acme.test/mcp",
            "transport": "http",
            "headers": {"Authorization": "Bearer ${vault:ACME_API_KEY}"},
        },
    )
    assert seen["headers"] == {"Authorization": "Bearer sk-live-42"}
    assert result["ok"] is True


# ── the sign-in that used to leave nothing behind ────────────────────────────


def _link(**params) -> LinkRecord:
    record = LinkRecord(id="ml_test", token_hash="x", flow="mcp-oauth", params=dict(params))
    record.session_id = "sess1"
    return record


async def test_completed_signin_registers_a_server_the_agent_lacked() -> None:
    admin.create_agent("oauthagent", description="mcp oauth")
    assert admin.list_mcp_servers("oauthagent") == []

    flow = flows.get_flow("mcp-oauth")
    await flow.post_complete(_link(agent="oauthagent", url="https://mcp.zoho.test/mcp"))

    servers = admin.list_mcp_servers("oauthagent")
    assert [s["url"] for s in servers] == ["https://mcp.zoho.test/mcp"]
    assert servers[0]["auth"] == "oauth"
    # No minter passes a transport and the flow session is gone here, so the
    # fallback must be "http" — normalize_transport("auto") is streamable HTTP,
    # which is what an auto-flow actually connected over. "sse" would pin the
    # entry to an endpoint most modern /mcp servers do not serve.
    assert servers[0]["transport"] == "http"


async def test_backfill_uses_the_transport_the_signin_connected_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flow session records the transport the OAuth handshake ran on; the
    back-fill must write that, not a guess — the runtime connect path has no
    http↔sse fallback."""
    import olano.magic_links.adapters.mcp_oauth as adapter

    class _Session:
        transport = "sse"

    monkeypatch.setattr(
        "olano.core.mcp_setup.poll_oauth_flow",
        lambda session_id: _Session() if session_id == "sess1" else None,
    )
    admin.create_agent("sseagent", description="sse server")
    await flows.get_flow("mcp-oauth").post_complete(
        _link(agent="sseagent", url="https://legacy.example.test/sse")
    )
    assert admin.list_mcp_servers("sseagent")[0]["transport"] == "sse"
    # And the SDK spelling maps to the config spelling.
    _Session.transport = "streamable_http"
    assert adapter.McpOAuthFlow._session_transport(_link(agent="x", url="https://y")) == "http"


async def test_an_existing_entry_is_never_clobbered() -> None:
    admin.create_agent("keepagent", description="already configured")
    admin.add_mcp_server(
        "keepagent",
        "https://mcp.zoho.test/mcp",
        transport="http",
        auth="oauth",
        oauth_client_id="preregistered-id",
    )
    flow = flows.get_flow("mcp-oauth")
    await flow.post_complete(_link(agent="keepagent", url="https://mcp.zoho.test/mcp"))

    servers = admin.list_mcp_servers("keepagent")
    assert len(servers) == 1
    assert servers[0]["transport"] == "http"
    assert servers[0]["oauth_client_id"] == "preregistered-id"


async def test_an_explicit_transport_on_the_link_is_honoured() -> None:
    admin.create_agent("httpagent", description="knows its transport")
    flow = flows.get_flow("mcp-oauth")
    await flow.post_complete(
        _link(agent="httpagent", url="https://mcp.zoho.test/mcp", transport="http")
    )
    assert admin.list_mcp_servers("httpagent")[0]["transport"] == "http"


async def test_a_config_failure_never_invalidates_the_token_but_degrades_the_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user has already signed in by this point — a config hiccup must be
    logged, not raised into a flow that would then look failed. But the ✅
    notice is built from link.result AFTER this hook, so it must carry the
    caveat: a server that was never written must not report as connected."""
    import olano.magic_links.adapters.mcp_oauth as adapter

    monkeypatch.setattr(
        adapter.McpOAuthFlow,
        "_ensure_configured",
        lambda self, link: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    link = _link(agent="x", url="https://y.test")
    link.result = "x is connected to y.test (3 tools)."
    await flows.get_flow("mcp-oauth").post_complete(link)
    assert "could not be added" in link.result
    assert "disk full" in link.result
    assert link.result.startswith("x is connected")  # the sign-in itself did succeed
