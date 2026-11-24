"""Tests for the SQLite users/sessions/access store."""

from __future__ import annotations

import pytest

from olano.users.db import DuplicateEmailError, UsersDB
from olano.users.passwords import hash_password, hash_token, new_session_token


@pytest.fixture
def db() -> UsersDB:
    # Uses the autouse `lionclaw_home` fixture → isolated tmp users.db.
    return UsersDB()


# ----------------------------------------------------------------- users


def test_create_and_fetch_user(db: UsersDB):
    u = db.create_user(email="Alice@Example.com", password_hash=hash_password("pw"), name="Alice")
    assert u.email == "alice@example.com"  # normalized
    assert db.get_user_by_id(u.id).email == "alice@example.com"
    assert db.get_user_by_email("ALICE@example.com").id == u.id
    assert "password_hash" not in u.public()


def test_duplicate_email_raises(db: UsersDB):
    db.create_user(email="dup@x.com", password_hash="h")
    with pytest.raises(DuplicateEmailError):
        db.create_user(email="DUP@x.com", password_hash="h2")


def test_counts_and_admins(db: UsersDB):
    assert db.count_users() == 0
    db.create_user(email="a@x.com", password_hash="h", is_admin=True)
    db.create_user(email="b@x.com", password_hash="h", is_admin=False)
    assert db.count_users() == 2
    assert db.count_admins() == 1


def test_update_and_delete_user(db: UsersDB):
    u = db.create_user(email="c@x.com", password_hash="h", name="C")
    assert db.update_user(u.id, name="C2", is_admin=True, is_active=False)
    refreshed = db.get_user_by_id(u.id)
    assert refreshed.name == "C2"
    assert refreshed.is_admin is True
    assert refreshed.is_active is False
    assert db.update_user(u.id) is False  # no recognized fields
    assert db.delete_user(u.id)
    assert db.get_user_by_id(u.id) is None


def test_update_email_conflict_raises(db: UsersDB):
    db.create_user(email="x@x.com", password_hash="h")
    other = db.create_user(email="y@x.com", password_hash="h")
    with pytest.raises(DuplicateEmailError):
        db.update_user(other.id, email="X@x.com")


# ----------------------------------------------------------------- prefs


def test_prefs_default_empty(db: UsersDB):
    assert db.get_prefs("nobody") == {}


def test_prefs_set_and_get(db: UsersDB):
    u = db.create_user(email="p@x.com", password_hash="h")
    db.set_prefs(u.id, {"nav_order": ["/chat", "/overview"], "x": 1})
    assert db.get_prefs(u.id) == {"nav_order": ["/chat", "/overview"], "x": 1}


def test_prefs_merge_preserves_other_keys(db: UsersDB):
    u = db.create_user(email="m@x.com", password_hash="h")
    db.set_prefs(u.id, {"nav_order": ["/a"], "keep": True})
    merged = db.merge_prefs(u.id, {"nav_order": ["/b", "/c"]})
    assert merged == {"nav_order": ["/b", "/c"], "keep": True}
    assert db.get_prefs(u.id) == merged


def test_prefs_isolated_per_user(db: UsersDB):
    a = db.create_user(email="a@p.com", password_hash="h")
    b = db.create_user(email="b@p.com", password_hash="h")
    db.set_prefs(a.id, {"nav_order": ["/a"]})
    db.set_prefs(b.id, {"nav_order": ["/b"]})
    assert db.get_prefs(a.id) == {"nav_order": ["/a"]}
    assert db.get_prefs(b.id) == {"nav_order": ["/b"]}


def test_prefs_owner_id_without_user_row(db: UsersDB):
    # Single-user mode stores under the synthetic "owner" id, absent from users.
    db.set_prefs("owner", {"nav_order": ["/overview"]})
    assert db.get_prefs("owner") == {"nav_order": ["/overview"]}


def test_prefs_cleaned_up_on_delete(db: UsersDB):
    u = db.create_user(email="d@p.com", password_hash="h")
    db.set_prefs(u.id, {"nav_order": ["/a"]})
    db.delete_user(u.id)
    assert db.get_prefs(u.id) == {}


# ----------------------------------------------------------------- sessions


def test_session_lifecycle(db: UsersDB):
    u = db.create_user(email="s@x.com", password_hash="h")
    raw = new_session_token()
    th = hash_token(raw)
    db.create_session(u.id, th, ttl_days=14)
    sess = db.get_session(th)
    assert sess is not None and sess["user_id"] == u.id
    db.touch_session(th)
    assert db.delete_session(th)
    assert db.get_session(th) is None


def test_purge_expired_sessions(db: UsersDB):
    u = db.create_user(email="e@x.com", password_hash="h")
    live = hash_token(new_session_token())
    dead = hash_token(new_session_token())
    db.create_session(u.id, live, ttl_days=14)
    db.create_session(u.id, dead, ttl_days=-1)  # already expired
    purged = db.purge_expired()
    assert purged == 1
    assert db.get_session(dead) is None
    assert db.get_session(live) is not None


def test_delete_user_cascades_sessions_and_access(db: UsersDB):
    u = db.create_user(email="cascade@x.com", password_hash="h")
    th = hash_token(new_session_token())
    db.create_session(u.id, th, ttl_days=1)
    db.grant_access(u.id, "merli")
    db.delete_user(u.id)
    assert db.get_session(th) is None
    assert db.has_access(u.id, "merli") is False


# ----------------------------------------------------------------- access


def test_access_grant_role_and_revoke(db: UsersDB):
    u = db.create_user(email="acc@x.com", password_hash="h")
    assert db.has_access(u.id, "merli") is False
    db.grant_access(u.id, "merli", role="member")
    assert db.has_access(u.id, "merli") is True
    assert db.get_role(u.id, "merli") == "member"
    db.set_owner(u.id, "scout")
    assert db.get_role(u.id, "scout") == "owner"
    assert db.owner_of("scout") == u.id
    assert {a["agent_name"] for a in db.list_agents_for_user(u.id)} == {"merli", "scout"}
    assert db.revoke_access(u.id, "merli") is True
    assert db.has_access(u.id, "merli") is False


def test_list_users_for_agent_and_revoke_agent(db: UsersDB):
    a = db.create_user(email="a2@x.com", password_hash="h")
    b = db.create_user(email="b2@x.com", password_hash="h")
    db.set_owner(a.id, "merli")
    db.grant_access(b.id, "merli", role="member")
    rows = db.list_users_for_agent("merli")
    assert {r["user_id"] for r in rows} == {a.id, b.id}
    assert db.revoke_agent("merli") == 2
    assert db.list_users_for_agent("merli") == []


def test_title_round_trips_and_may_be_cleared(db: UsersDB):
    """The position under a person's name — free text, editable, clearable."""
    u = db.create_user(email="t@x.com", password_hash="h", name="T", title="  Head of Sales ")
    assert u.title == "Head of Sales"
    assert db.get_user_by_id(u.id).title == "Head of Sales"
    assert u.public()["title"] == "Head of Sales"
    assert db.update_user(u.id, title="CTO")
    assert db.get_user_by_id(u.id).title == "CTO"
    # Unlike `name`, blank is a real write: no position.
    assert db.update_user(u.id, title="")
    assert db.get_user_by_id(u.id).title == ""


def test_pre_title_databases_read_an_empty_title(db: UsersDB):
    """A users.db created before the column existed gets it added on open and
    every existing row reads as untitled, never as NULL."""
    with db._conn() as conn:
        conn.execute("ALTER TABLE users DROP COLUMN title")
        conn.commit()
    db._init_db()
    u = db.create_user(email="old@x.com", password_hash="h", name="Old")
    assert db.get_user_by_id(u.id).title == ""
