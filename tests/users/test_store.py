"""Tests for the UserStore service layer (auth, sessions, access)."""

from __future__ import annotations

from olano.users.db import UsersDB
from olano.users.store import UserStore, get_user_store, reset_user_store


def _store() -> UserStore:
    return UserStore(UsersDB())


def test_authenticate_success_and_failure():
    s = _store()
    s.create_user(email="alice@x.com", password="hunter2", name="Alice")
    assert s.authenticate("ALICE@x.com", "hunter2") is not None  # email case-insensitive
    assert s.authenticate("alice@x.com", "wrong") is None
    assert s.authenticate("nobody@x.com", "hunter2") is None


def test_authenticate_inactive_user_rejected():
    s = _store()
    u = s.create_user(email="bob@x.com", password="pw")
    s.db.update_user(u.id, is_active=False)
    assert s.authenticate("bob@x.com", "pw") is None


def test_session_issue_and_resolve():
    s = _store()
    u = s.create_user(email="c@x.com", password="pw")
    raw = s.issue_session(u)
    resolved = s.resolve_session(raw)
    assert resolved is not None and resolved.id == u.id
    assert s.resolve_session("bogus-token") is None


def test_expired_session_is_rejected_and_purged():
    s = _store()
    u = s.create_user(email="d@x.com", password="pw")
    raw = s.issue_session(u, ttl_days=-1)  # already expired
    from olano.users.passwords import hash_token

    assert s.resolve_session(raw) is None
    assert s.db.get_session(hash_token(raw)) is None  # deleted on resolve


def test_revoke_session():
    s = _store()
    u = s.create_user(email="e@x.com", password="pw")
    raw = s.issue_session(u)
    assert s.revoke(raw) is True
    assert s.resolve_session(raw) is None


def test_can_chat_access_model():
    s = _store()
    admin = s.create_user(email="admin@x.com", password="pw", is_admin=True)
    member = s.create_user(email="m@x.com", password="pw")
    outsider = s.create_user(email="o@x.com", password="pw")
    s.db.grant_access(member.id, "merli")

    assert s.can_chat(admin, "anything") is True  # admin → any agent
    assert s.can_chat(member, "merli") is True
    assert s.can_chat(member, "scout") is False
    assert s.can_chat(outsider, "merli") is False
    assert s.list_agent_names(member) == ["merli"]


def test_bootstrap_admin_creates_admin():
    s = _store()
    admin = s.bootstrap_admin(email="root@x.com", password="pw", name="Root")
    assert admin.is_admin is True
    assert s.authenticate("root@x.com", "pw") is not None


def test_get_user_store_is_cached_per_home():
    reset_user_store()
    a = get_user_store()
    b = get_user_store()
    assert a is b  # same home → same cached store


def test_unknown_email_burns_a_password_check():
    """An unknown address must cost the same PBKDF2 work as a wrong password,
    or the response time says which addresses have accounts."""
    from unittest.mock import patch

    from olano.users import store as store_mod

    s = _store()
    s.create_user(email="alice@x.com", password="hunter2", name="Alice")
    with patch.object(store_mod, "verify_password", return_value=False) as verify:
        assert s.authenticate("nobody@x.com", "hunter2") is None
        assert verify.call_count == 1
        assert verify.call_args.args[1] == store_mod._dummy_password_hash()
        assert s.authenticate("alice@x.com", "wrong") is None
        assert verify.call_count == 2
