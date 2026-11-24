"""The one-time admin password note (users/initial_password.py).

quick-setup generates the first admin's password when nobody supplied one,
prints it once and keeps it under <home>/run/ (0600) so a deploy banner can
point at it. The note is a convenience with a short life: every path that sets
a new password on that account removes it, and another account's password
change leaves it alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from olano.users import initial_password
from olano.users.db import PROVISIONED_ADMIN_HANDLE
from olano.users.store import get_user_store, reset_user_store


@pytest.fixture(autouse=True)
def _reset_user_store():
    reset_user_store()
    yield
    reset_user_store()


def test_generated_passwords_are_long_unambiguous_and_distinct() -> None:
    seen = {initial_password.generate_password() for _ in range(50)}
    assert len(seen) == 50
    for password in seen:
        assert len(password) == initial_password.PASSWORD_LENGTH
        assert not set(password) & set("0O1lI"), password
    assert len(initial_password.generate_vault_passphrase()) >= 40


def test_remember_read_forget_roundtrip(lionclaw_home: Path) -> None:
    assert initial_password.read() is None
    path = initial_password.remember("owner@example.com", "Secret-One")
    assert path == initial_password.path()
    assert path.parent == lionclaw_home / "run"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert initial_password.read() == ("owner@example.com", "Secret-One")

    # Another account's change does not touch it; the owner's does, whatever
    # the letter case; and asking again afterwards is a quiet no.
    assert initial_password.forget("dana@example.com") is False
    assert initial_password.read() == ("owner@example.com", "Secret-One")
    assert initial_password.forget("OWNER@example.com") is True
    assert initial_password.read() is None
    assert initial_password.forget() is False


def test_a_malformed_note_reads_as_absent_and_is_dropped(lionclaw_home: Path) -> None:
    initial_password.path().parent.mkdir(parents=True, exist_ok=True)
    initial_password.path().write_text("not a note\n", encoding="utf-8")
    assert initial_password.read() is None
    initial_password.forget("owner@example.com")
    assert not initial_password.path().exists()


def _provisioned_admin():
    return get_user_store().bootstrap_admin(
        email="owner@example.com",
        password="one-time",
        name="Admin",
        username=PROVISIONED_ADMIN_HANDLE,
    )


def test_every_way_of_setting_the_accounts_password_retires_the_note(lionclaw_home: Path) -> None:
    store = get_user_store()
    admin = _provisioned_admin()

    initial_password.remember(admin.email, "one-time")
    assert store.change_password(admin.id, "one-time", "mine-now") is True
    assert initial_password.read() is None

    initial_password.remember(admin.email, "x")
    assert store.set_password(admin.id, "set-by-an-admin") is True
    assert initial_password.read() is None

    # The reset-password flow: a token the owner redeems with a new password.
    initial_password.remember(admin.email, "x")
    issued = store.reset_password(admin.id, created_by="cli")
    assert issued is not None
    assert store.accept_invite(issued[0], "from-the-reset-link") is not None
    assert initial_password.read() is None

    # The onboarding wizard's claim of the provisioned row.
    initial_password.remember(admin.email, "x")
    store.claim_provisioned_account(admin.id, name="Owner Name", password="claimed")
    assert initial_password.read() is None


def test_a_wrong_current_password_changes_nothing_including_the_note(lionclaw_home: Path) -> None:
    store = get_user_store()
    admin = _provisioned_admin()
    initial_password.remember(admin.email, "one-time")
    assert store.change_password(admin.id, "not-it", "mine-now") is False
    assert initial_password.read() == (admin.email, "one-time")


def test_another_users_password_change_keeps_the_note(lionclaw_home: Path) -> None:
    store = get_user_store()
    admin = _provisioned_admin()
    other = store.create_user(email="dana@example.com", password="pw", name="Dana")
    initial_password.remember(admin.email, "one-time")
    assert store.set_password(other.id, "new-pw") is True
    assert initial_password.read() == ("owner@example.com", "one-time")
