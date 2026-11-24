"""Claiming the provisioned ``admin`` row: the one sanctioned handle rewrite.

A managed box is provisioned with the owner's own email on a row whose handle
is ``admin``. The onboarding profile step turns that row into the owner's
account — name, handle minted from the name, password — without touching its
id, email, admin flag or agent grants. These pin what moves and what must not.
"""

from __future__ import annotations

import pytest

from olano.users.db import PROVISIONED_ADMIN_HANDLE, UsersDB
from olano.users.passwords import hash_password, hash_token, new_session_token, verify_password


@pytest.fixture
def db() -> UsersDB:
    # Uses the autouse `lionclaw_home` fixture → isolated tmp users.db.
    return UsersDB()


def _provisioned(db: UsersDB):
    return db.create_user(
        email="owner@example.com",
        password_hash=hash_password("temp-pw"),
        name="Admin",
        is_admin=True,
        username=PROVISIONED_ADMIN_HANDLE,
    )


def test_claim_rewrites_name_handle_and_password_only(db: UsersDB):
    admin = _provisioned(db)
    assert admin.username == "admin"

    claimed = db.claim_provisioned_account(
        admin.id, name="Carlos Rivera", password_hash=hash_password("my-own-password")
    )
    assert claimed.id == admin.id
    assert claimed.email == "owner@example.com"  # untouched — one email, one account
    assert claimed.is_admin is True
    assert claimed.name == "Carlos Rivera"
    assert claimed.username == "carlos-rivera"  # minted from the name, same rule as creation
    assert verify_password("my-own-password", claimed.password_hash)
    assert not verify_password("temp-pw", claimed.password_hash)
    # Persisted, not just returned.
    assert db.get_user_by_id(admin.id).username == "carlos-rivera"
    assert db.get_user_by_email("owner@example.com").name == "Carlos Rivera"


def test_claim_excludes_own_handle_from_taken(db: UsersDB):
    """A name that slugifies to the current handle keeps it — no ``admin-2``."""
    admin = _provisioned(db)
    claimed = db.claim_provisioned_account(admin.id, name="Admin", password_hash="h")
    assert claimed.username == "admin"


def test_claim_suffixes_when_another_account_holds_the_slug(db: UsersDB):
    admin = _provisioned(db)
    db.create_user(email="c@x.com", password_hash="h", name="Carlos Rivera")  # → carlos-rivera
    claimed = db.claim_provisioned_account(admin.id, name="Carlos Rivera", password_hash="h")
    assert claimed.username == "carlos-rivera-2"


def test_claim_requires_a_name_and_a_known_row(db: UsersDB):
    admin = _provisioned(db)
    with pytest.raises(ValueError):
        db.claim_provisioned_account(admin.id, name="   ", password_hash="h")
    with pytest.raises(KeyError):
        db.claim_provisioned_account("nope", name="Carlos", password_hash="h")
    # Nothing moved on the refused calls.
    assert db.get_user_by_id(admin.id).username == "admin"


def test_claim_keeps_agent_grants(db: UsersDB):
    """agent_access keys on the user id, so ownership survives the rename."""
    admin = _provisioned(db)
    db.set_owner(admin.id, "merli", granted_by=admin.id)
    db.claim_provisioned_account(admin.id, name="Carlos", password_hash="h")
    assert [r["agent_name"] for r in db.list_agents_for_user(admin.id)] == ["merli"]


def test_delete_sessions_for_user_can_spare_one(db: UsersDB):
    admin = _provisioned(db)
    mine = new_session_token()
    other = new_session_token()
    db.create_session(admin.id, hash_token(mine))
    db.create_session(admin.id, hash_token(other))

    assert db.delete_sessions_for_user(admin.id, keep_token_hash=hash_token(mine)) == 1
    assert db.get_session(hash_token(mine)) is not None
    assert db.get_session(hash_token(other)) is None
    # Without the keep it is the old blanket revoke.
    assert db.delete_sessions_for_user(admin.id) == 1
    assert db.get_session(hash_token(mine)) is None
