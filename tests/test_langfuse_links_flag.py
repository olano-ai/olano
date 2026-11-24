"""The per-user ``langfuse_links`` flag: who sees "Langfuse" links in the dashboard.

**Off by default, existing accounts included.** A new account and a row
written before the column existed both read as off, so turning tracing on
does not put a debugging link under every admin's chat replies.

**An admin sets it per person** through ``PATCH /api/admin/users/{id}`` (the
Admin -> Users table), and ``/api/auth/me`` carries the resolved value the
dashboard gates the links on. A single-user install has no users table to set
it in, so there it resolves on.

**It is display only.** The links route stays admin-only whatever the flag
says, so a member with the flag set still gets a refusal.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from olano.runtime import auth as auth_layer
from olano.runtime.api import create_api
from olano.users.db import UsersDB
from olano.users.store import get_user_store, reset_user_store

_OLD_SCHEMA = """
CREATE TABLE users (
    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT,
    password_hash TEXT NOT NULL, is_admin INTEGER DEFAULT 0,
    is_active INTEGER DEFAULT 1, can_terminal INTEGER DEFAULT 0,
    timezone TEXT DEFAULT 'UTC', created_at TEXT NOT NULL)
"""


def test_a_pre_migration_row_reads_as_off(tmp_path: Path):
    path = tmp_path / "legacy-users.db"
    conn = sqlite3.connect(str(path))
    conn.execute(_OLD_SCHEMA)
    conn.execute("INSERT INTO users VALUES ('u1','a@b.c','Ada','h',1,1,0,'UTC','2026-01-01')")
    conn.commit()
    conn.close()
    db = UsersDB(path)
    user = db.get_user_by_id("u1")
    assert user is not None and user.langfuse_links is False
    assert user.public()["langfuse_links"] is False
    # Re-opening must not fail on the second ALTER.
    assert UsersDB(path).get_user_by_id("u1") is not None


def test_a_new_account_is_off_and_the_flag_round_trips(tmp_path: Path):
    db = UsersDB(tmp_path / "users.db")
    user = db.create_user(email="new@b.c", password_hash="h", is_admin=True)
    assert db.get_user_by_id(user.id).langfuse_links is False
    db.update_user(user.id, langfuse_links=True)
    assert db.get_user_by_id(user.id).langfuse_links is True
    db.update_user(user.id, langfuse_links=False)
    assert db.get_user_by_id(user.id).langfuse_links is False


def test_single_user_install_shows_the_links():
    auth_layer.set_multi_user_enabled(False)
    assert auth_layer.resolve_langfuse_links() is True


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


def test_an_admin_turns_it_on_per_person_and_me_follows():
    client, store, admin = _client()
    _login(client)
    assert client.get("/api/auth/me").json()["langfuse_links"] is False

    res = client.patch(f"/api/admin/users/{admin.id}", json={"langfuse_links": True})
    assert res.status_code == 200
    assert res.json()["user"]["langfuse_links"] is True
    assert client.get("/api/auth/me").json()["langfuse_links"] is True

    # Changing another field leaves it alone.
    client.patch(f"/api/admin/users/{admin.id}", json={"title": "Ops"})
    assert store.db.get_user_by_id(admin.id).langfuse_links is True


def test_status_capabilities_never_carry_it():
    """/api/status is public and cached process-wide: no per-user values."""
    client, _store, _admin = _client()
    _login(client)
    caps = client.get("/api/status").json().get("capabilities", {})
    assert "langfuse_links" not in caps


def test_the_flag_grants_a_member_nothing():
    client, store, _admin = _client()
    member = store.create_user(email="m@x.com", password="pw", name="Mem")
    store.db.update_user(member.id, langfuse_links=True)
    _login(client, "m@x.com", "pw")
    res = client.get("/api/observability/langfuse/links?agent=merli")
    assert res.status_code in (401, 403)
    # And a member cannot set it on themselves.
    res = client.patch(f"/api/admin/users/{member.id}", json={"langfuse_links": False})
    assert res.status_code in (401, 403)


def test_the_admin_tool_helper_accepts_it():
    from olano.admin.users import update_user

    _client()
    store = get_user_store()
    member = store.create_user(email="m2@x.com", password="pw", name="Mem Two")
    out = update_user(member.email, {"langfuse_links": 1})
    assert out["user"]["langfuse_links"] is True
