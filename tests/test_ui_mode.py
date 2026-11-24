"""The ``ui_mode`` knob: which dashboard shell a human gets.

Three properties are load-bearing and each has a test here.

**It is its own knob.** The mode is read from the user record and nowhere
else — never derived from ``is_admin`` — so an admin can be put in the simple
shell and a member left in the full dashboard. A derived default was the
original design and was rejected precisely because it made those two cases
unrepresentable.

**Upgrading changes nobody's screen.** A row written before the column
existed, an unset value, and a value this build does not recognise all read as
``advanced`` — the surface every account already had. The column is added by
an idempotent ``ALTER`` with that same default, so a live box upgrades in
place.

**It is not a permission.** ``resolve_ui_mode`` is presentation only.
``is_admin``, ``_member_forbidden`` and ``assert_agent_access`` are untouched
by it, so reaching the other shell would grant nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.runtime import auth as auth_layer
from olano.runtime.api import create_api
from olano.users.db import DEFAULT_UI_MODE, UsersDB, normalize_ui_mode
from olano.users.store import get_user_store, reset_user_store

# ── the normalizer ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["advanced", "simple"])
def test_known_modes_pass_through(value: str):
    assert normalize_ui_mode(value) == value


@pytest.mark.parametrize("value", [None, "", "ADVANCED", "Simple", "wat", 0, 1, True, []])
def test_unknown_values_read_as_advanced(value: object):
    """Never blank the UI on a bad value — fall back to what everyone had."""
    assert normalize_ui_mode(value) == "advanced"
    assert DEFAULT_UI_MODE == "advanced"


# ── the column, and upgrading a live box ────────────────────────────────────

_OLD_SCHEMA = """
CREATE TABLE users (
    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT,
    password_hash TEXT NOT NULL, is_admin INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1, can_terminal INTEGER DEFAULT 0,
    timezone TEXT DEFAULT 'UTC', created_at TEXT NOT NULL)
"""


def _legacy_db(tmp_path: Path) -> Path:
    """A users.db exactly as it looked before ui_mode existed."""
    path = tmp_path / "legacy-users.db"
    conn = sqlite3.connect(str(path))
    conn.execute(_OLD_SCHEMA)
    conn.execute("INSERT INTO users VALUES ('u1','a@b.c','Ada','h',1,1,0,'UTC','2026-01-01')")
    conn.execute("INSERT INTO users VALUES ('u2','m@b.c','Mem','h',0,1,0,'UTC','2026-01-01')")
    conn.commit()
    conn.close()
    return path


def test_pre_migration_rows_read_as_advanced(tmp_path: Path):
    """The upgrade must not move a single existing human to another shell."""
    db = UsersDB(_legacy_db(tmp_path))
    for uid in ("u1", "u2"):  # the admin and the member alike
        user = db.get_user_by_id(uid)
        assert user is not None
        assert user.ui_mode == "advanced"
        assert user.public()["ui_mode"] == "advanced"


def test_migration_is_idempotent(tmp_path: Path):
    """Re-opening the DB must not fail on the second ALTER."""
    path = _legacy_db(tmp_path)
    UsersDB(path)
    again = UsersDB(path)
    assert again.get_user_by_id("u1") is not None


def test_new_user_defaults_to_advanced(tmp_path: Path):
    db = UsersDB(tmp_path / "users.db")
    user = db.create_user(email="new@b.c", password_hash="h")
    assert user.ui_mode == "advanced"
    assert db.get_user_by_id(user.id).ui_mode == "advanced"


def test_mode_is_independent_of_is_admin(tmp_path: Path):
    """The whole reason this is a column and not a derivation."""
    db = UsersDB(_legacy_db(tmp_path))
    db.update_user("u1", ui_mode="simple")  # an ADMIN in the simple shell
    db.update_user("u2", ui_mode="advanced")  # a MEMBER in the full dashboard
    assert db.get_user_by_id("u1").is_admin is True
    assert db.get_user_by_id("u1").ui_mode == "simple"
    assert db.get_user_by_id("u2").is_admin is False
    assert db.get_user_by_id("u2").ui_mode == "advanced"


def test_invalid_write_clamps_rather_than_persisting(tmp_path: Path):
    db = UsersDB(tmp_path / "users.db")
    user = db.create_user(email="x@b.c", password_hash="h", ui_mode="nonsense")
    assert user.ui_mode == "advanced"
    db.update_user(user.id, ui_mode="simple")
    db.update_user(user.id, ui_mode="garbage")
    assert db.get_user_by_id(user.id).ui_mode == "advanced"


# ── resolve_ui_mode ─────────────────────────────────────────────────────────


def test_single_user_install_defaults_to_advanced():
    """No users table to read: the implicit owner keeps the full dashboard."""
    auth_layer.set_multi_user_enabled(False)
    assert auth_layer.resolve_ui_mode() == "advanced"


def test_single_user_install_can_opt_into_simple():
    """A solo operator can pick the simple shell without multi-user mode."""
    from olano.config.home import ensure_olano_home, update_home_config

    auth_layer.set_multi_user_enabled(False)
    ensure_olano_home()
    update_home_config({"ui_mode": "simple"})
    assert auth_layer.resolve_ui_mode() == "simple"


def test_resolve_reads_the_user_record():
    reset_user_store()
    store = get_user_store()
    auth_layer.set_multi_user_enabled(True)
    member = store.create_user(email="m@x.com", password="pw", name="Mem")
    assert auth_layer.resolve_ui_mode(member) == "advanced"
    store.db.update_user(member.id, ui_mode="simple")
    assert auth_layer.resolve_ui_mode(store.db.get_user_by_id(member.id)) == "simple"


# ── the wire ────────────────────────────────────────────────────────────────


def _client():
    reset_user_store()
    store = get_user_store()
    admin = store.create_user(email="admin@x.com", password="pw", name="Root", is_admin=True)
    auth_layer.set_multi_user_enabled(True)
    runtime = MagicMock()
    runtime._runners = {}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    return TestClient(create_api(runtime)), store, admin


def _login(client: TestClient, email: str = "admin@x.com", password: str = "pw"):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def test_me_reports_the_resolved_mode():
    """The dashboard forks on this field, so it must ride /api/auth/me."""
    client, store, admin = _client()
    _login(client)
    body = client.get("/api/auth/me").json()
    assert body["ui_mode"] == "advanced"
    assert body["user"]["ui_mode"] == "advanced"

    store.db.update_user(admin.id, ui_mode="simple")
    body = client.get("/api/auth/me").json()
    assert body["ui_mode"] == "simple"


def test_status_capabilities_never_carry_the_mode():
    """/api/status is public and cached process-wide for 5s — a per-user value
    there would be served to the next, different human."""
    client, _store, _admin = _client()
    _login(client)
    caps = client.get("/api/status").json().get("capabilities", {})
    assert "ui_mode" not in caps
    assert "ui_simple" not in caps


def test_admin_can_set_another_users_mode():
    client, store, _admin = _client()
    member = store.create_user(email="m@x.com", password="pw", name="Mem")
    _login(client)
    res = client.patch(f"/api/admin/users/{member.id}", json={"ui_mode": "simple"})
    assert res.status_code == 200
    assert res.json()["user"]["ui_mode"] == "simple"
    assert store.db.get_user_by_id(member.id).ui_mode == "simple"


def test_patch_rejects_an_unrecognized_mode():
    """Loud, so a typo in an admin script is visible instead of silently
    leaving the user where they were."""
    client, store, _admin = _client()
    member = store.create_user(email="m@x.com", password="pw", name="Mem")
    _login(client)
    res = client.patch(f"/api/admin/users/{member.id}", json={"ui_mode": "grok"})
    assert res.status_code == 422
    assert store.db.get_user_by_id(member.id).ui_mode == "advanced"


def test_member_cannot_change_their_own_mode():
    """Admin-assigned only: the admin surface is the only way in."""
    client, store, _admin = _client()
    member = store.create_user(email="m@x.com", password="pw", name="Mem")
    _login(client, "m@x.com", "pw")
    res = client.patch(f"/api/admin/users/{member.id}", json={"ui_mode": "simple"})
    assert res.status_code in (401, 403)
    assert store.db.get_user_by_id(member.id).ui_mode == "advanced"


def test_quick_setup_rebuild_preserves_the_mode():
    """`save_home_config` writes the whole dataclass via asdict(), so any
    HomeConfig rebuilt from a subset of fields silently resets the rest. The
    quick-setup path rebuilds one; a single-user operator on the simple shell
    must not be put back on the dashboard by an unrelated agent change."""
    from olano.config.home import HomeConfig, load_home_config, save_home_config

    save_home_config(HomeConfig(default_agent="merli", ui_mode="simple"))
    assert load_home_config().ui_mode == "simple"

    before = load_home_config()
    save_home_config(
        HomeConfig(
            default_agent="other",
            preferred_model=before.preferred_model,
            runtime_api_port=before.runtime_api_port,
            dashboard_port=before.dashboard_port,
            multi_user_enabled=before.multi_user_enabled,
            ui_mode=before.ui_mode,
            extra_bind_roots=before.extra_bind_roots,
            archetype_dirs=before.archetype_dirs,
        )
    )
    assert load_home_config().ui_mode == "simple"


# ── self-service switching (admins only) ────────────────────────────────────


def test_admin_can_switch_their_own_view():
    client, store, admin = _client()
    _login(client)
    res = client.put("/api/auth/me/ui-mode", json={"ui_mode": "simple"})
    assert res.status_code == 200
    assert res.json()["ui_mode"] == "simple"
    assert store.db.get_user_by_id(admin.id).ui_mode == "simple"
    # And the resolved mode on /api/auth/me follows immediately, which is what
    # lets the client remount the other shell without a reload.
    assert client.get("/api/auth/me").json()["ui_mode"] == "simple"


def test_an_admin_in_the_simple_view_can_always_get_back():
    """No state an admin cannot return from.

    The switch renders in BOTH shells for an admin precisely so that switching
    into the simple view is not a one-way door — the way back must not require
    another admin, or a config file, or SSH.
    """
    client, store, admin = _client()
    _login(client)
    client.put("/api/auth/me/ui-mode", json={"ui_mode": "simple"})
    assert store.db.get_user_by_id(admin.id).ui_mode == "simple"
    res = client.put("/api/auth/me/ui-mode", json={"ui_mode": "advanced"})
    assert res.status_code == 200
    assert store.db.get_user_by_id(admin.id).ui_mode == "advanced"


def test_a_member_cannot_switch_their_own_view():
    """The asymmetry that makes the mode an operator decision rather than a
    preference. The reason is specific so the UI can explain it."""
    client, store, _admin = _client()
    member = store.create_user(email="m@x.com", password="pw", name="Mem")
    store.db.update_user(member.id, ui_mode="simple")
    _login(client, "m@x.com", "pw")
    res = client.put("/api/auth/me/ui-mode", json={"ui_mode": "advanced"})
    assert res.status_code == 403
    assert res.json()["detail"] == "ui_mode_admin_only"
    assert store.db.get_user_by_id(member.id).ui_mode == "simple"


def test_self_switch_rejects_an_unrecognized_mode():
    client, store, admin = _client()
    _login(client)
    res = client.put("/api/auth/me/ui-mode", json={"ui_mode": "grok"})
    assert res.status_code == 422
    assert store.db.get_user_by_id(admin.id).ui_mode == "advanced"


def test_self_switch_and_admin_assignment_write_the_same_column():
    """One notion of "mode", not two. A self-switch must be indistinguishable
    from an assignment afterwards, or the two surfaces drift."""
    client, store, admin = _client()
    _login(client)
    client.put("/api/auth/me/ui-mode", json={"ui_mode": "simple"})
    via_self = store.db.get_user_by_id(admin.id).ui_mode
    client.patch(f"/api/admin/users/{admin.id}", json={"ui_mode": "advanced"})
    client.put("/api/auth/me/ui-mode", json={"ui_mode": "simple"})
    assert store.db.get_user_by_id(admin.id).ui_mode == via_self == "simple"


def test_single_user_self_switch_actually_persists():
    """The review's finding: on single-user installs the switch wrote a users
    table row that does not exist ("owner" matches nothing), answered 200,
    and the next /api/auth/me reverted the shell. The mode lives in
    config.yaml there, so that is what the endpoint must write."""
    from olano.config.home import ensure_olano_home

    reset_user_store()
    auth_layer.set_multi_user_enabled(False)
    ensure_olano_home()
    runtime = MagicMock()
    runtime._runners = {}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    client = TestClient(create_api(runtime))

    res = client.put("/api/auth/me/ui-mode", json={"ui_mode": "simple"})
    assert res.status_code == 200
    assert auth_layer.resolve_ui_mode() == "simple"
    assert client.get("/api/auth/me").json()["ui_mode"] == "simple"

    # And back — "no state an admin cannot return from" on every install kind.
    client.put("/api/auth/me/ui-mode", json={"ui_mode": "advanced"})
    assert auth_layer.resolve_ui_mode() == "advanced"
