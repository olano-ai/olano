"""Integration tests for the multi-user auth + admin endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.runtime.api import create_api


@pytest.fixture
def app():
    runtime = MagicMock()
    runtime._runners = {}
    return create_api(runtime)


def _bootstrap(client: TestClient, email="admin@x.com", password="pw", name="Admin"):
    return client.post(
        "/api/auth/bootstrap", json={"email": email, "password": password, "name": name}
    )


def test_bootstrap_login_me_logout_flow(app):
    admin = TestClient(app)
    # First-run (single-user, no users) → bootstrap is reachable and creates admin.
    r = _bootstrap(admin)
    assert r.status_code == 200
    assert r.json()["user"]["is_admin"] is True

    me = admin.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "admin@x.com"
    assert me.json()["multi_user"] is True

    assert admin.post("/api/auth/logout").status_code == 200
    after = admin.get("/api/auth/me")
    assert after.status_code == 401
    assert after.json()["detail"] == "auth_required"


def test_bootstrap_is_single_shot(app):
    admin = TestClient(app)
    assert _bootstrap(admin).status_code == 200
    assert _bootstrap(admin).status_code == 409  # already_initialized


def test_admin_user_crud_and_duplicate(app):
    admin = TestClient(app)
    _bootstrap(admin)

    created = admin.post(
        "/api/admin/users", json={"email": "bob@x.com", "password": "pw", "name": "Bob"}
    )
    assert created.status_code == 200
    assert created.json()["user"]["is_admin"] is False

    assert len(admin.get("/api/admin/users").json()) == 2
    dup = admin.post("/api/admin/users", json={"email": "BOB@x.com", "password": "pw"})
    assert dup.status_code == 409


def test_non_admin_is_forbidden_from_admin_routes(app):
    admin = TestClient(app)
    _bootstrap(admin)
    admin.post("/api/admin/users", json={"email": "bob@x.com", "password": "pw"})

    member = TestClient(app)
    assert (
        member.post("/api/auth/login", json={"email": "bob@x.com", "password": "pw"}).status_code
        == 200
    )
    assert member.get("/api/auth/me").json()["user"]["is_admin"] is False
    assert member.get("/api/admin/users").status_code == 403  # admin_required


def test_unauthenticated_gating(app):
    admin = TestClient(app)
    _bootstrap(admin)

    anon = TestClient(app)  # no session cookie
    # Protected /api/ paths are gated to 401 before reaching the handler.
    gated = anon.get("/api/admin/users")
    assert gated.status_code == 401
    assert gated.json()["detail"] == "auth_required"
    # Public paths remain reachable.
    assert anon.get("/api/status").status_code == 200
    bad_login = anon.post("/api/auth/login", json={"email": "nope@x.com", "password": "x"})
    assert bad_login.status_code == 401
    assert bad_login.json()["detail"] == "invalid_credentials"


def test_cannot_delete_or_demote_last_admin(app):
    admin = TestClient(app)
    admin_id = _bootstrap(admin).json()["user"]["id"]

    demote = admin.patch(f"/api/admin/users/{admin_id}", json={"is_admin": False})
    assert demote.status_code == 409  # last_admin
    assert admin.delete(f"/api/admin/users/{admin_id}").status_code == 409


def test_single_user_mode_has_no_gate(app):
    # No bootstrap → multi_user stays off → status reachable, me resolves owner.
    client = TestClient(app)
    assert client.get("/api/status").status_code == 200
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["is_admin"] is True  # implicit owner/admin
    assert me.json()["multi_user"] is False


def test_prefs_round_trip_and_merge(app):
    admin = TestClient(app)
    _bootstrap(admin)

    # Empty by default.
    assert admin.get("/api/auth/me/prefs").json()["prefs"] == {}

    # PUT writes; the response echoes the stored blob.
    put = admin.put("/api/auth/me/prefs", json={"prefs": {"nav_order": ["/chat", "/overview"]}})
    assert put.status_code == 200
    assert put.json()["prefs"]["nav_order"] == ["/chat", "/overview"]
    assert admin.get("/api/auth/me/prefs").json()["prefs"]["nav_order"] == ["/chat", "/overview"]

    # A second PUT merges — it updates nav_order without dropping unrelated keys.
    admin.put("/api/auth/me/prefs", json={"prefs": {"theme_density": "compact"}})
    merged = admin.put("/api/auth/me/prefs", json={"prefs": {"nav_order": ["/agents"]}}).json()
    assert merged["prefs"] == {"nav_order": ["/agents"], "theme_density": "compact"}


def test_prefs_isolated_per_user(app):
    admin = TestClient(app)
    _bootstrap(admin)
    admin.post("/api/admin/users", json={"email": "bob@x.com", "password": "pw"})
    admin.put("/api/auth/me/prefs", json={"prefs": {"nav_order": ["/admin"]}})

    member = TestClient(app)
    member.post("/api/auth/login", json={"email": "bob@x.com", "password": "pw"})
    member.put("/api/auth/me/prefs", json={"prefs": {"nav_order": ["/chat"]}})

    assert admin.get("/api/auth/me/prefs").json()["prefs"]["nav_order"] == ["/admin"]
    assert member.get("/api/auth/me/prefs").json()["prefs"]["nav_order"] == ["/chat"]


def test_prefs_require_auth_when_multi_user(app):
    admin = TestClient(app)
    _bootstrap(admin)
    anon = TestClient(app)  # no session cookie
    assert anon.get("/api/auth/me/prefs").status_code == 401


def test_prefs_single_user_owner(app):
    # No bootstrap → owner principal persists prefs under id "owner".
    client = TestClient(app)
    client.put("/api/auth/me/prefs", json={"prefs": {"nav_order": ["/files"]}})
    assert client.get("/api/auth/me/prefs").json()["prefs"]["nav_order"] == ["/files"]


def test_oauth_callbacks_reachable_when_gated(app):
    """OAuth provider redirects arrive from the provider's browser redirect with
    no session cookie — the multi-user gate must exempt
    GET /api/auth/{service}/callback (and only the callback)."""
    admin = TestClient(app)
    _bootstrap(admin)  # turns multi-user mode on

    anon = TestClient(app)  # fresh client: no cookies at all
    google_cb = anon.get("/api/auth/google/callback", params={"state": "nope"})
    assert google_cb.status_code == 200  # reachable, not 401
    assert google_cb.headers["content-type"].startswith("text/html")
    assert "Authorization session not found" in google_cb.text

    broker_cb = anon.get("/api/auth/microsoft365/callback", params={"state": "nope"})
    assert broker_cb.status_code == 200
    assert "Authorization session not found" in broker_cb.text

    # Every other service-auth route stays gated.
    start = anon.post("/api/auth/microsoft365/start", json={})
    assert start.status_code == 401
    assert start.json()["detail"] == "auth_required"
