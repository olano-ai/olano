"""Broker-machinery tests (``olano/core/connectors/base.py``).

Everything runs against THROWAWAY providers (unique ``testsvc*`` ids) so no
real provider definition is exercised here — that is ``test_providers.py``'s
job. Registration is idempotent and the registry has no unregister, so the
autouse fixture snapshots and restores the registry's private state to keep
the global provider list pristine for other test files.

Network is stubbed at the broker's single HTTP seam (``base._post_token``);
token files are isolated under the autouse tmp ``LIONCLAW_HOME``.
"""

from __future__ import annotations

import base64 as b64
import hashlib
import json
import time
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from olano.config.home import get_olano_paths
from olano.core.connectors import base, registry
from olano.core.connectors.base import (
    ConnectorProvider,
    auth_methods,
    connector_auth_status,
    disconnect_connector,
    get_live_record,
    is_connector_shared,
    resolve_connector_callback,
    set_connector_shared,
    start_connector_auth,
)

CREDS = {"client_id": "cid", "client_secret": "cs"}
REDIRECT = "https://host.example/api/auth/testsvc/callback"


@pytest.fixture(autouse=True)
def _isolate_broker_state():
    """Clear flow/session/cache state and restore the global registry.

    The registry has no unregister — restoring the snapshot keeps in-test
    ``testsvc*`` registrations from leaking into ``test_providers.py``'s
    exact-registration assertions (and into the credential resolver's
    exposed-name index) for the rest of the pytest session.
    """
    from olano.vault import resolver

    saved_providers = dict(registry._PROVIDERS)
    saved_exposed = dict(registry._EXPOSED)
    saved_names = registry._EXPOSED_NAMES
    resolver.set_vault(None)
    base._SESSIONS.clear()
    base._BY_STATE.clear()
    base._RECORD_CACHE.clear()
    yield
    base._SESSIONS.clear()
    base._BY_STATE.clear()
    base._RECORD_CACHE.clear()
    registry._PROVIDERS.clear()
    registry._PROVIDERS.update(saved_providers)
    registry._EXPOSED.clear()
    registry._EXPOSED.update(saved_exposed)
    registry._EXPOSED_NAMES = saved_names
    resolver.set_vault(None)


def _provider(pid: str, **overrides: Any) -> ConnectorProvider:
    spec: dict[str, Any] = {
        "id": pid,
        "name": "Test Service",
        "authorize_url": "https://auth.testsvc.example/authorize",
        "token_url": "https://auth.testsvc.example/token",
        "scopes": ("read", "write"),
        "creds_vault_key": "TESTSVC_OAUTH_CLIENT",
    }
    spec.update(overrides)
    return ConnectorProvider(**spec)


def _start(provider: ConnectorProvider, **kw: Any):
    return start_connector_auth(
        provider,
        creds=kw.get("creds", dict(CREDS)),
        agent=kw.get("agent"),
        shared=kw.get("shared", False),
        redirect_uri=REDIRECT,
    )


def _fake_post(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | None = None):
    """Install a recording stub at the broker's token-endpoint seam."""
    calls: list[dict[str, Any]] = []

    def fake(url: str, data: dict[str, str], *, headers=None, auth=None) -> dict[str, Any]:
        calls.append({"url": url, "data": dict(data), "headers": headers, "auth": auth})
        if payload is None:
            raise RuntimeError("token endpoint down")
        return dict(payload)

    monkeypatch.setattr(base, "_post_token", fake)
    return calls


def _forbid_post(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **kw: Any) -> dict[str, Any]:
        raise AssertionError("_post_token must not be called")

    monkeypatch.setattr(base, "_post_token", boom)


def _write_record(path, **fields: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "access_token": "at-old",
        "token_type": "Bearer",
        "expires_at": None,
        "client_id": "cid",
        "client_secret": "cs",
        "params": {},
        "extras": {},
        "obtained_at": time.time(),
    }
    record.update(fields)
    base._save_record(record, path)
    return record


# ── Official-app (relay-mode) helpers ──────────────────────────────────────

RELAY_CFG_URL = "https://relay.example"


def _relay_cfg():
    from olano.core.mcp_relay import RelayConfig

    return RelayConfig(url=RELAY_CFG_URL, secret="shared", instance_id="inst-1")


def _official(monkeypatch: pytest.MonkeyPatch, client_id: str = "off-cid") -> list[dict[str, Any]]:
    """Make the official floor available at every seam the broker consults.

    Patches the catalog lookup, the relay config, the state registration (a
    recording stub — returned), and the pump (no-op: pump behaviour has its
    own direct tests, and start() must not leak polling threads here).
    """
    register_calls: list[dict[str, Any]] = []

    def fake_register(cfg, state, *, provider, code_verifier, params, timeout=10.0):
        register_calls.append(
            {"state": state, "provider": provider, "code_verifier": code_verifier, "params": params}
        )

    monkeypatch.setattr("olano.catalog.connectors_registry.official_client", lambda pid: client_id)
    monkeypatch.setattr(
        "olano.core.connect_relay.resolve_connect_relay_config", lambda: _relay_cfg()
    )
    monkeypatch.setattr("olano.core.connect_relay.register_connect_state", fake_register)
    monkeypatch.setattr(base, "_relay_pump", lambda provider, session: None)
    return register_calls


# ── start_connector_auth: URL building ─────────────────────────────────────


def test_start_builds_authorization_url_with_scope_join_state_and_extras():
    provider = _provider(
        "testsvc-url",
        scopes=("read", "write"),
        scope_separator=",",
        extra_auth_params={"duration": "permanent"},
    )
    session = _start(provider)

    url = session.authorization_url
    assert url is not None and url.startswith("https://auth.testsvc.example/authorize?")
    query = dict(parse_qsl(urlsplit(url).query))
    assert query["response_type"] == "code"
    assert query["client_id"] == "cid"
    assert query["redirect_uri"] == REDIRECT
    assert query["scope"] == "read,write"  # custom separator join
    assert query["duration"] == "permanent"  # extra_auth_params
    assert query["state"] and query["state"] == session.state
    # Not a PKCE provider → no challenge in the URL, no verifier on the session.
    assert "code_challenge" not in query
    assert session.code_verifier is None
    assert session.status == "awaiting_authorization"


def test_start_pkce_challenge_is_s256_of_verifier():
    provider = _provider("testsvc-pkce", pkce=True)
    session = _start(provider)

    assert session.authorization_url is not None
    query = dict(parse_qsl(urlsplit(session.authorization_url).query))
    assert query["code_challenge_method"] == "S256"
    assert session.code_verifier
    expected = (
        b64.urlsafe_b64encode(hashlib.sha256(session.code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert query["code_challenge"] == expected
    assert not query["code_challenge"].endswith("=")  # padding stripped


def test_start_missing_required_param_raises():
    provider = _provider("testsvc-req", required_params=("shop",))
    with pytest.raises(ValueError, match="missing required field.*shop"):
        _start(provider)


def test_start_rejects_hostile_param_value():
    provider = _provider("testsvc-charset", required_params=("shop",))
    with pytest.raises(ValueError, match="invalid value for 'shop'"):
        _start(provider, creds={**CREDS, "shop": "bad/../value"})


def test_start_applies_normalize_params():
    def normalize(params: dict[str, str]) -> dict[str, str]:
        out = dict(params)
        if "shop" in out:
            out["shop"] = out["shop"].removesuffix(".myshopify.com")
        return out

    provider = _provider(
        "testsvc-norm",
        authorize_url="https://{shop}.myshopify.example/authorize",
        token_url="https://{shop}.myshopify.example/token",
        required_params=("shop",),
        normalize_params=normalize,
    )
    session = _start(provider, creds={**CREDS, "shop": "acme.myshopify.com"})
    assert session.params["shop"] == "acme"
    assert session.authorization_url is not None
    assert session.authorization_url.startswith("https://acme.myshopify.example/authorize?")


# ── Client-credential resolution order ─────────────────────────────────────


def test_client_creds_explicit_body_beats_vault_blob_beats_env_pair(monkeypatch):
    provider = _provider(
        "testsvc-creds",
        creds_vault_key="TESTSVC_CREDS_OAUTH_CLIENT",
        client_env_pair=("TESTSVC_CREDS_CLIENT_ID", "TESTSVC_CREDS_CLIENT_SECRET"),
    )
    # get_credential falls through to os.environ for both fallback layers.
    monkeypatch.setenv(
        "TESTSVC_CREDS_OAUTH_CLIENT",
        json.dumps({"client_id": "blob-id", "client_secret": "blob-sec"}),
    )
    monkeypatch.setenv("TESTSVC_CREDS_CLIENT_ID", "pair-id")
    monkeypatch.setenv("TESTSVC_CREDS_CLIENT_SECRET", "pair-sec")

    # Explicit request-body creds win over everything.
    explicit = _start(provider, creds={"client_id": "body-id", "client_secret": "body-sec"})
    assert (explicit.client_id, explicit.client_secret) == ("body-id", "body-sec")

    # No body → the vault/env JSON blob wins over the credential pair.
    blob = _start(provider, creds=None)
    assert (blob.client_id, blob.client_secret) == ("blob-id", "blob-sec")

    # No blob either → the client_env_pair names are resolved.
    monkeypatch.delenv("TESTSVC_CREDS_OAUTH_CLIENT")
    pair = _start(provider, creds=None)
    assert (pair.client_id, pair.client_secret) == ("pair-id", "pair-sec")

    # Nothing anywhere → actionable error.
    monkeypatch.delenv("TESTSVC_CREDS_CLIENT_ID")
    monkeypatch.delenv("TESTSVC_CREDS_CLIENT_SECRET")
    with pytest.raises(ValueError, match="no OAuth client configured"):
        _start(provider, creds=None)

    # The official floor sits BELOW every BYO tier: with an official client
    # available it fills the gap, but any BYO client still beats it.
    _official(monkeypatch, "off-cid")
    official = _start(provider, creds=None)
    assert (official.client_id, official.client_secret) == ("off-cid", "")
    assert official.client_source == "official" and official.auth_mode == "relay"

    monkeypatch.setenv("TESTSVC_CREDS_CLIENT_ID", "pair-id")
    monkeypatch.setenv("TESTSVC_CREDS_CLIENT_SECRET", "pair-sec")
    byo = _start(provider, creds=None)
    assert byo.client_id == "pair-id"
    assert byo.client_source == "byo" and byo.auth_mode == "direct"


# ── Callback exchange ──────────────────────────────────────────────────────


def test_callback_exchange_writes_scoped_record_and_marks_shared(monkeypatch, lionclaw_home):
    hook_ran: list[str] = []

    def hook(provider: ConnectorProvider, record: dict[str, Any]) -> None:
        hook_ran.append(provider.id)
        record["extras"]["post_exchange"] = "ran"

    provider = _provider(
        "testsvc-cb",
        callback_extras=("realmId",),
        token_extras=("instance_url",),
        post_exchange=hook,
    )
    session = _start(provider, agent="a1", shared=True)
    calls = _fake_post(
        monkeypatch,
        {
            "access_token": "at-1",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "rt-1",
            "instance_url": "https://inst.example",
        },
    )

    result = resolve_connector_callback(
        provider, session.state, "code-1", None, {"state": session.state, "realmId": "realm-9"}
    )
    assert result is session
    assert session.status == "authorized"

    # Exchange request shape (client_auth="post" default).
    assert len(calls) == 1
    assert calls[0]["url"] == "https://auth.testsvc.example/token"
    assert calls[0]["auth"] is None
    data = calls[0]["data"]
    assert data["grant_type"] == "authorization_code"
    assert data["code"] == "code-1"
    assert data["redirect_uri"] == REDIRECT
    assert data["client_id"] == "cid"
    assert data["client_secret"] == "cs"

    # Record written at the per-agent path under LIONCLAW_HOME, mode 0600.
    paths = get_olano_paths()
    token_path = paths.agent_connector_token_path("a1", "testsvc-cb")
    assert session.token_path == token_path
    assert token_path.exists()
    assert token_path.is_relative_to(lionclaw_home)
    assert token_path.stat().st_mode & 0o777 == 0o600

    record = json.loads(token_path.read_text(encoding="utf-8"))
    assert record["access_token"] == "at-1"
    assert record["refresh_token"] == "rt-1"
    assert record["expires_at"] == pytest.approx(time.time() + 3600, abs=30)
    assert record["extras"]["realmId"] == "realm-9"  # from the callback query
    assert record["extras"]["instance_url"] == "https://inst.example"  # from the payload
    assert record["extras"]["post_exchange"] == "ran"
    assert hook_ran == ["testsvc-cb"]

    # agent + shared → the shared flag lands in the agent's connections.json.
    assert is_connector_shared("testsvc-cb", "a1") is True
    assert paths.agent_connections_meta_path("a1").exists()


def test_callback_error_marks_session_error(monkeypatch):
    provider = _provider("testsvc-err")
    session = _start(provider)
    _forbid_post(monkeypatch)

    result = resolve_connector_callback(provider, session.state, None, "access_denied")
    assert result is session
    assert session.status == "error"
    assert session.error == "access_denied"
    assert not session.token_path.exists()


def test_callback_unknown_state_returns_none(monkeypatch):
    provider = _provider("testsvc-unknown")
    _start(provider)
    _forbid_post(monkeypatch)

    assert resolve_connector_callback(provider, "never-issued", "code-1") is None
    assert resolve_connector_callback(provider, None, "code-1") is None


def test_callback_replay_never_reexchanges(monkeypatch):
    provider = _provider("testsvc-replay")
    session = _start(provider)
    state = session.state
    assert state is not None
    calls = _fake_post(monkeypatch, {"access_token": "at-1", "expires_in": 3600})

    first = resolve_connector_callback(provider, state, "code-1", None, {})
    assert first is session and first.status == "authorized"
    assert len(calls) == 1

    # The state is single-use: a replayed redirect must never re-exchange.
    second = resolve_connector_callback(provider, state, "code-1", None, {})
    assert len(calls) == 1
    assert second is None or second is first

    # A duplicate delivery that still finds the state mapping (racing requests)
    # gets the same terminal session back — again without exchanging.
    base._BY_STATE[state] = session.session_id
    third = resolve_connector_callback(provider, state, "code-1", None, {})
    assert third is first
    assert third is not None and third.status == "authorized"
    assert len(calls) == 1


# ── Official-app relay mode ────────────────────────────────────────────────


def test_relay_start_overrides_redirect_and_registers_filtered_params(monkeypatch):
    from urllib.parse import parse_qsl, urlsplit

    provider = _provider(
        "testsvc-relay",
        pkce=True,
        token_url="https://auth.testsvc.example/{tenant}/token",
        optional_params={"tenant": "common", "environment": "production"},
    )
    calls = _official(monkeypatch)

    session = _start(provider, creds=None)
    assert session.auth_mode == "relay" and session.client_source == "official"
    assert (session.client_id, session.client_secret) == ("off-cid", "")
    # The passed redirect_uri (the box's) is overridden with the relay's —
    # the one URI registered on the official app.
    assert session.redirect_uri == f"{RELAY_CFG_URL}/connect/callback"
    query = dict(parse_qsl(urlsplit(session.authorization_url).query))
    assert query["redirect_uri"] == f"{RELAY_CFG_URL}/connect/callback"
    assert query["client_id"] == "off-cid"
    assert query["code_challenge_method"] == "S256"

    # Registered before the URL was handed out, with ONLY the token-URL
    # template params — box-only params (environment) must stay home, since
    # the relay refuses unknown keys by design.
    assert len(calls) == 1
    assert calls[0]["state"] == session.state
    assert calls[0]["provider"] == provider.id
    assert calls[0]["code_verifier"] == session.code_verifier
    assert calls[0]["params"] == {"tenant": "common"}


def test_relay_start_register_failure_is_actionable(monkeypatch):
    provider = _provider("testsvc-relay-down")
    _official(monkeypatch)

    def refuse(cfg, state, **kw):
        raise RuntimeError("auth relay refused the authorization (503): relay busy")

    monkeypatch.setattr("olano.core.connect_relay.register_connect_state", refuse)
    with pytest.raises(ValueError, match="use your own OAuth app"):
        _start(provider, creds=None)
    # A refused registration leaves no half-session behind.
    assert not base._SESSIONS


def test_relay_body_creds_still_run_the_direct_flow(monkeypatch):
    # Official available, but the user typed a client → BYO direct, untouched.
    provider = _provider("testsvc-relay-byo")
    calls = _official(monkeypatch)
    session = _start(provider, creds=dict(CREDS))
    assert session.auth_mode == "direct" and session.client_source == "byo"
    assert session.redirect_uri == REDIRECT
    assert calls == []


def test_relay_capable_excludes_custom_exchanges():
    assert base.relay_capable(_provider("testsvc-cap")) is True
    assert base.relay_capable(_provider("testsvc-get", token_method="get")) is False
    custom = _provider(
        "testsvc-custom", token_request=lambda p, s, c: ("https://x", {}, None, None)
    )
    assert base.relay_capable(custom) is False


def test_relay_pump_finalizes_like_the_on_box_callback(monkeypatch, lionclaw_home):
    """The bundle path must run callback_extras/post_exchange/save identically."""
    hook_ran: list[str] = []

    def hook(provider: ConnectorProvider, record: dict[str, Any]) -> None:
        hook_ran.append(provider.id)
        record["extras"]["post_exchange"] = "ran"

    provider = _provider(
        "testsvc-pump",
        callback_extras=("realmId",),
        token_extras=("instance_url",),
        post_exchange=hook,
    )
    real_pump = base._relay_pump  # _official() stubs the attr for start()
    calls = _official(monkeypatch)
    session = _start(provider, agent="a1", shared=True, creds=None)
    assert calls  # registered
    _forbid_post(monkeypatch)  # the exchange ran relay-side — never here

    def fake_poll(cfg, state, *, timeout=10.0):
        assert state == session.state
        return {
            "status": "ready",
            "bundle": {
                "provider": provider.id,
                "token_response": {
                    "access_token": "at-relay",
                    "refresh_token": "rt-relay",
                    "expires_in": 3600,
                    "instance_url": "https://inst.example",
                },
                "callback_query": {"state": session.state, "realmId": "realm-9"},
                "obtained_at": time.time(),
            },
        }

    monkeypatch.setattr("olano.core.connect_relay.poll_connect_bundle", fake_poll)
    real_pump(provider, session)

    assert session.status == "authorized"
    record = json.loads(session.token_path.read_text(encoding="utf-8"))
    assert record["access_token"] == "at-relay"
    assert record["auth_mode"] == "relay"
    assert record["client_source"] == "official"
    assert record["client_secret"] == ""  # the official secret never lands here
    assert record["extras"]["realmId"] == "realm-9"
    assert record["extras"]["instance_url"] == "https://inst.example"
    assert record["extras"]["post_exchange"] == "ran"
    assert hook_ran == [provider.id]
    assert is_connector_shared(provider.id, "a1") is True
    # State mapping consumed — a stray on-box callback finds nothing to redo.
    assert session.state not in base._BY_STATE

    # And the saved record surfaces its origin in the status dict.
    assert connector_auth_status(provider, agent="a1")["client_source"] == "official"


def test_relay_pump_surfaces_relay_error_and_lost_state(monkeypatch):
    provider = _provider("testsvc-pump-err")
    real_pump = base._relay_pump  # _official() stubs the attr for start()
    _official(monkeypatch)
    monkeypatch.setattr(base, "_RELAY_POLL_S", 0.001)

    session = _start(provider, creds=None)
    monkeypatch.setattr(
        "olano.core.connect_relay.poll_connect_bundle",
        lambda cfg, state, timeout=10.0: {"status": "error", "error": "access_denied"},
    )
    real_pump(provider, session)
    assert session.status == "error" and session.error == "access_denied"

    lost = _start(provider, creds=None)
    monkeypatch.setattr(
        "olano.core.connect_relay.poll_connect_bundle",
        lambda cfg, state, timeout=10.0: {"status": "unknown"},
    )
    real_pump(provider, lost)
    assert lost.status == "error"
    assert "start again" in (lost.error or "")


def test_relay_mode_on_box_callback_is_a_noop(monkeypatch):
    provider = _provider("testsvc-relay-cb")
    _official(monkeypatch)
    session = _start(provider, creds=None)
    _forbid_post(monkeypatch)

    result = resolve_connector_callback(
        provider, session.state, "code-1", None, {"state": session.state}
    )
    # The pump owns relay flows: a stray on-box redirect must not consume the
    # state or attempt an exchange this box holds no secret for.
    assert result is session
    assert session.status == "awaiting_authorization"
    assert session.state in base._BY_STATE


# ── get_live_record: refresh machinery ─────────────────────────────────────


def test_get_live_record_fresh_is_returned_untouched(monkeypatch):
    provider = _provider("testsvc-fresh")
    path = get_olano_paths().connector_token_path(provider.id)
    _write_record(path, access_token="at-fresh", expires_at=time.time() + 9999)
    _forbid_post(monkeypatch)

    live = get_live_record(provider)
    assert live is not None
    assert live["access_token"] == "at-fresh"


def test_get_live_record_refreshes_expired_with_post_client_auth(monkeypatch):
    provider = _provider("testsvc-refresh")
    path = get_olano_paths().connector_token_path(provider.id)
    _write_record(path, expires_at=time.time() - 10, refresh_token="rt-old")
    calls = _fake_post(
        monkeypatch,
        {"access_token": "at-new", "refresh_token": "rt-new", "expires_in": 1800},
    )

    live = get_live_record(provider)
    assert live is not None
    assert live["access_token"] == "at-new"
    assert live["expires_at"] == pytest.approx(time.time() + 1800, abs=30)

    assert len(calls) == 1
    data = calls[0]["data"]
    assert data["grant_type"] == "refresh_token"
    assert data["refresh_token"] == "rt-old"
    # client_auth="post" → creds in the body, no basic auth header.
    assert calls[0]["auth"] is None
    assert data["client_id"] == "cid"
    assert data["client_secret"] == "cs"

    # The rotated refresh token was persisted to disk.
    on_disk = base._load_record(path)
    assert on_disk is not None and on_disk["refresh_token"] == "rt-new"


def test_get_live_record_refresh_uses_basic_auth_when_configured(monkeypatch):
    provider = _provider("testsvc-basic", client_auth="basic")
    path = get_olano_paths().connector_token_path(provider.id)
    _write_record(path, expires_at=time.time() - 10, refresh_token="rt-old")
    calls = _fake_post(monkeypatch, {"access_token": "at-new", "expires_in": 1800})

    live = get_live_record(provider)
    assert live is not None and live["access_token"] == "at-new"
    assert calls[0]["auth"] == ("cid", "cs")
    assert "client_id" not in calls[0]["data"]
    assert "client_secret" not in calls[0]["data"]


def test_get_live_record_refresh_rearms_expiry_from_expires_default(monkeypatch):
    provider = _provider("testsvc-rearm", expires_default=7200.0)
    path = get_olano_paths().connector_token_path(provider.id)
    _write_record(path, expires_at=time.time() - 10, refresh_token="rt-old")
    _fake_post(monkeypatch, {"access_token": "at-new"})  # no expires_in in payload

    live = get_live_record(provider)
    assert live is not None
    assert live["access_token"] == "at-new"
    assert live["expires_at"] == pytest.approx(time.time() + 7200, abs=30)


def test_get_live_record_refresh_failure_returns_stale_while_valid(monkeypatch):
    provider = _provider("testsvc-stale")
    path = get_olano_paths().connector_token_path(provider.id)
    # Inside the refresh margin (120s) but not yet expired.
    _write_record(path, expires_at=time.time() + 60, refresh_token="rt-old")
    _fake_post(monkeypatch, payload=None)  # refresh endpoint raises

    live = get_live_record(provider)
    assert live is not None
    assert live["access_token"] == "at-old"  # stale but still-valid token returned


def test_get_live_record_refresh_failure_returns_none_when_expired(monkeypatch):
    provider = _provider("testsvc-dead")
    path = get_olano_paths().connector_token_path(provider.id)
    _write_record(path, expires_at=time.time() - 10, refresh_token="rt-old")
    _fake_post(monkeypatch, payload=None)  # refresh endpoint raises

    assert get_live_record(provider) is None


def test_refresh_relay_record_routes_through_relay(monkeypatch):
    """auth_mode == "relay" records refresh centrally — no on-box token POST."""
    provider = _provider(
        "testsvc-relayref",
        token_url="https://auth.testsvc.example/{tenant}/token",
    )
    path = get_olano_paths().connector_token_path(provider.id)
    _write_record(
        path,
        expires_at=time.time() - 10,
        refresh_token="rt-relay",
        auth_mode="relay",
        client_source="official",
        client_secret="",
        params={"tenant": "t1", "environment": "production"},
    )
    _forbid_post(monkeypatch)
    monkeypatch.setattr(
        "olano.core.connect_relay.resolve_connect_relay_config", lambda: _relay_cfg()
    )
    calls: list[dict[str, Any]] = []

    def fake_refresh(cfg, provider_id, refresh_token, *, params=None, timeout=35.0):
        calls.append({"provider": provider_id, "refresh_token": refresh_token, "params": params})
        return {"access_token": "at-new", "refresh_token": "rt-rotated", "expires_in": 1800}

    monkeypatch.setattr("olano.core.connect_relay.refresh_via_relay", fake_refresh)

    live = get_live_record(provider)
    assert live is not None and live["access_token"] == "at-new"
    assert live["expires_at"] == pytest.approx(time.time() + 1800, abs=30)
    assert calls == [
        {
            "provider": provider.id,
            "refresh_token": "rt-relay",
            # Only the token-URL template params travel — box-only params
            # (environment) would be refused by the relay's allow-list.
            "params": {"tenant": "t1"},
        }
    ]
    # Rotation persisted, relay markers intact.
    on_disk = base._load_record(path)
    assert on_disk is not None
    assert on_disk["refresh_token"] == "rt-rotated"
    assert on_disk["auth_mode"] == "relay" and on_disk["client_source"] == "official"


def test_refresh_relay_failure_keeps_stale_while_valid(monkeypatch):
    provider = _provider("testsvc-relaystale")
    path = get_olano_paths().connector_token_path(provider.id)
    _forbid_post(monkeypatch)
    monkeypatch.setattr(
        "olano.core.connect_relay.resolve_connect_relay_config", lambda: _relay_cfg()
    )

    def refuse(cfg, provider_id, refresh_token, *, params=None, timeout=35.0):
        raise RuntimeError("relay refresh failed (502): provider_error")

    monkeypatch.setattr("olano.core.connect_relay.refresh_via_relay", refuse)

    # Inside the refresh margin but still valid → the stale token is returned.
    _write_record(path, expires_at=time.time() + 60, refresh_token="rt", auth_mode="relay")
    live = get_live_record(provider)
    assert live is not None and live["access_token"] == "at-old"

    # Fully expired → None (credential resolution falls through to vault/env).
    _write_record(path, expires_at=time.time() - 10, refresh_token="rt", auth_mode="relay")
    assert get_live_record(provider) is None


def test_refresh_direct_records_never_touch_the_relay(monkeypatch):
    """Pre-fork and BYO records keep the on-box refresh, byte-identically."""
    provider = _provider("testsvc-directref")
    path = get_olano_paths().connector_token_path(provider.id)
    _write_record(path, expires_at=time.time() - 10, refresh_token="rt-old")  # no auth_mode

    def boom(*a: Any, **kw: Any):
        raise AssertionError("relay refresh must not be called for direct records")

    monkeypatch.setattr("olano.core.connect_relay.refresh_via_relay", boom)
    calls = _fake_post(monkeypatch, {"access_token": "at-new", "expires_in": 1800})

    live = get_live_record(provider)
    assert live is not None and live["access_token"] == "at-new"
    assert len(calls) == 1


def test_get_live_record_without_refresh_support(monkeypatch):
    provider = _provider("testsvc-norefresh", supports_refresh=False)
    path = get_olano_paths().connector_token_path(provider.id)
    _forbid_post(monkeypatch)

    # Expired → None even though a refresh token is present.
    _write_record(path, expires_at=time.time() - 10, refresh_token="rt-old")
    assert get_live_record(provider) is None

    # Non-expiring (expires_at None) → returned as-is, no refresh attempted.
    _write_record(path, access_token="at-forever", expires_at=None)
    live = get_live_record(provider)
    assert live is not None and live["access_token"] == "at-forever"


# ── connector_auth_status: three-tier scoping ──────────────────────────────


def test_status_three_tier_resolution():
    provider = _provider("testsvc-status")
    paths = get_olano_paths()

    # Nothing anywhere → none / disconnected for an agent lookup.
    assert connector_auth_status(provider, agent="a1") == {
        "connected": False,
        "scope": "none",
        "token_path": None,
        "shared": None,
        "methods": auth_methods(provider),
    }

    # System token → system scope, both for system and agent (fallback) lookups.
    _write_record(paths.connector_token_path(provider.id))
    system = connector_auth_status(provider)
    assert system["connected"] is True and system["scope"] == "system"
    fallback = connector_auth_status(provider, agent="a1")
    assert fallback["connected"] is True
    assert fallback["scope"] == "system"

    # Another agent's shared token beats the system fallback.
    _write_record(paths.agent_connector_token_path("a2", provider.id))
    set_connector_shared(provider.id, "a2", True)
    shared = connector_auth_status(provider, agent="a1")
    assert shared["connected"] is True
    assert shared["scope"] == "shared"
    assert shared["shared"] is True

    # The agent's own token wins over shared and system; shared flag from meta.
    own_path = paths.agent_connector_token_path("a1", provider.id)
    _write_record(own_path)
    own = connector_auth_status(provider, agent="a1")
    assert own["connected"] is True
    assert own["scope"] == "agent"
    assert own["shared"] is False
    assert own["token_path"] == str(own_path)
    set_connector_shared(provider.id, "a1", True)
    assert connector_auth_status(provider, agent="a1")["shared"] is True


def test_status_expired_without_refresh_is_disconnected():
    provider = _provider("testsvc-expstat")
    path = get_olano_paths().connector_token_path(provider.id)

    # Expired, no refresh token → disconnected.
    _write_record(path, expires_at=time.time() - 10)
    status = connector_auth_status(provider)
    assert status["connected"] is False and status["scope"] == "system"

    # A refresh token makes it connected again (refreshable)…
    _write_record(path, expires_at=time.time() - 10, refresh_token="rt")
    assert connector_auth_status(provider)["connected"] is True

    # …unless the provider cannot refresh.
    no_refresh = _provider("testsvc-expstat", supports_refresh=False)
    assert connector_auth_status(no_refresh)["connected"] is False


# ── disconnect ─────────────────────────────────────────────────────────────


def test_disconnect_removes_file_and_record_cache():
    provider = _provider("testsvc-disc")
    paths = get_olano_paths()

    path = paths.connector_token_path(provider.id)
    _write_record(path)
    assert base._load_record(path) is not None  # prime the record cache
    assert str(path) in base._RECORD_CACHE

    assert disconnect_connector(provider) is True
    assert not path.exists()
    assert str(path) not in base._RECORD_CACHE
    assert disconnect_connector(provider) is False  # already gone

    # Agent scope removes the per-agent file.
    agent_path = paths.agent_connector_token_path("a1", provider.id)
    _write_record(agent_path)
    assert disconnect_connector(provider, agent="a1") is True
    assert not agent_path.exists()


def test_disconnect_clears_the_shared_flag():
    """A leftover shared=True would silently re-share the *next* token
    connected for this service, though the reconnect asked for a private one."""
    provider = _provider("testsvc-shared-disc")
    paths = get_olano_paths()

    _write_record(paths.agent_connector_token_path("a1", provider.id))
    base.set_connector_shared(provider.id, "a1", True)
    assert base.is_connector_shared(provider.id, "a1") is True

    assert disconnect_connector(provider, agent="a1") is True
    assert base.is_connector_shared(provider.id, "a1") is False

    # Other services in the same connections.json are left alone.
    base.set_connector_shared("othersvc", "a1", True)
    disconnect_connector(provider, agent="a1")
    assert base.is_connector_shared("othersvc", "a1") is True


def test_disconnect_drops_an_in_flight_authorization():
    """A half-done OAuth flow must not land its token after a disconnect."""
    provider = _provider("testsvc-session-disc")
    session = base.OAuthSession(
        service=provider.id,
        session_id="sess-1",
        agent="a1",
        shared=False,
        token_path=get_olano_paths().agent_connector_token_path("a1", provider.id),
        redirect_uri="https://host.example/api/auth/x/callback",
        client_id="cid",
        client_secret="secret",
        state="state-1",
    )
    base._SESSIONS[session.session_id] = session
    base._BY_STATE[session.state] = session.session_id

    disconnect_connector(provider, agent="a1")
    assert "sess-1" not in base._SESSIONS
    assert "state-1" not in base._BY_STATE


# ── get_credential integration (tools/_credentials.py) ─────────────────────


def _register_testsvc() -> ConnectorProvider:
    return registry.register(_provider("testsvc", exposes={"TESTSVC_ACCESS_TOKEN": "access_token"}))


def test_get_credential_resolves_exposed_name_from_broker(monkeypatch):
    from olano.tools._credentials import get_credential

    provider = _register_testsvc()
    _write_record(get_olano_paths().connector_token_path(provider.id), access_token="broker-tok")
    monkeypatch.delenv("TESTSVC_ACCESS_TOKEN", raising=False)
    assert get_credential("TESTSVC_ACCESS_TOKEN") == "broker-tok"


def test_get_credential_overlay_beats_broker(monkeypatch):
    from olano.tools._credentials import agent_runtime_context, get_credential

    provider = _register_testsvc()
    _write_record(get_olano_paths().connector_token_path(provider.id), access_token="broker-tok")
    monkeypatch.delenv("TESTSVC_ACCESS_TOKEN", raising=False)

    with agent_runtime_context("a1", {"TESTSVC_ACCESS_TOKEN": "overlay-tok"}):
        assert get_credential("TESTSVC_ACCESS_TOKEN") == "overlay-tok"
    # Outside the per-turn overlay the broker resolves again.
    assert get_credential("TESTSVC_ACCESS_TOKEN") == "broker-tok"


def test_get_credential_broker_beats_environ(monkeypatch):
    from olano.tools._credentials import get_credential

    provider = _register_testsvc()
    _write_record(get_olano_paths().connector_token_path(provider.id), access_token="broker-tok")
    monkeypatch.setenv("TESTSVC_ACCESS_TOKEN", "env-tok")
    assert get_credential("TESTSVC_ACCESS_TOKEN") == "broker-tok"


def test_get_credential_unexposed_name_falls_through_to_environ(monkeypatch):
    from olano.tools._credentials import get_credential

    provider = _register_testsvc()
    _write_record(get_olano_paths().connector_token_path(provider.id), access_token="broker-tok")
    monkeypatch.setenv("TESTSVC_OTHER_KEY", "env-val")
    assert get_credential("TESTSVC_OTHER_KEY") == "env-val"


# ── refusal reports: forced refresh + the needs_reauth mark ────────────────
# A toolkit handed a 401 reports it here. The file cannot know a token is dead
# (revoked session, a session policy shorter than expires_default), so the
# probe read "Connected" until someone reconnected by hand.


def test_refresh_record_now_refreshes_a_token_the_file_still_calls_valid(monkeypatch):
    provider = _provider("testsvc-forced")
    path = get_olano_paths().connector_token_path(provider.id)
    _write_record(path, expires_at=time.time() + 3600, refresh_token="rt-old")
    calls = _fake_post(monkeypatch, {"access_token": "at-new", "expires_in": 1800})

    fresh, why = base.refresh_record_now(provider)

    assert why == "" and fresh is not None and fresh["access_token"] == "at-new"
    assert len(calls) == 1 and calls[0]["data"]["refresh_token"] == "rt-old"
    on_disk = base._load_record(path)
    assert on_disk is not None and on_disk["access_token"] == "at-new"
    assert connector_auth_status(provider)["connected"] is True


def test_refresh_record_now_without_a_refresh_grant_says_so(monkeypatch):
    provider = _provider("testsvc-norefresh")
    path = get_olano_paths().connector_token_path(provider.id)
    _write_record(path, expires_at=None)  # no refresh_token
    _forbid_post(monkeypatch)

    fresh, why = base.refresh_record_now(provider)

    assert fresh is None
    assert "signing in again" in why
    # Nothing was marked: the caller decides after its own retry.
    assert "needs_reauth" not in connector_auth_status(provider)


def test_refresh_record_now_marks_on_a_token_endpoint_refusal_but_not_on_transport(monkeypatch):
    provider = _provider("testsvc-refused")
    path = get_olano_paths().connector_token_path(provider.id)
    _write_record(path, expires_at=time.time() + 3600, refresh_token="rt-old")

    # A dead socket is not a verdict on the grant.
    _fake_post(monkeypatch, payload=None)  # raises "token endpoint down"
    fresh, why = base.refresh_record_now(provider)
    assert fresh is None and "token endpoint down" in why
    assert "needs_reauth" not in connector_auth_status(provider)

    # RFC 6749 §5.2: a refusal is a 4xx from the token endpoint. That one is final.
    def refuse(url, data, *, headers=None, auth=None):
        raise RuntimeError('Token endpoint returned 400: {"error":"invalid_grant"}')

    monkeypatch.setattr(base, "_post_token", refuse)
    fresh, why = base.refresh_record_now(provider)
    assert fresh is None and "invalid_grant" in why
    status = connector_auth_status(provider)
    assert status["connected"] is False and status["needs_reauth"] is True
    assert "invalid_grant" in status["auth_error"]


def test_refresh_record_now_honours_the_cooldown_after_a_mark(monkeypatch):
    provider = _provider("testsvc-cooldown")
    path = get_olano_paths().connector_token_path(provider.id)
    _write_record(path, expires_at=time.time() + 3600, refresh_token="rt-old")
    base.mark_auth_failed(provider, error="sf_query: 401 INVALID_SESSION_ID")
    _forbid_post(monkeypatch)

    fresh, why = base.refresh_record_now(provider)

    assert fresh is None and "INVALID_SESSION_ID" in why

    # Past the cooldown the refresh runs again, and, honoured, clears the mark.
    marked = base._load_record(path)
    assert marked is not None
    base._save_record({**marked, "auth_failed_at": time.time() - 3600}, path)
    _fake_post(monkeypatch, {"access_token": "at-new", "expires_in": 1800})
    fresh, why = base.refresh_record_now(provider)
    assert fresh is not None and fresh["access_token"] == "at-new"
    status = connector_auth_status(provider)
    assert status["connected"] is True and "needs_reauth" not in status


def test_mark_auth_failed_turns_the_status_honest_without_deleting_the_record(monkeypatch):
    provider = _provider("testsvc-marked")
    path = get_olano_paths().connector_token_path(provider.id)
    _write_record(path, expires_at=time.time() + 3600, refresh_token="rt-old")
    _forbid_post(monkeypatch)
    assert connector_auth_status(provider)["connected"] is True

    marked = base.mark_auth_failed(provider, error="  sf_query:   401 Unauthorized  ")

    assert marked is not None and marked["access_token"] == "at-old"
    status = connector_auth_status(provider)
    assert status["connected"] is False
    assert status["needs_reauth"] is True
    assert status["auth_error"] == "sf_query: 401 Unauthorized"  # whitespace folded
    assert status["auth_failed_at"] == pytest.approx(time.time(), abs=5)
    # The record still resolves: a diagnosis, not a deletion.
    live = get_live_record(provider)
    assert live is not None and live["access_token"] == "at-old"
    assert base.auth_failure(live) == {
        "failed_at": status["auth_failed_at"],
        "error": status["auth_error"],
    }
    # No record at this scope → nothing to mark.
    assert base.mark_auth_failed(_provider("testsvc-absent"), error="x") is None


def test_mark_auth_failed_lands_on_the_record_the_agent_resolves(monkeypatch):
    """Agent scope resolves own → shared → system; the mark follows the same path."""
    provider = _provider("testsvc-scoped")
    paths = get_olano_paths()
    system_path = paths.connector_token_path(provider.id)
    _write_record(system_path, access_token="sys")
    _forbid_post(monkeypatch)

    base.mark_auth_failed(provider, "agent-a", error="401")

    assert connector_auth_status(provider, "agent-a")["needs_reauth"] is True
    assert connector_auth_status(provider)["needs_reauth"] is True  # it WAS the system record
    # A reconnect at agent scope writes a fresh record, unmarked, and wins for that agent.
    _write_record(paths.agent_connector_token_path("agent-a", provider.id), access_token="own")
    status = connector_auth_status(provider, "agent-a")
    assert (
        status["connected"] is True and status["scope"] == "agent" and "needs_reauth" not in status
    )


def test_exposing_provider_answers_from_the_registry_alone():
    from olano.core.connectors import exposing_provider

    provider = _register_testsvc()
    found = exposing_provider("TESTSVC_ACCESS_TOKEN")
    assert found is not None and found[0] is provider and found[1] == "access_token"
    assert exposing_provider("NOBODY_EXPOSES_THIS") is None


def test_saved_record_is_owner_only(tmp_path):
    import os

    if os.name != "posix":
        return
    path = tmp_path / "tokens" / "svc.json"
    _write_record(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["svc.json"]
