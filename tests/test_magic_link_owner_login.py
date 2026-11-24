"""Owner-login: the chat → dashboard magic link.

This flow is the most sensitive magic link — it hands out a real dashboard
session — so these tests pin every path that establishes trust (bootstrap,
password, auto-bind, one-tap grant) and every guard around it: the session must
authenticate, the redirect must be a safe enum, the wrong-password cap must
burn, a stale binding must fall back, and none of the account/session material
may leak into the link store.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.config.system_settings import set_magic_link_settings
from olano.magic_links import owner_login, service, store
from olano.runtime.api import create_api
from olano.runtime.auth import multi_user_enabled, session_cookie_name
from olano.users.store import get_user_store

PASSWORD = "hunter2hunter2"


@pytest.fixture(autouse=True)
def public_origin(monkeypatch):
    monkeypatch.setenv("LIONCLAW_PUBLIC_URL", "https://acme.olano.run")
    import olano.core.mcp_setup as mcp_setup

    monkeypatch.setattr(mcp_setup, "_LAST_SEEN_ORIGIN", None)


@pytest.fixture(autouse=True)
def owner_login_enabled():
    set_magic_link_settings({"owner_login": {"enabled": True}})


@pytest.fixture
def client():
    return TestClient(create_api(MagicMock()), base_url="https://testserver")


def _mint(client, *, user_id="12345", section="overview", agent=""):
    params = {"section": section}
    if agent:
        params["agent"] = agent
    minted = service.mint_link(
        "owner-login",
        params=params,
        requested_by={"channel": "telegram", "user_id": user_id, "username": "jrivera"},
        notify={},
        minted_by="test",
    )
    token = minted.url.rsplit("/", 1)[-1]
    client.post(f"/api/connect/{token}/unlock", json={"pin": minted.pin})
    return minted, token


def _submit(client, token, fields):
    return client.post(f"/api/connect/{token}/action", json={"op": "submit", "fields": fields})


def _has_session_cookie(response) -> bool:
    return session_cookie_name() in response.headers.get("set-cookie", "")


# ── gating ───────────────────────────────────────────────────────────────────


def test_disabled_owner_login_refuses_to_mint():
    set_magic_link_settings({"owner_login": {"enabled": False}})
    with pytest.raises(service.MagicLinkError, match="turned off"):
        service.mint_link("owner-login", params={}, requested_by={}, notify={}, minted_by="t")


# ── bootstrap (no accounts yet) ──────────────────────────────────────────────


def test_bootstrap_creates_admin_enables_multiuser_and_binds(client):
    assert get_user_store().db.count_users() == 0
    _, token = _mint(client, section="prompt", agent="merli")

    state = client.get(f"/api/connect/{token}/state").json()
    assert state["mode"] == "bootstrap"

    r = _submit(client, token, {"email": "owner@example.com", "password": PASSWORD})
    assert r.status_code == 200
    assert r.json()["redirect"] == "/agents/merli"
    assert _has_session_cookie(r)

    assert get_user_store().db.count_users() == 1
    assert multi_user_enabled() is True
    assert owner_login.get_binding("telegram:12345") is not None
    # The issued cookie authenticates a real dashboard request.
    me = client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["user"]["is_admin"] is True


def test_bootstrap_rejects_a_weak_password(client):
    _, token = _mint(client)
    r = _submit(client, token, {"email": "owner@example.com", "password": "short"})
    assert r.status_code == 400
    assert "at least" in r.json()["detail"]
    assert get_user_store().db.count_users() == 0  # nothing created


def test_first_run_bootstrap_preserves_other_config_blocks(client):
    """Enabling multi-user during bootstrap must not wipe the magic-links block
    (or cognition/disk_monitor/…) — set_multi_user_enabled merges, not clobbers."""
    set_magic_link_settings({"ttl_minutes": 20, "owner_login": {"enabled": True}})
    _, token = _mint(client)
    _submit(client, token, {"email": "owner@example.com", "password": PASSWORD})
    from olano.config.system_settings import load_magic_link_settings

    after = load_magic_link_settings()
    assert after.ttl_minutes == 20  # survived the multi-user flip
    assert after.owner_login.enabled is True


# ── password path + one-tap grant ────────────────────────────────────────────


def _bootstrap_admin(client):
    _, token = _mint(client)
    _submit(client, token, {"email": "owner@example.com", "password": PASSWORD})


def test_new_chat_identity_must_prove_the_password_then_is_bound(client):
    _bootstrap_admin(client)  # admin exists; telegram:12345 already bound

    # A different chat identity has no binding → login mode.
    other = TestClient(create_api(MagicMock()), base_url="https://testserver")
    _, token = _mint(other, user_id="99999", section="vault")
    assert other.get(f"/api/connect/{token}/state").json()["mode"] == "login"

    bad = _submit(other, token, {"email": "owner@example.com", "password": "wrong"})
    assert bad.status_code == 403 and "left" in bad.json()["detail"]

    good = _submit(other, token, {"email": "owner@example.com", "password": PASSWORD})
    assert good.status_code == 200 and good.json()["redirect"] == "/vault"
    assert _has_session_cookie(good)
    assert owner_login.get_binding("telegram:99999") is not None


def test_bound_identity_gets_one_tap_grant_no_password(client):
    _bootstrap_admin(client)  # binds telegram:12345
    _, token = _mint(client, section="connections")  # same identity
    state = client.get(f"/api/connect/{token}/state").json()
    assert state["mode"] == "grant"
    r = _submit(client, token, {})  # no fields
    assert r.status_code == 200 and r.json()["redirect"] == "/connections"
    assert _has_session_cookie(r)


def test_wrong_password_burns_the_link_after_the_cap(client):
    set_magic_link_settings({"owner_login": {"enabled": True}, "max_pin_attempts": 3})
    _bootstrap_admin(client)
    other = TestClient(create_api(MagicMock()), base_url="https://testserver")
    _, token = _mint(other, user_id="88888")
    for _ in range(3):
        r = _submit(other, token, {"email": "owner@example.com", "password": "nope"})
    assert r.status_code == 403 and "disabled" in r.json()["detail"]
    assert service.resolve_token(token).status == store.STATUS_REVOKED
    # Even the correct password can't revive a burned link.
    r = _submit(other, token, {"email": "owner@example.com", "password": PASSWORD})
    assert r.status_code == 400


# ── auto-bind ────────────────────────────────────────────────────────────────


def test_auto_bind_grants_without_password_for_single_admin(client):
    set_magic_link_settings({"owner_login": {"enabled": True, "auto_bind": True}})
    _bootstrap_admin(client)  # exactly one admin

    other = TestClient(create_api(MagicMock()), base_url="https://testserver")
    _, token = _mint(other, user_id="55555")
    state = other.get(f"/api/connect/{token}/state").json()
    assert state["mode"] == "grant"  # auto-bind → no password form
    r = _submit(other, token, {})
    assert r.status_code == 200 and _has_session_cookie(r)
    # Auto-bind persists the binding for next time.
    assert owner_login.get_binding("telegram:55555") is not None


def test_auto_bind_off_by_default_requires_password(client):
    _bootstrap_admin(client)  # auto_bind not set → default off
    other = TestClient(create_api(MagicMock()), base_url="https://testserver")
    _, token = _mint(other, user_id="44444")
    assert other.get(f"/api/connect/{token}/state").json()["mode"] == "login"


# ── redirect safety + stale binding ──────────────────────────────────────────


def test_hostile_section_falls_back_to_a_safe_path(client):
    _bootstrap_admin(client)
    _, token = _mint(client, section="https://evil.example/steal")
    r = _submit(client, token, {})
    assert r.json()["redirect"] == "/overview"  # never the attacker URL


def test_stale_binding_falls_back_to_login(client):
    _bootstrap_admin(client)
    # Point the binding at a user id that doesn't exist anymore.
    owner_login.set_binding("telegram:12345", user_id="ghost", email="g@x.com", via="password")
    _, token = _mint(client)
    # resolve_mode drops the stale binding → login form.
    assert client.get(f"/api/connect/{token}/state").json()["mode"] == "login"


# ── no leakage ───────────────────────────────────────────────────────────────


def test_no_session_token_or_password_in_the_link_store(client):
    _, token = _mint(client)
    _submit(client, token, {"email": "owner@example.com", "password": PASSWORD})
    raw = store.get_olano_paths().magic_links_store_path.read_text()
    assert PASSWORD not in raw
    # The link record is terminal but carries no session token.
    record = service.resolve_token(token)
    assert record.status == store.STATUS_DONE
    assert "raw_token" not in raw


def test_bindings_file_has_no_secret_material(client):
    _bootstrap_admin(client)
    raw = store.get_olano_paths().magic_links_bindings_path.read_text()
    assert PASSWORD not in raw
    # Only the mapping metadata — key, user id, email, when, how.
    binding = owner_login.get_binding("telegram:12345")
    assert set(binding) == {"user_id", "email", "bound_at", "bound_via"}


# ── admin bindings API ───────────────────────────────────────────────────────


def test_admin_can_list_and_revoke_bindings(client):
    _bootstrap_admin(client)
    listing = client.get("/api/magic-links/bindings").json()["bindings"]
    assert listing and listing[0]["email"] == "owner@example.com"
    key = listing[0]["key"]  # e.g. "telegram:12345" — the :path route matches the colon

    assert client.request("DELETE", f"/api/magic-links/bindings/{key}").status_code == 200
    assert owner_login.get_binding(key) is None
    # Next link for that identity must prove the password again.
    _, token = _mint(client)
    assert client.get(f"/api/connect/{token}/state").json()["mode"] == "login"
