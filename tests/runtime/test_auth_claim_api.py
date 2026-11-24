"""POST /api/auth/claim — the owner takes over the provisioned admin account.

The onboarding profile step's account half. A managed box arrives with one
sign-in (handle ``admin``, the owner's email, a temporary password); the
claim rewrites name + handle + password on that row, revokes every other
session of the user, and stamps the password rotation — which is also what
closes the endpoint for any later call.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.deployment import credential_rotation as cr
from olano.runtime.api import create_api
from olano.runtime.auth import set_multi_user_enabled
from olano.users.db import PROVISIONED_ADMIN_HANDLE
from olano.users.store import get_user_store

OWNER_EMAIL = "owner@example.com"
TEMP_PW = "temp-password"


@pytest.fixture
def app(monkeypatch):
    # No cloud to report to; the reporter is covered in tests/test_credential_rotation.py.
    monkeypatch.setattr(cr, "schedule_report", lambda home=None: None)
    runtime = MagicMock()
    runtime._runners = {}
    return create_api(runtime)


def _provision(handle: str = PROVISIONED_ADMIN_HANDLE):
    """What `olano quick-setup` does on a managed box: the owner's email on a
    row whose handle is ``admin``, temporary password, no rotation recorded."""
    store = get_user_store()
    admin = store.bootstrap_admin(
        email=OWNER_EMAIL, password=TEMP_PW, name="Admin", username=handle
    )
    set_multi_user_enabled(True)
    return admin


def _login(client: TestClient, password: str = TEMP_PW) -> None:
    r = client.post("/api/auth/login", json={"email": OWNER_EMAIL, "password": password})
    assert r.status_code == 200, r.text


def test_claim_makes_the_provisioned_row_the_owners_account(app):
    _provision()
    first = TestClient(app)
    other = TestClient(app)  # a second browser on the temporary password
    _login(first)
    _login(other)
    assert other.get("/api/auth/me").status_code == 200

    r = first.post("/api/auth/claim", json={"name": "Carlos Rivera", "password": "my-own-pw"})
    assert r.status_code == 200, r.text
    user = r.json()["user"]
    assert user["email"] == OWNER_EMAIL  # the same account, not a second one
    assert user["name"] == "Carlos Rivera"
    assert user["username"] == "carlos-rivera"
    assert user["is_admin"] is True
    assert "password_hash" not in user

    # The claimer stays signed in; every other session is gone.
    me = first.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "carlos-rivera"
    assert other.get("/api/auth/me").status_code == 401

    # The new password is live, the temporary one is dead, and the box has
    # recorded the rotation for the control plane.
    fresh = TestClient(app)
    assert (
        fresh.post("/api/auth/login", json={"email": OWNER_EMAIL, "password": TEMP_PW}).status_code
        == 401
    )
    _login(fresh, "my-own-pw")
    assert cr.admin_password_rotated() is True
    assert me.json()["onboarding"]["admin_password_rotated"] is True


def test_claim_is_single_shot(app):
    _provision()
    client = TestClient(app)
    _login(client)
    assert (
        client.post("/api/auth/claim", json={"name": "Carlos", "password": "my-own-pw"}).status_code
        == 200
    )
    again = client.post("/api/auth/claim", json={"name": "Someone Else", "password": "another-pw"})
    assert again.status_code == 409
    assert again.json()["detail"] == "already_claimed"
    assert client.get("/api/auth/me").json()["user"]["name"] == "Carlos"


def test_claim_refuses_an_account_that_is_not_the_provisioned_one(app):
    """An owner who created their own account (any handle but ``admin``) has
    nothing to claim — and a rotation already recorded closes it too."""
    _provision(handle="carlos")
    client = TestClient(app)
    _login(client)
    r = client.post("/api/auth/claim", json={"name": "Carlos", "password": "my-own-pw"})
    assert r.status_code == 409
    assert r.json()["detail"] == "already_claimed"


def test_claim_validates_name_and_password(app):
    _provision()
    client = TestClient(app)
    _login(client)
    short = client.post("/api/auth/claim", json={"name": "Carlos", "password": "short"})
    assert short.status_code == 400
    assert short.json()["detail"] == "password_short"
    blank = client.post("/api/auth/claim", json={"name": "  ", "password": "long-enough-pw"})
    assert blank.status_code == 400
    assert blank.json()["detail"] == "name_required"
    # Nothing moved.
    assert client.get("/api/auth/me").json()["user"]["username"] == PROVISIONED_ADMIN_HANDLE
    assert cr.admin_password_rotated() is False


def test_claim_is_admin_only(app):
    _provision()
    admin = TestClient(app)
    _login(admin)
    admin.post("/api/admin/users", json={"email": "bob@x.com", "password": "pw", "name": "Bob"})
    member = TestClient(app)
    assert (
        member.post("/api/auth/login", json={"email": "bob@x.com", "password": "pw"}).status_code
        == 200
    )
    r = member.post("/api/auth/claim", json={"name": "Bob", "password": "long-enough-pw"})
    assert r.status_code == 403
    anon = TestClient(app)
    assert (
        anon.post("/api/auth/claim", json={"name": "X", "password": "long-enough-pw"}).status_code
        == 401
    )
