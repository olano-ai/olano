"""The handle rule at the API: explicit is refused, auto is suffixed, probes work.

An explicitly typed handle is a permanent identity someone chose — handing
them ``carlos-m-2`` silently when they asked for ``carlos-m`` is worse than
refusing. An auto-minted one (empty field) was never chosen, so a quiet
suffix is fine. The availability endpoint exists so forms can tell the user
*before* submit; its gate exists so it can't be used to enumerate accounts.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.runtime.api import create_api
from olano.users.store import get_user_store, reset_user_store


@pytest.fixture(autouse=True)
def _plain_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LIONCLAW_PUBLIC_URL", raising=False)
    monkeypatch.delenv("LIONCLAW_SECURE_COOKIES", raising=False)


def _fresh_client() -> TestClient:
    reset_user_store()
    runtime = MagicMock()
    runtime._runners = {}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    return TestClient(create_api(runtime))


def _bootstrap(client: TestClient, **extra) -> dict:
    body = {"email": "owner@x.com", "password": "secret123", "name": "Carlos M.", **extra}
    resp = client.post("/api/auth/bootstrap", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["user"]


# ------------------------------------------------------------- availability


def test_probe_is_public_only_while_no_users_exist():
    client = _fresh_client()
    res = client.get("/api/auth/username-available", params={"username": "Carlos M."})
    assert res.status_code == 200
    assert res.json() == {"available": True, "normalized": "carlos-m", "suggestion": "carlos-m"}


def test_probe_requires_a_session_once_accounts_exist():
    """After setup an open probe would leak who has an account here."""
    client = _fresh_client()
    _bootstrap(client)
    anonymous = TestClient(create_api(MagicMock(_runners={}, registry=MagicMock())))
    res = anonymous.get("/api/auth/username-available", params={"username": "carlos-m"})
    assert res.status_code == 401

    # The bootstrap cookie on the original client still probes fine.
    res = client.get("/api/auth/username-available", params={"username": "carlos-m"})
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is False
    assert body["suggestion"] == "carlos-m-2"


def test_probe_reports_the_normalized_form():
    """The form shows "saved as carlos-m" so the preview can never disagree
    with the eventual insert — same slugify, same mint."""
    client = _fresh_client()
    _bootstrap(client)
    res = client.get("/api/auth/username-available", params={"username": "Dana Ray!"})
    assert res.json()["normalized"] == "dana-ray"


# ----------------------------------------------------------------- refusals


def test_explicitly_typed_taken_handle_is_a_409_not_a_suffix():
    client = _fresh_client()
    _bootstrap(client, username="carlos-m")
    res = client.post(
        "/api/admin/users",
        json={"email": "d@x.com", "password": "secret123", "name": "Dana", "username": "carlos-m"},
    )
    assert res.status_code == 409
    assert res.json()["detail"] == "username_exists"


def test_taken_email_is_still_email_exists():
    """The two 409s must never be confused — each drives different form copy."""
    client = _fresh_client()
    _bootstrap(client)
    res = client.post(
        "/api/admin/users", json={"email": "owner@x.com", "password": "secret123", "name": "X"}
    )
    assert res.status_code == 409
    assert res.json()["detail"] == "email_exists"


def test_auto_minted_collision_still_suffixes_quietly():
    """Nobody chose the auto handle, so a suffix is fine there."""
    client = _fresh_client()
    _bootstrap(client)  # name "Carlos M." → carlos-m
    res = client.post(
        "/api/admin/users", json={"email": "d@x.com", "password": "secret123", "name": "carlos m"}
    )
    assert res.status_code == 200
    assert res.json()["user"]["username"] == "carlos-m-2"


def test_invite_flow_also_refuses_a_taken_explicit_handle():
    client = _fresh_client()
    _bootstrap(client, username="carlos-m")
    # No password → the invite branch of the same route.
    res = client.post(
        "/api/admin/users", json={"email": "d@x.com", "name": "Dana", "username": "carlos-m"}
    )
    assert res.status_code == 409
    assert res.json()["detail"] == "username_exists"


# ------------------------------------------------------------- never empty


def test_bootstrap_fills_both_halves_from_almost_nothing():
    """PasswordStep-style bootstrap: email + password only. The account still
    comes out with a handle and a presentable name — never blank."""
    client = _fresh_client()
    user = _bootstrap(client, name="", email="dana.ray@x.com")
    assert user["username"] == "dana-ray"
    assert user["name"] == "Dana Ray"


def test_blank_name_patch_is_refused():
    client = _fresh_client()
    user = _bootstrap(client)
    res = client.patch(f"/api/admin/users/{user['id']}", json={"name": "   "})
    assert res.status_code == 422
    assert res.json()["detail"] == "name_required"
    fetched = get_user_store().db.get_user_by_id(user["id"])
    assert fetched is not None and fetched.name == "Carlos M."


def test_invite_info_carries_the_minted_handle():
    """The accept screen is where an invitee first learns their handle."""
    client = _fresh_client()
    _bootstrap(client)
    res = client.post("/api/admin/users", json={"email": "d@x.com", "name": "Dana Ray"})
    assert res.status_code == 200, res.text
    token = res.json()["invite"]["token"]
    info = client.get(f"/api/auth/invite/{token}")
    assert info.status_code == 200
    assert info.json()["username"] == "dana-ray"
