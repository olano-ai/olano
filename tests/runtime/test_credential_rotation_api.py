"""Endpoint hooks for temporary-credential rotation (cloud-provisioned boxes).

Every path that can replace the deployment admin's sign-in password — self
change, admin-set, reset-link completion — plus a vault rekey must stamp the
local rotation record (deployment/credential_rotation.py) so the reporter can
tell the control plane to purge its temporary-password copy. Password changes
for other accounts must NOT stamp it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.deployment import credential_rotation as cr
from olano.runtime.api import create_api

ADMIN_EMAIL = "admin@x.com"


@pytest.fixture
def app(monkeypatch):
    # The box's provisioned sign-in (cloud_init env) — only this account's
    # rotation invalidates the control plane's temporary password.
    monkeypatch.setenv(cr.ADMIN_EMAIL_ENV, ADMIN_EMAIL)
    # Keep the fire-and-forget report out of these tests (no cloud to talk to);
    # the reporter itself is covered in tests/test_credential_rotation.py.
    monkeypatch.setattr(cr, "schedule_report", lambda home=None: None)
    runtime = MagicMock()
    runtime._runners = {}
    return create_api(runtime)


def _bootstrap(client: TestClient, email=ADMIN_EMAIL, password="temp-pw", name="Admin"):
    r = client.post(
        "/api/auth/bootstrap", json={"email": email, "password": password, "name": name}
    )
    assert r.status_code == 200
    return r


def _ack_pending() -> None:
    """Mark everything recorded so far as reported, the way a successful cloud
    POST would. Bootstrap itself now stamps an admin rotation (the owner chose
    their own password, so the provisioned one is dead) — acking it gives the
    tests below a clean baseline for the action they actually exercise."""
    state = cr.load_state()
    for rotated_key, reported_key in cr._FLAG_KEYS.values():
        if state.get(rotated_key):
            state[reported_key] = "2026-01-01T00:00:00.000000Z"
    cr._write_state(state)
    assert cr.has_pending() is False


def test_bootstrap_stamps_rotation(app):
    """The owner creating the first account IS the rotation: bootstrap only
    runs on an empty user table, so no account ever held the provisioned
    temporary password and the control plane should drop its copy."""
    admin = TestClient(app)
    _bootstrap(admin)
    assert cr.pending_flags(cr.load_state())["admin_password_rotated"] is True
    assert admin.get("/api/auth/me").json()["onboarding"]["admin_password_rotated"] is True


def test_bootstrap_stamps_even_when_the_owner_signs_up_under_another_email(app, monkeypatch):
    """The box is provisioned for one address; an owner who signs up under a
    different one still killed the temporary password."""
    monkeypatch.setenv(cr.ADMIN_EMAIL_ENV, "provisioned@x.com")
    admin = TestClient(app)
    _bootstrap(admin, email="someone@else.com")
    assert cr.pending_flags(cr.load_state())["admin_password_rotated"] is True


def test_admin_password_change_stamps_rotation(app):
    admin = TestClient(app)
    _bootstrap(admin)
    _ack_pending()

    r = admin.post(
        "/api/auth/password", json={"current_password": "temp-pw", "new_password": "my-own-pw"}
    )
    assert r.status_code == 200
    assert cr.pending_flags(cr.load_state())["admin_password_rotated"] is True


def test_wrong_current_password_does_not_stamp(app):
    admin = TestClient(app)
    _bootstrap(admin)
    _ack_pending()

    r = admin.post(
        "/api/auth/password", json={"current_password": "nope", "new_password": "my-own-pw"}
    )
    assert r.status_code == 403
    assert cr.has_pending() is False


def test_other_users_password_change_does_not_stamp(app):
    admin = TestClient(app)
    _bootstrap(admin)
    _ack_pending()
    admin.post("/api/admin/users", json={"email": "bob@x.com", "password": "pw", "name": "Bob"})

    member = TestClient(app)
    assert (
        member.post("/api/auth/login", json={"email": "bob@x.com", "password": "pw"}).status_code
        == 200
    )
    r = member.post("/api/auth/password", json={"current_password": "pw", "new_password": "pw2"})
    assert r.status_code == 200
    assert cr.has_pending() is False


def test_admin_set_password_on_deployment_admin_stamps(app):
    admin = TestClient(app)
    _bootstrap(admin)
    _ack_pending()
    user_id = admin.get("/api/auth/me").json()["user"]["id"]

    r = admin.patch(f"/api/admin/users/{user_id}", json={"password": "set-by-admin"})
    assert r.status_code == 200
    assert cr.pending_flags(cr.load_state())["admin_password_rotated"] is True


def test_reset_link_completion_stamps(app):
    admin = TestClient(app)
    _bootstrap(admin)
    _ack_pending()
    user_id = admin.get("/api/auth/me").json()["user"]["id"]

    reset = admin.post(f"/api/admin/users/{user_id}/reset")
    assert reset.status_code == 200
    token = reset.json()["reset"]["token"]

    fresh = TestClient(app)
    r = fresh.post(f"/api/auth/invite/{token}/accept", json={"password": "post-reset-pw"})
    assert r.status_code == 200
    assert cr.pending_flags(cr.load_state())["admin_password_rotated"] is True


def test_vault_rekey_stamps_vault_rotation(app):
    client = TestClient(app)  # single-user mode → implicit admin
    # Before: the wizard still has a vault half to show.
    assert client.get("/api/auth/me").json()["onboarding"]["vault_password_rotated"] is False
    r = client.post(
        "/api/vault/rekey", json={"old_passphrase": "temp-vault", "new_passphrase": "my-own-vault"}
    )
    assert r.status_code == 200
    flags = cr.pending_flags(cr.load_state())
    assert flags["vault_password_rotated"] is True
    assert flags["admin_password_rotated"] is False
    # After: /api/auth/me carries it, so the profile step drops that half.
    assert client.get("/api/auth/me").json()["onboarding"]["vault_password_rotated"] is True
