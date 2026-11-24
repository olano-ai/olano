"""Session-cookie hardening: Secure + __Host- prefix on HTTPS installs.

On HTTPS-only customer instances the
session cookie must be `__Host-` prefixed (Secure, Path=/, no Domain) so no
sibling host under a shared parent zone can plant/fixate a session cookie
("cookie tossing"), with a dev-mode carve-out for http://localhost e2e runs.

Covers: name/flag selection from env, the legacy-cookie fallback read (live
production sessions issued before the upgrade must keep working), logout
clearing both names, and the explicit LIONCLAW_SECURE_COOKIES override.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.runtime import auth as auth_layer
from olano.runtime.api import create_api
from olano.users.store import get_user_store, reset_user_store


@pytest.fixture(autouse=True)
def _plain_env(monkeypatch: pytest.MonkeyPatch):
    """Start every test from the dev default: no public URL, no override."""
    monkeypatch.delenv("LIONCLAW_PUBLIC_URL", raising=False)
    monkeypatch.delenv("LIONCLAW_SECURE_COOKIES", raising=False)


def _client_with_admin():
    reset_user_store()
    store = get_user_store()
    admin = store.create_user(email="admin@x.com", password="pw", name="Root", is_admin=True)
    auth_layer.set_multi_user_enabled(True)
    runtime = MagicMock()
    runtime._runners = {}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    client = TestClient(create_api(runtime))
    return client, admin


def _login(client: TestClient):
    return client.post("/api/auth/login", json={"email": "admin@x.com", "password": "pw"})


# ── mode selection ──────────────────────────────────────────────────────────


def test_secure_cookies_off_by_default():
    assert auth_layer.secure_cookies_enabled() is False
    assert auth_layer.session_cookie_name() == "lc_session"


def test_https_public_url_enables_secure(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://acme.olano.run")
    assert auth_layer.secure_cookies_enabled() is True
    assert auth_layer.session_cookie_name() == "__Host-lc_session"


def test_http_public_url_stays_insecure(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "http://localhost:8765")
    assert auth_layer.secure_cookies_enabled() is False


def test_override_beats_inference(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://acme.olano.run")
    monkeypatch.setenv("LIONCLAW_SECURE_COOKIES", "0")
    assert auth_layer.secure_cookies_enabled() is False
    monkeypatch.delenv("LIONCLAW_PUBLIC_URL")
    monkeypatch.setenv("LIONCLAW_SECURE_COOKIES", "1")
    assert auth_layer.secure_cookies_enabled() is True


# ── issuing ─────────────────────────────────────────────────────────────────


def test_login_sets_legacy_cookie_without_secure_in_dev():
    client, _admin = _client_with_admin()
    resp = _login(client)
    assert resp.status_code == 200
    header = resp.headers["set-cookie"]
    assert header.startswith("lc_session=")
    assert "secure" not in header.lower()
    assert "httponly" in header.lower()


def test_login_sets_host_prefixed_secure_cookie_on_https(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://acme.olano.run")
    client, _admin = _client_with_admin()
    resp = _login(client)
    assert resp.status_code == 200
    headers = resp.headers.get_list("set-cookie")
    issued = [h for h in headers if h.startswith("__Host-lc_session=") and "Max-Age=0" not in h]
    assert len(issued) == 1, headers
    low = issued[0].lower()
    # The __Host- contract: Secure, Path=/, and NO Domain attribute.
    assert "secure" in low
    assert "path=/" in low
    assert "domain=" not in low
    assert "httponly" in low
    assert "samesite=lax" in low
    # The legacy name is retired in the same response (expired, not re-issued).
    retired = [h for h in headers if h.startswith("lc_session=")]
    assert retired and all('=""' in h or "Max-Age=0" in h for h in retired), headers


# ── reading ─────────────────────────────────────────────────────────────────


def test_legacy_cookie_still_authenticates_in_secure_mode(monkeypatch: pytest.MonkeyPatch):
    """A session issued before the secure-cookie upgrade keeps working."""
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://acme.olano.run")
    client, admin = _client_with_admin()
    token = get_user_store().issue_session(admin)
    client.cookies.set("lc_session", token)
    resp = client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == "admin@x.com"


def test_host_prefixed_cookie_wins_over_legacy():
    store_token = "host-token"
    cookies = {"lc_session": "legacy-token", "__Host-lc_session": store_token}
    assert auth_layer.session_token_from_cookies(cookies) == store_token
    assert auth_layer.session_token_from_cookies({"lc_session": "legacy-token"}) == "legacy-token"
    assert auth_layer.session_token_from_cookies({}) is None


# ── logout ──────────────────────────────────────────────────────────────────


def test_logout_revokes_and_clears_both_names(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://acme.olano.run")
    client, admin = _client_with_admin()
    store = get_user_store()
    token = store.issue_session(admin)
    client.cookies.set("lc_session", token)  # pre-upgrade browser state
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 200
    cleared = resp.headers.get_list("set-cookie")
    names = {h.split("=", 1)[0] for h in cleared}
    assert names == {"__Host-lc_session", "lc_session"}, cleared
    # The token itself is dead server-side, whichever cookie carried it.
    assert store.resolve_session(token) is None
