"""Provider-definition tests — registration sanity for all 12 services plus
each provider's special behavior (hooks, templated URLs, extras capture).

Network is stubbed at the broker's HTTP seams (``base._post_token`` /
``base._get_json``); the autouse ``lionclaw_home`` fixture isolates all token
files under a tmp LIONCLAW_HOME.
"""

from __future__ import annotations

import time

import pytest

import olano.core.connectors as conn
from olano.config.home import get_olano_paths
from olano.core.connectors import base

ALL_IDS = [
    "buffer",
    "docusign",
    "fitbit",
    "github",
    "hubspot",
    "linkedin",
    "meta",
    "microsoft365",
    "notion",
    "quickbooks",
    "reddit",
    "salesforce",
    "shopify",
    "slack",
    "x_twitter",
]

CREDS = {"client_id": "cid", "client_secret": "csec"}


@pytest.fixture(autouse=True)
def _clear_sessions():
    base._SESSIONS.clear()
    base._BY_STATE.clear()
    base._RECORD_CACHE.clear()
    yield
    base._SESSIONS.clear()
    base._BY_STATE.clear()
    base._RECORD_CACHE.clear()


def _start(service: str, creds: dict | None = None, **kw):
    provider = conn.get_provider(service)
    assert provider is not None
    return provider, conn.start_connector_auth(
        provider,
        creds={**CREDS, **(creds or {})},
        agent=kw.get("agent"),
        shared=kw.get("shared", False),
        redirect_uri=f"https://host.example/api/auth/{service}/callback",
    )


def _finish(provider, session, monkeypatch, payload: dict, query: dict | None = None):
    calls: list[dict] = []

    def fake_post(url, data, *, headers=None, auth=None):
        calls.append({"url": url, "data": data, "headers": headers, "auth": auth})
        return payload

    monkeypatch.setattr(base, "_post_token", fake_post)
    result = conn.resolve_connector_callback(
        provider, session.state, "auth-code", None, query or {}
    )
    return result, calls


# ── registration sanity ────────────────────────────────────────────────────


def test_all_providers_registered():
    assert conn.provider_ids() == ALL_IDS


@pytest.mark.parametrize("service", ALL_IDS)
def test_provider_shape(service):
    p = conn.get_provider(service)
    assert p is not None
    assert p.authorize_url.startswith("https://")
    assert p.token_url.startswith("https://")
    assert p.creds_vault_key.endswith("_OAUTH_CLIENT")
    assert p.exposes, f"{service} exposes nothing"
    for field_path in p.exposes.values():
        assert (
            field_path in base._EXPOSABLE_TOP_LEVEL
            or field_path.startswith("extras.")
            or field_path.startswith("params.")
        )
    # Templated URLs must be fully resolvable from optional/required params.
    params = dict(p.optional_params)
    for req in p.required_params:
        params.setdefault(req, "sample")
    p.authorize_url.format(**params)
    p.token_url.format(**params)


@pytest.mark.parametrize("service", ALL_IDS)
def test_status_disconnected_by_default(service):
    p = conn.get_provider(service)
    status = conn.connector_auth_status(p)
    assert status["connected"] is False


# ── per-provider behavior ──────────────────────────────────────────────────


def test_microsoft365_tenant_templating_and_pkce(monkeypatch):
    provider, session = _start("microsoft365", {"tenant": "my-tenant-guid"})
    assert "login.microsoftonline.com/my-tenant-guid/oauth2/v2.0/authorize" in (
        session.authorization_url or ""
    )
    assert "code_challenge_method=S256" in session.authorization_url
    result, calls = _finish(
        provider,
        session,
        monkeypatch,
        {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
    )
    assert result.status == "authorized"
    assert "my-tenant-guid" in calls[0]["url"]
    assert calls[0]["data"]["code_verifier"] == session.code_verifier
    assert calls[0]["data"]["client_secret"] == "csec"  # client_auth=post


def test_salesforce_instance_url_and_default_expiry(monkeypatch):
    provider, session = _start("salesforce")
    result, _ = _finish(
        provider,
        session,
        monkeypatch,
        {
            "access_token": "at",
            "refresh_token": "rt",
            "instance_url": "https://org.my.salesforce.com",
        },
    )
    assert result.status == "authorized"
    record = base._load_record(session.token_path)
    assert record["extras"]["instance_url"] == "https://org.my.salesforce.com"
    # No expires_in in the response → expires_default (~2h) applies.
    assert record["expires_at"] == pytest.approx(time.time() + 7200, abs=30)
    assert conn.resolve_exposed_credential  # exposure covered in broker tests


def test_salesforce_refresh_rearms_without_expires_in(monkeypatch):
    provider, session = _start("salesforce")
    _finish(
        provider,
        session,
        monkeypatch,
        {"access_token": "old", "refresh_token": "rt", "instance_url": "https://x"},
    )
    record = base._load_record(session.token_path)
    record["expires_at"] = time.time() - 10  # force expiry
    base._save_record(record, session.token_path)

    def fake_refresh(url, data, *, headers=None, auth=None):
        assert data["grant_type"] == "refresh_token"
        return {"access_token": "new"}  # Salesforce omits expires_in

    monkeypatch.setattr(base, "_post_token", fake_refresh)
    live = conn.get_live_record(provider)
    assert live["access_token"] == "new"
    assert live["expires_at"] == pytest.approx(time.time() + 7200, abs=30)


def test_quickbooks_realm_capture_and_basic_auth(monkeypatch):
    provider, session = _start("quickbooks")
    result, calls = _finish(
        provider,
        session,
        monkeypatch,
        {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
        query={"realmId": "1234567890"},
    )
    assert result.status == "authorized"
    assert calls[0]["auth"] == ("cid", "csec")  # basic client auth
    record = base._load_record(session.token_path)
    assert record["extras"]["realmId"] == "1234567890"
    assert record["params"]["environment"] == "production"
    assert base.extract_exposed_field(record, "extras.realmId") == "1234567890"
    assert base.extract_exposed_field(record, "params.environment") == "production"


def test_shopify_normalize_token_request_and_store_domain(monkeypatch):
    provider, session = _start("shopify", {"shop": "https://My-Store.myshopify.com/"})
    assert session.params["shop"] == "My-Store"
    assert "My-Store.myshopify.com/admin/oauth/authorize" in session.authorization_url
    result, calls = _finish(provider, session, monkeypatch, {"access_token": "at"})
    assert result.status == "authorized"
    assert calls[0]["url"] == "https://My-Store.myshopify.com/admin/oauth/access_token"
    assert calls[0]["data"] == {"client_id": "cid", "client_secret": "csec", "code": "auth-code"}
    record = base._load_record(session.token_path)
    assert record["expires_at"] is None  # permanent offline token
    assert record["extras"]["store_domain"] == "My-Store.myshopify.com"
    assert base.extract_exposed_field(record, "extras.store_domain") == "My-Store.myshopify.com"


def test_shopify_requires_shop():
    provider = conn.get_provider("shopify")
    with pytest.raises(ValueError, match="shop"):
        conn.start_connector_auth(
            provider, creds=dict(CREDS), agent=None, shared=False, redirect_uri="https://h/cb"
        )


def test_meta_long_lived_exchange_prefers_configured_page(monkeypatch):
    provider, session = _start("meta")
    monkeypatch.setenv("META_PAGE_ID", "p2")
    urls: list[str] = []

    def fake_get_json(url, *, headers=None):
        urls.append(url)
        if "fb_exchange_token" in url:
            return {"access_token": "long-lived-user", "expires_in": 5184000}
        return {
            "data": [
                {"id": "p1", "name": "One", "access_token": "page-1"},
                {"id": "p2", "name": "Two", "access_token": "page-2"},
            ]
        }

    monkeypatch.setattr(base, "_get_json", fake_get_json)
    # token_method="get" → the code exchange itself also goes through httpx.get;
    # stub the module-level httpx call inside resolve_connector_callback.
    import httpx

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "short-lived", "expires_in": 5000}

    monkeypatch.setattr(httpx, "get", lambda url, **kw: _Resp())
    result = conn.resolve_connector_callback(provider, session.state, "code", None, {})
    assert result.status == "authorized"
    record = base._load_record(session.token_path)
    assert record["access_token"] == "page-2"  # META_PAGE_ID match wins
    assert record["expires_at"] is None  # page tokens don't expire
    assert record["extras"]["user_token"] == "long-lived-user"
    assert record["extras"]["page_id"] == "p2"
    assert any("fb_exchange_token" in u for u in urls)


def test_meta_keeps_user_token_when_no_page_choice(monkeypatch):
    provider, session = _start("meta")
    monkeypatch.delenv("META_PAGE_ID", raising=False)

    def fake_get_json(url, *, headers=None):
        if "fb_exchange_token" in url:
            return {"access_token": "long-lived-user", "expires_in": 5184000}
        return {
            "data": [
                {"id": "p1", "name": "One", "access_token": "page-1"},
                {"id": "p2", "name": "Two", "access_token": "page-2"},
            ]
        }

    monkeypatch.setattr(base, "_get_json", fake_get_json)
    import httpx

    class _Resp:
        status_code = 200

        def json(self):
            return {"access_token": "short-lived", "expires_in": 5000}

    monkeypatch.setattr(httpx, "get", lambda url, **kw: _Resp())
    result = conn.resolve_connector_callback(provider, session.state, "code", None, {})
    assert result.status == "authorized"
    record = base._load_record(session.token_path)
    # Two pages, none configured → keep the long-lived user token.
    assert record["access_token"] == "long-lived-user"
    assert record["expires_at"] == pytest.approx(time.time() + 5184000, abs=60)
    assert [p["id"] for p in record["extras"]["pages"]] == ["p1", "p2"]


def test_docusign_userinfo_discovery(monkeypatch):
    provider, session = _start("docusign", {"environment": "account-d"})
    assert "account-d.docusign.com/oauth/auth" in session.authorization_url

    def fake_get_json(url, *, headers=None):
        assert url == "https://account-d.docusign.com/oauth/userinfo"
        assert headers == {"Authorization": "Bearer at"}
        return {
            "accounts": [
                {"account_id": "aid-2", "is_default": False, "base_uri": "https://eu.docusign.net"},
                {"account_id": "aid-1", "is_default": True, "base_uri": "https://na3.docusign.net"},
            ]
        }

    monkeypatch.setattr(base, "_get_json", fake_get_json)
    result, calls = _finish(
        provider,
        session,
        monkeypatch,
        {"access_token": "at", "refresh_token": "rt", "expires_in": 28800},
    )
    assert result.status == "authorized"
    assert calls[0]["auth"] == ("cid", "csec")
    assert calls[0]["data"]["code_verifier"]  # pkce
    record = base._load_record(session.token_path)
    assert record["extras"]["account_id"] == "aid-1"  # is_default preferred
    assert record["extras"]["base_url"] == "https://na3.docusign.net/restapi"


def test_github_classic_token_nonexpiring_and_exposure(monkeypatch):
    provider, session = _start("github")
    result, calls = _finish(
        provider,
        session,
        monkeypatch,
        # Classic OAuth-app response: no expires_in, no refresh_token.
        {"access_token": "gho_x", "token_type": "bearer", "scope": "repo"},
    )
    assert result.status == "authorized"
    assert calls[0]["url"] == "https://github.com/login/oauth/access_token"
    assert calls[0]["data"]["client_secret"] == "csec"  # client_auth=post
    record = base._load_record(session.token_path)
    assert record["expires_at"] is None  # non-expiring, no reconnects needed
    # One connection surfaces under both credential names (toolkit + MCP).
    assert conn.resolve_exposed_credential("GITHUB_TOKEN") == "gho_x"
    assert conn.resolve_exposed_credential("GITHUB_PERSONAL_ACCESS_TOKEN") == "gho_x"


def test_github_app_token_expires_and_refreshes(monkeypatch):
    provider, session = _start("github")
    _finish(
        provider,
        session,
        monkeypatch,
        # GitHub App user-token response: 8h expiry + rotating refresh token.
        {"access_token": "ghu_1", "refresh_token": "ghr_1", "expires_in": 28800},
    )
    record = base._load_record(session.token_path)
    assert record["expires_at"] == pytest.approx(time.time() + 28800, abs=30)
    record["expires_at"] = time.time() - 10  # force expiry
    base._save_record(record, session.token_path)

    def fake_refresh(url, data, *, headers=None, auth=None):
        assert url == "https://github.com/login/oauth/access_token"
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "ghr_1"
        return {"access_token": "ghu_2", "refresh_token": "ghr_2", "expires_in": 28800}

    monkeypatch.setattr(base, "_post_token", fake_refresh)
    live = conn.get_live_record(provider)
    assert live["access_token"] == "ghu_2"
    assert live["refresh_token"] == "ghr_2"


def test_reddit_exposure_and_token_headers(monkeypatch):
    provider, session = _start("reddit")
    assert "duration=permanent" in session.authorization_url
    result, calls = _finish(
        provider,
        session,
        monkeypatch,
        {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
    )
    assert result.status == "authorized"
    assert calls[0]["headers"]["User-Agent"].startswith("olano:")
    assert calls[0]["auth"] == ("cid", "csec")
    record = base._load_record(session.token_path)
    assert base.extract_exposed_field(record, "refresh_token") == "rt"
    assert base.extract_exposed_field(record, "client_id") == "cid"
    assert base.extract_exposed_field(record, "client_secret") == "csec"


def test_x_twitter_pkce_basic_auth(monkeypatch):
    provider, session = _start("x_twitter")
    assert provider.pkce and provider.client_auth == "basic"
    assert "offline.access" in " ".join(provider.scopes)
    result, calls = _finish(
        provider,
        session,
        monkeypatch,
        {"access_token": "at", "refresh_token": "rt", "expires_in": 7200},
    )
    assert result.status == "authorized"
    assert calls[0]["auth"] == ("cid", "csec")
    assert calls[0]["data"]["code_verifier"] == session.code_verifier


def test_linkedin_and_buffer_no_refresh():
    for service in ("linkedin", "buffer"):
        p = conn.get_provider(service)
        assert p.supports_refresh is False


def test_fitbit_scopes_match_toolkit():
    p = conn.get_provider("fitbit")
    assert set(p.scopes) == {"activity", "heartrate", "sleep", "weight"}
    assert p.pkce and p.client_auth == "basic"


def test_hubspot_required_and_optional_scopes_and_exchange(monkeypatch):
    from urllib.parse import parse_qs, urlparse

    from olano.core.connectors import base as connector_base
    from olano.core.connectors.providers import hubspot as hs

    provider, session = _start("hubspot")
    url = session.authorization_url or ""
    assert url.startswith("https://app.hubspot.com/oauth/authorize?")
    query = parse_qs(urlparse(url).query)
    # Required scopes ride `scope`, optional ones `optional_scope`; HubSpot
    # refuses an install that puts one in the other's parameter.
    assert query["scope"] == [" ".join(hs.REQUIRED_SCOPES)]
    assert query["optional_scope"] == [" ".join(hs.OPTIONAL_SCOPES)]
    assert not set(hs.REQUIRED_SCOPES) & set(hs.OPTIONAL_SCOPES)
    assert "code_challenge" not in query  # HubSpot has no PKCE
    # Relay-shaped, so an official client can serve it.
    assert connector_base.relay_capable(provider)

    result, calls = _finish(
        provider,
        session,
        monkeypatch,
        {"access_token": "at", "refresh_token": "rt", "expires_in": 1800, "hub_id": 4242},
    )
    assert result.status == "authorized"
    assert calls[0]["url"] == "https://api.hubapi.com/oauth/v3/token"
    assert calls[0]["data"]["client_secret"] == "csec"  # client_auth=post
    assert "code_verifier" not in calls[0]["data"]
    record = base._load_record(session.token_path)
    assert record["extras"]["hub_id"] == 4242
    assert record["expires_at"] == pytest.approx(time.time() + 1800, abs=30)
    assert conn.resolve_exposed_credential("HUBSPOT_ACCESS_TOKEN") == "at"


def test_hubspot_setup_guide_lists_exactly_the_requested_scopes():
    """The Connections setup guide tells an operator which scopes to configure
    on the HubSpot app, and HubSpot refuses an install whose scopes differ from
    the app's in either direction, so the guide's chips must be exactly the
    provider's two lists, optional ones marked."""
    import re
    from pathlib import Path

    from olano.core.connectors.providers import hubspot as hs

    root = Path(__file__).resolve().parents[2]
    source = (root / "olano/dashboard_2.0/src/views/connections/SetupGuideModal.tsx").read_text(
        encoding="utf-8"
    )
    block = source.split('"hubspot-oauth": {', 1)[1].split("quirk:", 1)[0]
    chips_src = block.split("chips: [", 1)[1].split("],", 1)[0]
    chips = re.findall(r'"([^"]+)"', chips_src)
    required = [c for c in chips if not c.endswith(" (optional)")]
    optional = [c.removesuffix(" (optional)") for c in chips if c.endswith(" (optional)")]
    assert required == list(hs.REQUIRED_SCOPES)
    assert optional == list(hs.OPTIONAL_SCOPES)


def test_agent_scoped_token_and_share(monkeypatch):
    provider, session = _start("microsoft365", agent="merli", shared=True)
    _finish(
        provider,
        session,
        monkeypatch,
        {"access_token": "at", "refresh_token": "rt", "expires_in": 3600},
    )
    paths = get_olano_paths()
    assert paths.agent_connector_token_path("merli", "microsoft365").exists()
    assert conn.is_connector_shared("microsoft365", "merli") is True
    # Another agent falls back to the shared token.
    status = conn.connector_auth_status(provider, agent="someone-else")
    assert status["connected"] is True
    assert status["scope"] == "shared"
