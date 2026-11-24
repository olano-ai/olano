"""Invite-based onboarding: admin invites by email → user sets own password."""

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


def _admin(app) -> TestClient:
    c = TestClient(app)
    c.post("/api/auth/bootstrap", json={"email": "admin@x.com", "password": "pw", "name": "Admin"})
    return c


def test_invite_accept_flow(app):
    admin = _admin(app)
    # Invite (no password) → pending user + token.
    r = admin.post("/api/admin/users", json={"email": "bob@x.com", "name": "Bob"})
    assert r.status_code == 200
    inv = r.json()["invite"]
    assert inv and inv["token"] and inv["path"] == f"/invite/{inv['token']}"
    token = inv["token"]

    anon = TestClient(app)
    # Pending account cannot log in yet.
    assert (
        anon.post("/api/auth/login", json={"email": "bob@x.com", "password": "x"}).status_code
        == 401
    )
    # Invite info is public (no session).
    info = anon.get(f"/api/auth/invite/{token}")
    assert info.status_code == 200 and info.json()["email"] == "bob@x.com"
    # Accept → activates, sets the user's own password, issues a session.
    acc = anon.post(f"/api/auth/invite/{token}/accept", json={"password": "bobsecret"})
    assert acc.status_code == 200 and acc.json()["user"]["email"] == "bob@x.com"
    assert anon.get("/api/auth/me").json()["user"]["email"] == "bob@x.com"
    # Token is single-use.
    assert anon.get(f"/api/auth/invite/{token}").status_code == 404
    # And bob can now log in normally with the password he chose.
    fresh = TestClient(app)
    assert (
        fresh.post(
            "/api/auth/login", json={"email": "bob@x.com", "password": "bobsecret"}
        ).status_code
        == 200
    )


def test_invalid_or_expired_token(app):
    _admin(app)  # users exist → gate active
    anon = TestClient(app)
    assert anon.get("/api/auth/invite/nope").status_code == 404
    assert anon.post("/api/auth/invite/nope/accept", json={"password": "x"}).status_code == 404


def test_reinvite_pending_only(app):
    admin = _admin(app)
    pending_id = admin.post("/api/admin/users", json={"email": "c@x.com"}).json()["user"]["id"]
    assert admin.post(f"/api/admin/users/{pending_id}/invite").json()["invite"]["token"]
    # Re-inviting an already-active user is a conflict.
    active_id = admin.post("/api/admin/users", json={"email": "d@x.com", "password": "pw"}).json()[
        "user"
    ]["id"]
    assert admin.post(f"/api/admin/users/{active_id}/invite").status_code == 409


def test_pending_user_shows_inactive(app):
    admin = _admin(app)
    admin.post("/api/admin/users", json={"email": "e@x.com"})
    users = admin.get("/api/admin/users").json()
    bob = next(u for u in users if u["email"] == "e@x.com")
    assert bob["is_active"] is False


def test_admin_reset_password(app):
    admin = _admin(app)
    uid = admin.post("/api/admin/users", json={"email": "f@x.com", "password": "old"}).json()[
        "user"
    ]["id"]
    # Admin generates a reset link for an ACTIVE user.
    r = admin.post(f"/api/admin/users/{uid}/reset")
    assert r.status_code == 200
    token = r.json()["reset"]["token"]
    # User opens the link and sets a new password.
    anon = TestClient(app)
    assert anon.get(f"/api/auth/invite/{token}").json()["email"] == "f@x.com"
    assert (
        anon.post(f"/api/auth/invite/{token}/accept", json={"password": "newpw"}).status_code == 200
    )
    # Old password no longer works; new one does.
    fresh = TestClient(app)
    assert (
        fresh.post("/api/auth/login", json={"email": "f@x.com", "password": "old"}).status_code
        == 401
    )
    assert (
        fresh.post("/api/auth/login", json={"email": "f@x.com", "password": "newpw"}).status_code
        == 200
    )


def test_change_own_password(app):
    admin = _admin(app)
    admin.post("/api/admin/users", json={"email": "g@x.com", "password": "old"})
    user = TestClient(app)
    user.post("/api/auth/login", json={"email": "g@x.com", "password": "old"})
    # Wrong current password → 403.
    assert (
        user.post(
            "/api/auth/password", json={"current_password": "nope", "new_password": "x"}
        ).status_code
        == 403
    )
    # Correct current → 200, and the new password works on a fresh login.
    assert (
        user.post(
            "/api/auth/password", json={"current_password": "old", "new_password": "brandnew"}
        ).status_code
        == 200
    )
    fresh = TestClient(app)
    assert (
        fresh.post("/api/auth/login", json={"email": "g@x.com", "password": "brandnew"}).status_code
        == 200
    )
