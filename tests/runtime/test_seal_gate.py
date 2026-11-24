"""The sealed volume's ceremony gate: ``GET /api/system/seal-gate``.

On a managed box with a sealed data volume the recovery-key ceremony is served
by the sealer itself on a unix socket, and Caddy proxies ``/api/seal/*`` to it
only after ``forward_auth`` has asked this route (``deploy/Caddyfile.allinone``).
The route must say yes for exactly one caller, a signed-in admin on a box that
has a sealed volume, and its yes is a 204 carrying the two headers the sealer
trusts. Everything else is a 401/403/404 that Caddy returns to the client
without ever touching the socket. The cases below are the whole decision table.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.runtime.api import create_api
from olano.runtime.seal_guard import SEAL_STATE_ENV

GATE = "/api/system/seal-gate"
_UUID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


@pytest.fixture
def app():
    runtime = MagicMock()
    runtime._runners = {}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    return create_api(runtime)


def _admin(app) -> TestClient:
    """Bootstrap the first (admin) account, which also turns multi-user mode on."""
    c = TestClient(app)
    r = c.post(
        "/api/auth/bootstrap", json={"email": "admin@x.com", "password": "pw", "name": "Admin"}
    )
    assert r.status_code == 200, r.text
    return c


def _member(app, admin: TestClient) -> TestClient:
    r = admin.post("/api/admin/users", json={"email": "bob@x.com", "password": "pw", "name": "Bob"})
    assert r.status_code == 200, r.text
    member = TestClient(app)
    login = member.post("/api/auth/login", json={"email": "bob@x.com", "password": "pw"})
    assert login.status_code == 200, login.text
    return member


def _sealed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **fields: object) -> Path:
    """Point the seal state at a file naming a sealed volume (the sealer's seal.json)."""
    state = tmp_path / "seal.json"
    payload: dict[str, object] = {"mode": "standard", "luks_uuid": _UUID, **fields}
    state.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv(SEAL_STATE_ENV, str(state))
    return state


def test_anonymous_is_refused_before_anything_is_read(app, tmp_path, monkeypatch) -> None:
    """No session: 401, and no gate header on the way out, sealed volume or not."""
    _admin(app)  # accounts exist, so the gate is enforced
    _sealed(tmp_path, monkeypatch)
    resp = TestClient(app).get(GATE)
    assert resp.status_code == 401
    assert resp.json() == {"detail": "auth_required"}
    assert "x-olano-seal-gate" not in resp.headers


def test_a_member_is_refused(app, tmp_path, monkeypatch) -> None:
    """A non-admin member may chat; the recovery key is the owner's alone."""
    admin = _admin(app)
    member = _member(app, admin)
    _sealed(tmp_path, monkeypatch)
    resp = member.get(GATE)
    assert resp.status_code == 403
    assert resp.json() == {"detail": "admin_required"}
    assert "x-olano-seal-gate" not in resp.headers


def test_an_admin_on_a_box_without_a_sealed_volume_gets_404(app) -> None:
    """Every legacy and self-hosted box: the autouse fixture points the seal state
    at a file that does not exist, so there is no socket to forward to."""
    admin = _admin(app)
    resp = admin.get(GATE)
    assert resp.status_code == 404
    assert resp.json() == {"detail": "no_sealed_volume"}
    assert "x-olano-seal-gate" not in resp.headers


def test_seal_state_without_a_volume_uuid_is_404_too(app, tmp_path, monkeypatch) -> None:
    admin = _admin(app)
    _sealed(tmp_path, monkeypatch, luks_uuid="")
    assert admin.get(GATE).status_code == 404


def test_an_admin_on_a_sealed_box_passes_with_the_headers_the_sealer_trusts(
    app, tmp_path, monkeypatch
) -> None:
    admin = _admin(app)
    _sealed(tmp_path, monkeypatch)
    me = admin.get("/api/auth/me")
    assert me.status_code == 200, me.text
    user_id = me.json()["user"]["id"]

    resp = admin.get(GATE)
    assert resp.status_code == 204, resp.text
    assert resp.content == b""
    assert resp.headers["x-olano-seal-gate"] == "ok"
    assert resp.headers["x-olano-seal-user"] == user_id
    # forward_auth's answer must never be served from a cache: the decision is
    # per request and per session.
    assert resp.headers["cache-control"] == "no-store"


def test_a_forged_gate_header_changes_nothing(app, tmp_path, monkeypatch) -> None:
    """The gate never reads the headers it sets. Caddy strips a client's copy
    before asking, and even a copy that reached the route would not help."""
    admin = _admin(app)
    forged = {"X-Olano-Seal-Gate": "ok", "X-Olano-Seal-User": "root"}
    assert TestClient(app).get(GATE, headers=forged).status_code == 401
    assert admin.get(GATE, headers=forged).status_code == 404  # no sealed volume yet
    _sealed(tmp_path, monkeypatch)
    resp = admin.get(GATE, headers={"X-Olano-Seal-Gate": "yes", "X-Olano-Seal-User": "root"})
    assert resp.status_code == 204
    assert resp.headers["x-olano-seal-gate"] == "ok"
    assert resp.headers["x-olano-seal-user"] != "root"


def test_single_user_mode_passes_as_the_implicit_owner(app, tmp_path, monkeypatch) -> None:
    """A box with no accounts at all (the classic self-hosted posture) has one
    implicit owner, and that owner is the admin the gate asks for."""
    _sealed(tmp_path, monkeypatch)
    resp = TestClient(app).get(GATE)
    assert resp.status_code == 204, resp.text
    assert resp.headers["x-olano-seal-gate"] == "ok"
    assert resp.headers["x-olano-seal-user"] == "owner"
