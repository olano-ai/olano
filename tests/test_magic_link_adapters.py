"""Tests for the magic-link flow adapters.

Adapters are translation layers over auth machinery that already exists, so
these mock that machinery and assert the translation: the right underlying
session is started, its own status vocabulary is mapped onto the store's, and
the page gets the fields it needs to render.
"""

from __future__ import annotations

import pytest

from olano.magic_links import flows, service, store
from olano.magic_links.store import LinkRecord


@pytest.fixture(autouse=True)
def public_origin(monkeypatch):
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://acme.olano.run")
    import olano.core.mcp_setup as mcp_setup

    monkeypatch.setattr(mcp_setup, "_LAST_SEEN_ORIGIN", None)


def _record(flow: str, **params) -> LinkRecord:
    return LinkRecord(id="hs_test", token_hash="x", flow=flow, params=dict(params))


def _with_session(flow: str, **params) -> LinkRecord:
    """A record whose underlying flow has already been started."""
    record = _record(flow, **params)
    record.session_id = "sess1"
    return record


# ── connector-device ─────────────────────────────────────────────────────────


def test_device_refuses_a_provider_without_a_code():
    with pytest.raises(ValueError, match="does not offer a sign-in code"):
        flows.get_flow("connector-device").validate({"service": "microsoft365"})
    with pytest.raises(ValueError, match="Unknown service"):
        flows.get_flow("connector-device").validate({"service": "nope"})


def test_device_freezes_scope_at_mint_time():
    out = flows.get_flow("connector-device").validate(
        {"service": "GitHub", "agent": "merli", "shared": True}
    )
    assert out == {"service": "github", "agent": "merli", "shared": True}
    # The alias pins the service; everything else passes through.
    assert flows.get_flow("github-device").validate({"agent": "merli"}) == {
        "service": "github",
        "agent": "merli",
        "shared": False,
    }


def test_device_maps_the_broker_session_onto_page_fields(monkeypatch):
    import olano.core.connectors as connectors

    session = {
        "session_id": "sess1",
        "service": "github",
        "status": "pending",
        "user_code": "F8D7-4317",
        "verification_url": "https://github.com/login/device",
        "agent": "merli",
    }
    seen: dict = {}

    def fake_start(provider, *, agent=None, shared=False):
        seen.update(provider=provider.id, agent=agent, shared=shared)
        return session

    monkeypatch.setattr(connectors, "start_device_auth", fake_start)
    monkeypatch.setattr(connectors, "poll_device_auth", lambda sid: session)

    flow = flows.get_flow("connector-device")
    link = _record("connector-device", service="github", agent="merli", shared=True)
    assert flow.start(link) == {"session_id": "sess1"}
    assert seen == {"provider": "github", "agent": "merli", "shared": True}

    live = _with_session("connector-device", service="github", agent="merli")
    state = flow.state(live)
    assert state["status"] == store.STATUS_IN_PROGRESS
    assert state["user_code"] == "F8D7-4317"
    assert state["verification_url"].endswith("/login/device")

    session.update(status="approved", account="jrivera")
    done = flow.state(live)
    assert done["status"] == store.STATUS_DONE
    assert "jrivera" in done["result"] and "merli" in done["result"]

    session.update(status="expired", error_message="The device code expired before approval.")
    lapsed = flow.state(live)
    assert lapsed["status"] == store.STATUS_EXPIRED
    assert "expired" in lapsed["error"]


# ── connector-oauth ──────────────────────────────────────────────────────────


def test_connector_validate_rejects_an_unknown_service():
    flow = flows.get_flow("connector-oauth")
    with pytest.raises(ValueError, match="Unknown service"):
        flow.validate({"service": "not-a-real-provider"})
    with pytest.raises(ValueError, match="service is required"):
        flow.validate({})


def test_connector_freezes_scope_at_mint_time():
    """Whoever holds the link cannot change which agent the token lands on."""
    flow = flows.get_flow("connector-oauth")
    params = flow.validate({"service": "github", "agent": "merli", "shared": True})
    assert params == {"service": "github", "agent": "merli", "shared": True}


def test_connector_start_uses_the_public_origin_callback(monkeypatch):
    import olano.core.connectors as connectors

    captured = {}

    class _Session:
        session_id = "csess"
        status = "awaiting_authorization"
        authorization_url = "https://github.com/login/oauth/authorize?state=abc"
        error = None

    def _fake_start(provider, *, creds, agent, shared, redirect_uri):
        captured.update(provider=provider.id, agent=agent, redirect_uri=redirect_uri)
        return _Session()

    monkeypatch.setattr(connectors, "start_connector_auth", _fake_start)
    monkeypatch.setattr(connectors, "poll_connector_auth", lambda sid: _Session())

    flow = flows.get_flow("connector-oauth")
    record = _record("connector-oauth", service="github", agent="merli")
    assert flow.start(record) == {"session_id": "csess"}
    assert captured["redirect_uri"] == "https://acme.olano.run/api/auth/github/callback"
    assert captured["agent"] == "merli"

    state = flow.state(_with_session("connector-oauth", service="github", agent="merli"))
    assert state["status"] == store.STATUS_IN_PROGRESS
    assert state["authorization_url"].startswith("https://github.com/login/oauth/authorize")


# ── mcp-oauth ────────────────────────────────────────────────────────────────


def test_mcp_requires_both_an_agent_and_a_url():
    flow = flows.get_flow("mcp-oauth")
    with pytest.raises(ValueError, match="agent is required"):
        flow.validate({"url": "https://mcp.example.com/mcp"})
    with pytest.raises(ValueError, match="URL is required"):
        flow.validate({"agent": "merli"})
    assert (
        flow.validate({"agent": "merli", "url": "https://mcp.example.com/mcp"})["agent"] == "merli"
    )


def test_mcp_descriptor_names_the_host():
    """A server the directory does not know keeps its host as the name."""
    flow = flows.get_flow("mcp-oauth")
    record = _record("mcp-oauth", agent="merli", url="https://mcp.zoho.com/mcp")
    desc = flow.descriptor(record)
    assert desc["service"]["name"] == "mcp.zoho.com"
    assert desc["mode"] == "signin" and desc["fields"] == []


HUBSPOT = "https://mcp.hubspot.com"
STRIPE = "https://mcp.stripe.com"


@pytest.fixture
def offline_directory(monkeypatch):
    """The bundled MCP directory: HubSpot is `preregistered`, Stripe is `dcr`."""
    from olano.core import mcp_registry

    mcp_registry._cache.clear()
    monkeypatch.setattr(mcp_registry, "_fetch_full", lambda *a, **k: None)
    yield
    mcp_registry._cache.clear()


class _Session:
    def __init__(self, status="error", *, error_code=None, error="", session_id="sess1"):
        self.status = status
        self.error_code = error_code
        self.error = error
        self.session_id = session_id
        self.authorization_url = None
        self.tool_count = 0
        self.transport = "streamable_http"


def test_mcp_validate_opens_on_the_form_for_preregistered_servers(offline_directory):
    """The directory decides the first page: HubSpot wants an OAuth app of the
    user's own, so the link opens on the credentials form; Stripe registers a
    client itself, so it opens on the hand-off."""
    flow = flows.get_flow("mcp-oauth")
    hubspot = flow.validate({"agent": "merli", "url": HUBSPOT})
    assert hubspot["needs_client"] is True
    assert hubspot["retry_reason"] == "known"
    assert hubspot["directory"]["name"] == "HubSpot"
    assert hubspot["directory"]["oauth_client"] == "preregistered"
    assert "MCP Auth Apps" in hubspot["directory"]["console_label"]
    assert hubspot["composio"]["connected"] is False
    assert hubspot["composio"]["signup_url"] == "https://composio.dev"

    stripe = flow.validate({"agent": "merli", "url": STRIPE})
    assert stripe["needs_client"] is False
    assert stripe["directory"]["name"] == "Stripe"


def test_mcp_validate_skips_the_form_when_a_client_is_already_configured(
    offline_directory, monkeypatch
):
    """Credentials typed in the dashboard count — the link must not ask twice."""
    from olano.magic_links.adapters import mcp_oauth

    monkeypatch.setattr(
        mcp_oauth,
        "configured_entry",
        lambda agent, url: {"url": url, "oauth_client_id": "from-dashboard"},
    )
    params = flows.get_flow("mcp-oauth").validate({"agent": "merli", "url": HUBSPOT})
    assert params["needs_client"] is False
    # And a client id handed in at mint time does the same.
    monkeypatch.setattr(mcp_oauth, "configured_entry", lambda agent, url: None)
    params = flows.get_flow("mcp-oauth").validate(
        {"agent": "merli", "url": HUBSPOT, "oauth_client_id": "minted"}
    )
    assert params["needs_client"] is False


def test_mcp_credentials_descriptor_carries_everything_the_form_needs(offline_directory):
    flow = flows.get_flow("mcp-oauth")
    record = _record("mcp-oauth", **flow.validate({"agent": "merli", "url": HUBSPOT}))
    desc = flow.descriptor(record)
    assert desc["kind"] == "mcp" and desc["mode"] == "credentials"
    assert desc["title"] == "Connect HubSpot"
    assert [f["name"] for f in desc["fields"]] == ["client_id", "client_secret"]
    assert desc["fields"][1]["secret"] is True and desc["fields"][1]["required"] is False
    # The exact callback the flow will send — the one the OAuth app must carry.
    assert desc["redirect_uri"] == "https://acme.olano.run/api/mcp/oauth/callback"
    assert desc["guide"]["console_url"] == "https://app.hubspot.com/"
    assert "MCP Auth Apps" in desc["guide"]["console_label"]
    assert "does not register apps automatically" in desc["instructions"]
    assert desc["cta"] == "Save and continue to HubSpot"
    composio = desc["composio"]
    assert composio["connected"] is False
    assert composio["agent"] == "merli"
    assert composio["free_calls_per_month"] == 20_000
    assert composio["message"] == (
        "First give me a magic link to connect to the Composio MCP, and after that "
        "help me connect to HubSpot via the Composio MCP."
    )


def test_mcp_composio_message_when_already_connected(offline_directory):
    flow = flows.get_flow("mcp-oauth")
    params = flow.validate({"agent": "merli", "url": HUBSPOT})
    params["composio"]["connected"] = True
    desc = flow.descriptor(_record("mcp-oauth", **params))
    assert desc["composio"]["message"] == "Help me connect to HubSpot via the Composio MCP."


def test_mcp_composio_status_needs_an_enabled_server_with_tokens(offline_directory, monkeypatch):
    import olano.admin.mcp as admin_mcp
    import olano.core.mcp_setup as mcp_setup
    from olano.magic_links.adapters.mcp_oauth import composio_status

    servers = [{"url": "https://connect.composio.dev/mcp", "auth": "oauth", "enabled": True}]
    monkeypatch.setattr(admin_mcp, "list_mcp_servers", lambda agent: servers)
    monkeypatch.setattr(mcp_setup, "has_oauth_tokens", lambda agent, url: True)
    assert composio_status("merli")["connected"] is True
    monkeypatch.setattr(mcp_setup, "has_oauth_tokens", lambda agent, url: False)
    assert composio_status("merli")["connected"] is False
    servers[0]["enabled"] = False
    monkeypatch.setattr(mcp_setup, "has_oauth_tokens", lambda agent, url: True)
    assert composio_status("merli")["connected"] is False
    # An API-key instance carries its credential in the header — connected.
    servers[:] = [{"url": "https://backend.composio.dev/v3/mcp/abc", "auth": "none"}]
    assert composio_status("merli")["connected"] is True


def test_mcp_state_returns_to_the_form_when_the_provider_refuses_dcr(monkeypatch):
    """The old behaviour was a dead link and a paragraph in the chat; now the
    same link shows the credentials form, and the sign-in continues from it."""
    import olano.core.mcp_setup as mcp_setup
    from olano.magic_links.adapters import mcp_oauth

    monkeypatch.setattr(
        mcp_setup, "poll_oauth_flow", lambda sid: _Session(error_code="dcr_unsupported")
    )
    monkeypatch.setattr(mcp_oauth, "composio_status", lambda agent: {"connected": False})
    flow = flows.get_flow("mcp-oauth")
    link = _with_session("mcp-oauth", agent="merli", url=HUBSPOT, needs_client=False)
    assert flow.state(link) == {"status": store.STATUS_PENDING}
    assert link.params["needs_client"] is True
    assert link.params["retry_reason"] == "rejected"
    assert link.params["retry_error"] == ""
    assert link.session_id is None  # a fresh session starts when they save
    desc = flow.descriptor(link)
    assert desc["mode"] == "credentials"
    assert "refused the automatic sign-in" in desc["instructions"]


def test_mcp_state_keeps_an_ordinary_failure_terminal_before_any_credentials(monkeypatch):
    import olano.core.mcp_setup as mcp_setup

    monkeypatch.setattr(mcp_setup, "poll_oauth_flow", lambda sid: _Session(error="boom"))
    link = _with_session("mcp-oauth", agent="merli", url=HUBSPOT, needs_client=False)
    out = flows.get_flow("mcp-oauth").state(link)
    assert out["status"] == store.STATUS_ERROR and out["error"] == "boom"


def test_mcp_state_returns_to_the_form_after_a_failed_credential_attempt(monkeypatch):
    """A wrong id/secret/redirect after the form is a retry, with the provider's words."""
    import olano.core.mcp_setup as mcp_setup
    from olano.magic_links.adapters import mcp_oauth

    monkeypatch.setattr(
        mcp_setup, "poll_oauth_flow", lambda sid: _Session(error="invalid_client: nope")
    )
    monkeypatch.setattr(mcp_oauth, "composio_status", lambda agent: {"connected": False})
    link = _with_session(
        "mcp-oauth", agent="merli", url=HUBSPOT, needs_client=False, client_submitted=True
    )
    assert flows.get_flow("mcp-oauth").state(link) == {"status": store.STATUS_PENDING}
    assert link.params["retry_reason"] == "failed"
    assert "invalid_client" in link.params["retry_error"]
    desc = flows.get_flow("mcp-oauth").descriptor(link)
    assert desc["retry_error"] == "invalid_client: nope"
    assert "did not go through" in desc["instructions"]


async def test_mcp_submit_validates_the_credentials():
    flow = flows.get_flow("mcp-oauth")
    with pytest.raises(ValueError, match="nothing to fill in"):
        await flow.submit(_record("mcp-oauth", agent="merli", url=HUBSPOT), {"client_id": "x"})
    link = _record("mcp-oauth", agent="merli", url=HUBSPOT, needs_client=True)
    with pytest.raises(ValueError, match="Client ID is required"):
        await flow.submit(link, {"client_id": "  "})
    with pytest.raises(ValueError, match="no spaces"):
        await flow.submit(link, {"client_id": "abc def"})
    with pytest.raises(ValueError, match="too long"):
        await flow.submit(link, {"client_id": "x" * 600})


async def test_mcp_submit_stores_the_client_on_the_agent_and_starts_the_sign_in(
    offline_directory, monkeypatch, base_config
):
    """The credentials land on the agent's server entry (never in the link
    record), and the sign-in starts with them — no second tap."""
    import olano.config.loader as loader
    import olano.core.mcp_setup as mcp_setup

    base_config.mcp_servers = []
    monkeypatch.setattr(
        loader,
        "mutate_agent_config",
        lambda name, apply, **kw: (apply(base_config), base_config)[1],
    )
    started: dict = {}

    def _start(agent, entry):
        started.update(agent=agent, entry=dict(entry))
        return _Session(status="starting", session_id="sess2")

    monkeypatch.setattr(mcp_setup, "start_mcp_authorization", _start)
    flow = flows.get_flow("mcp-oauth")
    link = _record("mcp-oauth", **flow.validate({"agent": "merli", "url": HUBSPOT}))
    out = await flow.submit(link, {"client_id": "cid-1", "client_secret": "sec-1"})
    assert out == {"status": store.STATUS_IN_PROGRESS}

    (entry,) = base_config.mcp_servers
    assert entry.url == HUBSPOT and entry.auth == "oauth"
    assert entry.transport == "http"  # the directory's transport for a new entry
    assert entry.oauth_client_id == "cid-1" and entry.oauth_client_secret == "sec-1"
    assert started["agent"] == "merli"
    assert started["entry"]["oauth_client_id"] == "cid-1"
    assert started["entry"]["oauth_client_secret"] == "sec-1"
    assert started["entry"]["auth"] == "oauth"
    # The link now runs the sign-in and never holds the secret.
    assert link.session_id == "sess2"
    assert link.params["needs_client"] is False and link.params["client_submitted"] is True
    assert link.params["oauth_client_id"] == "cid-1"
    assert "sec-1" not in str(link.params)


async def test_mcp_submit_updates_an_existing_entry_in_place(
    offline_directory, monkeypatch, base_config
):
    """An entry the user already tuned keeps its transport and headers."""
    import olano.config.loader as loader
    import olano.core.mcp_setup as mcp_setup
    from olano.config.agent_config import MCPServerConfig

    base_config.mcp_servers = [
        MCPServerConfig(url=HUBSPOT, transport="sse", headers={"X-Tenant": "acme"})
    ]
    monkeypatch.setattr(
        loader,
        "mutate_agent_config",
        lambda name, apply, **kw: (apply(base_config), base_config)[1],
    )
    monkeypatch.setattr(
        mcp_setup, "start_mcp_authorization", lambda a, e: _Session(status="starting")
    )
    flow = flows.get_flow("mcp-oauth")
    link = _record("mcp-oauth", **flow.validate({"agent": "merli", "url": HUBSPOT}))
    await flow.submit(link, {"client_id": "cid-2", "client_secret": ""})
    (entry,) = base_config.mcp_servers
    assert entry.transport == "sse" and entry.headers == {"X-Tenant": "acme"}
    assert entry.auth == "oauth" and entry.oauth_client_id == "cid-2"
    assert entry.oauth_client_secret is None  # a public, PKCE-only app


async def test_mcp_submit_keeps_the_form_when_the_sign_in_cannot_start(
    offline_directory, monkeypatch, base_config
):
    import olano.config.loader as loader
    import olano.core.mcp_setup as mcp_setup

    base_config.mcp_servers = []
    monkeypatch.setattr(
        loader,
        "mutate_agent_config",
        lambda name, apply, **kw: (apply(base_config), base_config)[1],
    )

    def _refuse(agent, entry):
        raise ValueError("No MCP OAuth redirect target configured.")

    monkeypatch.setattr(mcp_setup, "start_mcp_authorization", _refuse)
    flow = flows.get_flow("mcp-oauth")
    link = _record("mcp-oauth", **flow.validate({"agent": "merli", "url": HUBSPOT}))
    with pytest.raises(ValueError, match="redirect target"):
        await flow.submit(link, {"client_id": "cid-3"})
    # Saved for next time, and the form is still there to retry from.
    assert base_config.mcp_servers[0].oauth_client_id == "cid-3"
    assert link.params["needs_client"] is True and link.session_id is None


async def test_mcp_submit_vaults_the_secret_on_disk_and_signs_in_with_the_literal(
    offline_directory, monkeypatch
):
    """Through the real config write: the file gets a ${vault:…} reference and
    the vault the value, while the sign-in that starts right away still gets
    the literal — it never has to read the vault back."""
    import olano.core.mcp_setup as mcp_setup
    from olano import admin
    from olano.config.loader import load_agent_config

    class _Vault:
        def __init__(self):
            self.stored = {}

        def set(self, name, value, agent=None, **kw):
            self.stored[(name, agent)] = value

    vault = _Vault()
    monkeypatch.setattr("olano.config.channel_secrets._resolve_vault", lambda: vault)
    admin.create_agent("merli", model="fake:instant")
    started: dict = {}

    def _start(agent, entry):
        started.update(entry)
        return _Session(status="starting", session_id="sess3")

    monkeypatch.setattr(mcp_setup, "start_mcp_authorization", _start)
    flow = flows.get_flow("mcp-oauth")
    link = _record("mcp-oauth", **flow.validate({"agent": "merli", "url": HUBSPOT}))
    await flow.submit(link, {"client_id": "cid-9", "client_secret": "sec-9"})

    (entry,) = load_agent_config("merli").mcp_servers
    assert entry.oauth_client_id == "cid-9"
    assert entry.oauth_client_secret == "${vault:MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM}"
    assert vault.stored == {("MCP_OAUTH_CLIENT_SECRET_MCP_HUBSPOT_COM", "merli"): "sec-9"}
    assert started["oauth_client_secret"] == "sec-9"


async def test_mcp_submit_reports_a_config_write_failure_safely(offline_directory, monkeypatch):
    import olano.config.loader as loader

    def _explode(name, apply, **kw):
        raise RuntimeError("DeploymentLimitExceeded: MCP servers 3/3")

    monkeypatch.setattr(loader, "mutate_agent_config", _explode)
    flow = flows.get_flow("mcp-oauth")
    link = _record("mcp-oauth", **flow.validate({"agent": "merli", "url": HUBSPOT}))
    with pytest.raises(ValueError, match="Could not save the credentials to merli") as excinfo:
        await flow.submit(link, {"client_id": "cid-4"})
    assert "MCP servers 3/3" in str(excinfo.value)


def test_mcp_start_uses_the_configured_entry_and_refuses_before_credentials(monkeypatch):
    """A client id typed in the dashboard reaches the sign-in — the old flow
    built its entry from the link alone and hit DCR regardless."""
    import olano.core.mcp_setup as mcp_setup
    from olano.magic_links.adapters import mcp_oauth

    monkeypatch.setattr(
        mcp_oauth,
        "configured_entry",
        lambda agent, url: {
            "url": url,
            "transport": "sse",
            "headers": {"X-Tenant": "acme"},
            "oauth_client_id": "dash-id",
            "oauth_client_secret": "dash-secret",
        },
    )
    started: dict = {}

    def _start(agent, entry):
        started.update(entry)
        return _Session(status="starting", session_id="s9")

    monkeypatch.setattr(mcp_setup, "start_mcp_authorization", _start)
    flow = flows.get_flow("mcp-oauth")
    link = _record("mcp-oauth", agent="merli", url=HUBSPOT, needs_client=False)
    assert flow.start(link) == {"session_id": "s9"}
    assert started["oauth_client_id"] == "dash-id"
    assert started["oauth_client_secret"] == "dash-secret"
    assert started["transport"] == "sse" and started["headers"] == {"X-Tenant": "acme"}
    assert started["auth"] == "oauth"

    with pytest.raises(ValueError, match="Client ID and Client secret first"):
        flow.start(_record("mcp-oauth", agent="merli", url=HUBSPOT, needs_client=True))


async def test_start_refusal_reaches_the_page_as_a_magic_link_error(offline_directory):
    """`act_on_link("start")` on a link waiting for credentials is a 400 with
    the reason, not a 500."""
    minted = service.mint_link(
        "mcp-oauth", params={"agent": "merli", "url": HUBSPOT}, requested_by={}, notify={}
    )
    record = service.resolve_token(minted.url.rsplit("/", 1)[-1])
    with pytest.raises(service.MagicLinkError, match="Client ID and Client secret first"):
        await service.act_on_link(record, "start")


# ── secret-entry ─────────────────────────────────────────────────────────────


def test_secret_entry_normalizes_and_validates_the_name():
    flow = flows.get_flow("secret-entry")
    assert flow.validate({"name": "stripe_api_key"})["name"] == "STRIPE_API_KEY"
    with pytest.raises(ValueError, match="name is required"):
        flow.validate({})
    with pytest.raises(ValueError, match="valid credential name"):
        flow.validate({"name": "not a valid name!"})


def test_secret_entry_refuses_when_the_vault_is_locked(monkeypatch):
    import olano.magic_links.adapters.secret_entry as se

    monkeypatch.setattr(se, "unlocked_vault", lambda: None)
    flow = flows.get_flow("secret-entry")
    with pytest.raises(ValueError, match="vault is locked"):
        flow.submit(_record("secret-entry", name="STRIPE_API_KEY"), {"value": "sk-x"})


def test_secret_entry_writes_to_the_vault_at_the_requested_scope(monkeypatch):
    import olano.magic_links.adapters.secret_entry as se

    written = {}

    class _Vault:
        def set(self, **kwargs):
            written.update(kwargs)

    monkeypatch.setattr(se, "unlocked_vault", lambda: _Vault())
    flow = flows.get_flow("secret-entry")
    out = flow.submit(
        _record("secret-entry", name="STRIPE_API_KEY", agent="merli"), {"value": "sk-live-x"}
    )
    assert out["status"] == store.STATUS_DONE
    assert written["name"] == "STRIPE_API_KEY"
    assert written["value"] == "sk-live-x"
    assert written["agent"] == "merli"
    # The confirmation names the credential but never carries its value.
    assert "sk-live-x" not in out["result"]


def test_secret_entry_rejects_an_empty_value(monkeypatch):
    import olano.magic_links.adapters.secret_entry as se

    monkeypatch.setattr(se, "unlocked_vault", lambda: object())
    with pytest.raises(ValueError, match="can't be empty"):
        flows.get_flow("secret-entry").submit(_record("secret-entry", name="K"), {"value": "  "})


# ── provider-key ─────────────────────────────────────────────────────────────


def test_provider_key_rejects_providers_that_take_no_api_key():
    flow = flows.get_flow("provider-key")
    for excluded in ("ollama", "openai-codex"):
        with pytest.raises(ValueError, match="Unknown model provider"):
            flow.validate({"provider": excluded})
    assert flow.validate({"provider": "anthropic"})["provider"] == "anthropic"


def test_provider_key_stores_under_the_provider_env_var(monkeypatch):
    import olano.magic_links.adapters.secret_entry as se

    written = {}

    class _Vault:
        def set(self, **kwargs):
            written.update(kwargs)

    monkeypatch.setattr(se, "unlocked_vault", lambda: _Vault())
    flow = flows.get_flow("provider-key")
    out = flow.submit(_record("provider-key", provider="anthropic"), {"value": "sk-ant-x"})
    assert written["name"] == "ANTHROPIC_API_KEY"
    assert out["status"] == store.STATUS_DONE
    assert "ANTHROPIC_API_KEY" in out["result"]


def test_provider_key_picker_lists_only_api_key_providers():
    flow = flows.get_flow("provider-key")
    descriptor = flow.descriptor(_record("provider-key"))
    options = {o["value"] for f in descriptor["fields"] for o in f["options"]}
    assert "anthropic" in options
    assert "ollama" not in options and "openai-codex" not in options


def test_provider_key_honours_the_byok_entitlement(monkeypatch):
    from olano.deployment import enforcement

    monkeypatch.setattr(enforcement, "feature_enabled", lambda name: name != "model_selection")
    with pytest.raises(ValueError, match="not included in this deployment's plan"):
        flows.get_flow("provider-key").validate({"provider": "anthropic"})


def test_provider_key_honours_the_plan_byok_flag(monkeypatch):
    # `byok` off with model_selection on (Basic/Pro managed plans) blocks too.
    from olano.deployment import enforcement

    monkeypatch.setattr(enforcement, "feature_enabled", lambda name: name != "byok")
    with pytest.raises(ValueError, match="Ultra plan and above"):
        flows.get_flow("provider-key").validate({"provider": "anthropic"})


# ── registry ─────────────────────────────────────────────────────────────────


def test_every_adapter_declares_a_usable_identity():
    for flow in flows.all_flows():
        assert flow.id and flow.label
        assert flow.kind in ("device_code", "redirect", "form", "qr", "session", "connect", "mcp")


def test_mint_records_the_flow_and_scope():
    minted = service.mint_link(
        "github-device", params={}, requested_by={}, notify={}, minted_by="test"
    )
    record = service.resolve_token(minted.url.rsplit("/", 1)[-1])
    assert record.flow == "github-device"
    assert minted.label == "GitHub"


# ── edit-prompt ──────────────────────────────────────────────────────────────


class _FakeConfig:
    def __init__(self, system_prompt: str = "Be helpful."):
        self.system_prompt = system_prompt


def test_edit_prompt_requires_a_real_agent(monkeypatch):
    import olano.config.loader as loader

    monkeypatch.setattr(loader, "load_agent_config", lambda name: _FakeConfig())
    flow = flows.get_flow("edit-prompt")
    assert flow.validate({"agent": "merli"})["agent"] == "merli"
    with pytest.raises(ValueError, match="agent name is required"):
        flow.validate({})

    def _boom(name):
        raise FileNotFoundError("nope")

    monkeypatch.setattr(loader, "load_agent_config", _boom)
    with pytest.raises(ValueError, match="not found"):
        flow.validate({"agent": "ghost"})


def test_edit_prompt_rejects_unknown_targets(monkeypatch):
    import olano.config.loader as loader

    monkeypatch.setattr(loader, "load_agent_config", lambda name: _FakeConfig())
    with pytest.raises(ValueError, match="Unknown edit target"):
        flows.get_flow("edit-prompt").validate({"agent": "merli", "target": "SOUL.md"})


def test_edit_prompt_descriptor_prefills_the_current_prompt(monkeypatch):
    import olano.config.loader as loader

    monkeypatch.setattr(loader, "load_agent_config", lambda name: _FakeConfig("Be very concise."))
    desc = flows.get_flow("edit-prompt").descriptor(
        _record("edit-prompt", agent="merli", target="system_prompt")
    )
    assert desc["kind"] == "form"
    assert "merli" in desc["title"]
    (field,) = desc["fields"]
    assert field["multiline"] is True
    assert field["value"] == "Be very concise."


def test_edit_prompt_refuses_empty_and_oversized(monkeypatch):
    from olano.magic_links.adapters import edit_prompt as ep

    flow = flows.get_flow("edit-prompt")
    link = _record("edit-prompt", agent="merli", target="system_prompt")
    with pytest.raises(ValueError, match="can't be empty"):
        flow.submit(link, {"value": "   "})
    with pytest.raises(ValueError, match="too long"):
        flow.submit(link, {"value": "x" * (ep.MAX_PROMPT_CHARS + 1)})


def test_edit_prompt_saves_through_the_validated_admin_path(monkeypatch):
    import olano.admin.agents as admin_agents

    saved: dict = {}

    def fake_update(name, updates):
        saved["name"] = name
        saved["updates"] = updates
        return {"name": name}

    monkeypatch.setattr(admin_agents, "update_agent", fake_update)
    out = flows.get_flow("edit-prompt").submit(
        _record("edit-prompt", agent="merli", target="system_prompt"),
        {"value": "  New standing instructions.  "},
    )
    assert out["status"] == store.STATUS_DONE
    assert saved == {"name": "merli", "updates": {"system_prompt": "New standing instructions."}}


def test_edit_prompt_save_failure_is_a_safe_page_error(monkeypatch):
    import olano.admin.agents as admin_agents

    def _explode(name, updates):
        raise RuntimeError("disk on fire: /secret/path")

    monkeypatch.setattr(admin_agents, "update_agent", _explode)
    with pytest.raises(ValueError) as excinfo:
        flows.get_flow("edit-prompt").submit(
            _record("edit-prompt", agent="merli", target="system_prompt"),
            {"value": "New instructions."},
        )
    # The page-facing message never leaks the underlying exception.
    assert "disk on fire" not in str(excinfo.value)


# ── edit-file ────────────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A real agent workspace, wired through the admin file helpers."""
    import olano.admin.workspace_files as wf

    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(wf, "_workspace_root", lambda agent: root)
    # The sandbox chown is a no-op outside a container and needs no root here.
    import olano.core.builder as builder

    monkeypatch.setattr(builder, "chown_for_sandbox", lambda *a, **k: None)
    return root


@pytest.fixture(autouse=True)
def _clear_edit_file_snapshots():
    from olano.magic_links.adapters import edit_file

    edit_file._SNAPSHOT.clear()
    yield
    edit_file._SNAPSHOT.clear()


@pytest.fixture
def as_agent(monkeypatch):
    """Bind a real minting identity on the ContextVar the runner binds.

    Set on the actual ContextVar rather than patched: the privilege gate reads
    it, and a stub would test the stub.
    """
    from olano.tools import talk_to

    tokens = []

    def _bind(name: str, tools: list[str] | None = None) -> None:
        tokens.append(talk_to.current_agent.set(name))
        if tools is not None:
            import olano.config.loader as loader

            class _Cfg:
                def __init__(self):
                    self.tools = list(tools)

            monkeypatch.setattr(loader, "load_agent_config", lambda n: _Cfg())

    yield _bind
    for token in reversed(tokens):
        talk_to.current_agent.reset(token)


def test_edit_file_requires_a_path():
    with pytest.raises(ValueError, match="file path is required"):
        flows.get_flow("edit-file").validate({"agent": "merli"})


def test_edit_file_validates_a_workspace_path(workspace):
    (workspace / "notes.md").write_text("hello", encoding="utf-8")
    params = flows.get_flow("edit-file").validate({"agent": "merli", "path": "notes.md"})
    assert params == {
        "agent": "merli",
        "path": "notes.md",
        "scope": "workspace",
        "service": "edit-file",
    }


def test_edit_file_refuses_an_escape_from_the_workspace(workspace, tmp_path):
    (tmp_path / "outside.txt").write_text("secret", encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        flows.get_flow("edit-file").validate({"agent": "merli", "path": "../outside.txt"})


def test_edit_file_refuses_directories_binaries_and_giants(workspace):
    (workspace / "sub").mkdir()
    (workspace / "blob.bin").write_bytes(b"PK\x03\x04\x00binary")
    (workspace / "huge.txt").write_text("x" * (256 * 1024 + 1), encoding="utf-8")
    flow = flows.get_flow("edit-file")

    with pytest.raises(ValueError, match="directory"):
        flow.validate({"agent": "merli", "path": "sub"})
    with pytest.raises(ValueError, match="binary"):
        flow.validate({"agent": "merli", "path": "blob.bin"})
    with pytest.raises(ValueError, match="larger than"):
        flow.validate({"agent": "merli", "path": "huge.txt"})
    with pytest.raises(ValueError, match="does not exist"):
        flow.validate({"agent": "merli", "path": "ghost.txt"})


def test_edit_file_descriptor_prefills_and_survives_a_poll(workspace):
    """The textarea holds the file as it was when the page opened.

    link_state re-runs descriptor() on every 2.5s poll; re-reading the file
    there would wipe a half-typed edit the moment anything else wrote to it.
    """
    target = workspace / "config.yaml"
    target.write_text("mode: fast\n", encoding="utf-8")
    record = _record("edit-file", agent="merli", path="config.yaml", scope="workspace")

    first = flows.get_flow("edit-file").descriptor(record)
    assert first["fields"][0]["value"] == "mode: fast\n"
    assert first["title"] == "Edit config.yaml"

    target.write_text("mode: slow\n", encoding="utf-8")  # something else writes
    second = flows.get_flow("edit-file").descriptor(record)
    assert second["fields"][0]["value"] == "mode: fast\n"


def test_edit_file_unreadable_file_shows_an_empty_box(workspace):
    record = _record("edit-file", agent="merli", path="gone.yaml", scope="workspace")
    desc = flows.get_flow("edit-file").descriptor(record)
    assert desc["fields"][0]["value"] == ""


def test_edit_file_saves_and_reports_the_path_without_the_contents(workspace):
    (workspace / "app.env").write_text("KEY=old\n", encoding="utf-8")
    out = flows.get_flow("edit-file").submit(
        _record("edit-file", agent="merli", path="app.env", scope="workspace"),
        {"value": "KEY=new-and-secret\n"},
    )
    assert out["status"] == store.STATUS_DONE
    assert (workspace / "app.env").read_text(encoding="utf-8") == "KEY=new-and-secret\n"
    assert "app.env" in out["result"]
    # The result string is posted verbatim into the chat.
    assert "new-and-secret" not in out["result"]


def test_edit_file_allows_saving_an_empty_file(workspace):
    (workspace / "notes.md").write_text("something", encoding="utf-8")
    out = flows.get_flow("edit-file").submit(
        _record("edit-file", agent="merli", path="notes.md", scope="workspace"),
        {"value": ""},
    )
    assert out["status"] == store.STATUS_DONE
    assert (workspace / "notes.md").read_text(encoding="utf-8") == ""


def test_edit_file_rechecks_containment_at_save_time(workspace, tmp_path):
    """A path validated at mint can be a symlink out by the time it is saved."""
    outside = tmp_path / "escaped.txt"
    outside.write_text("original", encoding="utf-8")
    (workspace / "notes.md").write_text("fine", encoding="utf-8")
    (workspace / "notes.md").unlink()
    (workspace / "notes.md").symlink_to(outside)

    with pytest.raises(ValueError, match="no longer inside"):
        flows.get_flow("edit-file").submit(
            _record("edit-file", agent="merli", path="notes.md", scope="workspace"),
            {"value": "overwritten"},
        )
    assert outside.read_text(encoding="utf-8") == "original"


def test_edit_file_save_failure_is_a_safe_page_error(workspace, monkeypatch):
    import olano.admin.workspace_files as wf

    def _explode(agent, path, content):
        raise RuntimeError("disk on fire: /secret/path")

    monkeypatch.setattr(wf, "write_agent_workspace_file", _explode)
    (workspace / "notes.md").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        flows.get_flow("edit-file").submit(
            _record("edit-file", agent="merli", path="notes.md", scope="workspace"),
            {"value": "y"},
        )
    assert "disk on fire" not in str(excinfo.value)


def test_edit_file_snapshot_cache_is_bounded(workspace):
    """A link that simply expires runs neither submit nor cancel.

    Without a bound the cache would hold a copy of every file ever opened
    through a link nobody finished.
    """
    from olano.magic_links.adapters import edit_file

    (workspace / "notes.md").write_text("x" * 100, encoding="utf-8")
    flow = flows.get_flow("edit-file")
    for i in range(edit_file._SNAPSHOT_MAX + 10):
        record = LinkRecord(
            id=f"hs_{i}",
            token_hash="x",
            flow="edit-file",
            params={"agent": "merli", "path": "notes.md", "scope": "workspace"},
        )
        flow.descriptor(record)
    assert len(edit_file._SNAPSHOT) <= edit_file._SNAPSHOT_MAX


# ── edit-file: the host-path privilege gate ─────────────────────────────────


def test_edit_file_host_path_refused_for_a_plain_agent(as_agent, tmp_path):
    target = tmp_path / "olano.env"
    target.write_text("TOKEN=abc\n", encoding="utf-8")
    as_agent("merli", tools=["read_file", "write_file"])
    with pytest.raises(ValueError, match="admin agent"):
        flows.get_flow("edit-file").validate({"agent": "merli", "path": str(target)})


def test_edit_file_host_path_allowed_for_an_admin_agent(as_agent, tmp_path):
    target = tmp_path / "olano.env"
    target.write_text("TOKEN=abc\n", encoding="utf-8")
    as_agent("agentfather", tools=["olano_write_agent_file"])
    params = flows.get_flow("edit-file").validate({"path": str(target)})
    assert params["scope"] == "host"
    assert params["path"] == str(target.resolve())


def test_edit_file_host_path_allowed_for_the_operator(as_agent, tmp_path):
    """No bound agent means the dashboard, the CLI, or /olano connect.

    Each of those is already admin- or owner-gated at its own entry point.
    """
    target = tmp_path / "config.yaml"
    target.write_text("a: 1\n", encoding="utf-8")
    as_agent("")
    assert flows.get_flow("edit-file").validate({"path": str(target)})["scope"] == "host"


def test_edit_file_ignores_a_scope_supplied_by_the_caller(workspace, as_agent):
    """The dashboard forwards request params verbatim — recompute, never trust."""
    (workspace / "notes.md").write_text("hi", encoding="utf-8")
    as_agent("merli", tools=[])
    params = flows.get_flow("edit-file").validate(
        {"agent": "merli", "path": "notes.md", "scope": "host"}
    )
    assert params["scope"] == "workspace"


def test_edit_file_host_save_does_not_chown_to_the_sandbox(monkeypatch, tmp_path):
    """A host path belongs to the runtime; handing it to the sandbox uid would
    be a permission change nobody asked for."""
    import olano.core.builder as builder

    calls: list = []
    monkeypatch.setattr(builder, "chown_for_sandbox", lambda p: calls.append(p))
    target = tmp_path / "olano.env"
    target.write_text("TOKEN=abc\n", encoding="utf-8")

    out = flows.get_flow("edit-file").submit(
        _record("edit-file", agent="agentfather", path=str(target), scope="host"),
        {"value": "TOKEN=xyz\n"},
    )
    assert out["status"] == store.STATUS_DONE
    assert target.read_text(encoding="utf-8") == "TOKEN=xyz\n"
    assert calls == []


def test_flow_field_never_prefills_secrets():
    from olano.magic_links.flows import FlowField

    assert FlowField(name="v", label="V", secret=True, value="sk-123").public()["value"] == ""
    assert FlowField(name="v", label="V", multiline=True, value="text").public()["value"] == "text"
