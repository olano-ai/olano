"""The human handle: minting, the backfill, and what it guarantees.

`users.username` exists so a person's identity in the issue tracker stops
being a function of their editable display name. These tests pin the two
properties that makes true: an existing box's principals survive the
migration unchanged, and a rename can never move one.
"""

from __future__ import annotations

import sqlite3

import pytest

from olano.issues.principals import human_principal, slugify
from olano.users.db import DuplicateUsernameError, UsersDB, humanize_handle, mint_username
from olano.users.passwords import hash_password

# The users table exactly as it was before `username` existed. The backfill's
# whole job is to be correct against this, so the test builds the real thing
# rather than mutating a current-schema DB.
_LEGACY_SCHEMA = """
CREATE TABLE users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT,
    password_hash TEXT NOT NULL,
    is_admin      INTEGER DEFAULT 0,
    is_active     INTEGER DEFAULT 1,
    can_terminal  INTEGER DEFAULT 0,
    timezone      TEXT DEFAULT 'UTC',
    created_at    TEXT NOT NULL
)
"""


def _legacy_db(tmp_path, rows):
    """A pre-migration users.db holding *rows* of (id, email, name, created_at)."""
    path = tmp_path / "users.db"
    conn = sqlite3.connect(path)
    conn.execute(_LEGACY_SCHEMA)
    for user_id, email, name, created_at in rows:
        conn.execute(
            "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?,?,?,?,?)",
            (user_id, email, name, "x", created_at),
        )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def db() -> UsersDB:
    # Uses the autouse `lionclaw_home` fixture → isolated tmp users.db.
    return UsersDB()


# ------------------------------------------------------------------ minting


def test_mint_slugifies_and_suffixes_on_collision():
    assert mint_username("Carlos M.", []) == "carlos-m"
    assert mint_username("Carlos M.", {"carlos-m"}) == "carlos-m-2"
    assert mint_username("Carlos M.", {"carlos-m", "carlos-m-2"}) == "carlos-m-3"


def test_mint_ignores_blank_entries_in_taken():
    assert mint_username("Ana", {"", None}) == "ana"  # type: ignore[arg-type]


# ------------------------------------------------------------------ backfill


def test_backfill_preserves_every_unambiguous_principal(tmp_path):
    """The migration must not move a principal that was already unique.

    Every `human-*` string in issues.db was built as `slugify(user.name)`.
    Reproducing it exactly is what stops the migration from orphaning an
    append-only audit log.
    """
    rows = [
        ("u-ana", "ana@x.com", "Ana", "2023-01-01T00:00:00"),
        ("u-carlos", "carlos@x.com", "Carlos M.", "2024-01-01T00:00:00"),
        ("u-blank", "blank@x.com", "", "2024-03-01T00:00:00"),
    ]
    path = _legacy_db(tmp_path, rows)
    before = {uid: human_principal(slugify(name)) for uid, _, name, _ in rows}

    users = {u.id: u for u in UsersDB(db_path=path).list_users()}

    for uid, _, _, _ in rows:
        assert human_principal(users[uid].username) == before[uid]


def test_backfill_resolves_a_collision_in_favour_of_the_older_account(tmp_path):
    """Two names that slugified alike used to *share* one principal.

    They can't any more, so the newer account moves — and the older one, which
    has the longer history behind that principal, keeps it.
    """
    path = _legacy_db(
        tmp_path,
        [
            ("u-old", "a@x.com", "Carlos M.", "2024-01-01T00:00:00"),
            ("u-new", "b@x.com", "carlos m", "2024-06-01T00:00:00"),
        ],
    )
    users = {u.id: u for u in UsersDB(db_path=path).list_users()}

    assert users["u-old"].username == "carlos-m"
    assert users["u-new"].username == "carlos-m-2"


def test_backfill_keeps_the_ugly_handle_a_nameless_account_already_had(tmp_path):
    """`slugify("")` is "user", so a nameless account's principal *is*
    `human-user`. Minting something prettier here would orphan it."""
    path = _legacy_db(tmp_path, [("u1", "someone@x.com", "", "2024-01-01T00:00:00")])
    assert UsersDB(db_path=path).list_users()[0].username == "user"


def test_backfill_is_idempotent(tmp_path):
    path = _legacy_db(
        tmp_path,
        [
            ("u1", "a@x.com", "Ana", "2024-01-01T00:00:00"),
            ("u2", "b@x.com", "ana", "2024-02-01T00:00:00"),
        ],
    )
    first = {u.id: u.username for u in UsersDB(db_path=path).list_users()}
    second = {u.id: u.username for u in UsersDB(db_path=path).list_users()}
    assert first == second


def test_username_uniqueness_is_enforced_by_an_index(tmp_path):
    path = _legacy_db(tmp_path, [("u1", "a@x.com", "Ana", "2024-01-01T00:00:00")])
    UsersDB(db_path=path)
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO users (id, email, username, name, password_hash, created_at) "
            "VALUES ('u2','b@x.com','ana','Ana Two','x','2025-01-01T00:00:00')"
        )
    conn.close()


# ------------------------------------------------------- the guarantee itself


def test_renaming_a_user_does_not_move_their_principal(db: UsersDB):
    """The bug this whole field exists to fix.

    `human_principal` used to slugify the display name on every call, so this
    sequence silently re-keyed the user from `human-carlos` to
    `human-carlos-m` and stranded every issue already assigned to them.
    """
    user = db.create_user(email="c@x.com", password_hash=hash_password("pw"), name="Carlos")
    before = human_principal(user.username)

    assert db.update_user(user.id, name="Carlos M.")
    after = human_principal(db.get_user_by_id(user.id).username)  # type: ignore[union-attr]

    assert after == before == "human-carlos"


def test_username_is_not_writable_through_update_user(db: UsersDB):
    """It is the stable key; the allowlist must keep refusing it."""
    user = db.create_user(email="c@x.com", password_hash=hash_password("pw"), name="Carlos")
    db.update_user(user.id, username="someone-else")
    assert db.get_user_by_id(user.id).username == "carlos"  # type: ignore[union-attr]


# ------------------------------------------------------------------ creation


def test_create_mints_from_name_and_suffixes_a_clash(db: UsersDB):
    a = db.create_user(email="a@x.com", password_hash=hash_password("pw"), name="Ana")
    b = db.create_user(email="b@x.com", password_hash=hash_password("pw"), name="ana")
    assert (a.username, b.username) == ("ana", "ana-2")


def test_create_falls_back_to_the_email_local_part_when_nameless(db: UsersDB):
    """A brand-new account has no history to preserve, so it gets the better
    handle rather than the literal "user" the backfill has to keep."""
    user = db.create_user(email="dana.ray@x.com", password_hash=hash_password("pw"))
    assert user.username == "dana-ray"


def test_explicit_username_is_still_slugified(db: UsersDB):
    """A handle must never carry a character `human-<handle>` couldn't."""
    user = db.create_user(
        email="a@x.com", password_hash=hash_password("pw"), name="Ana", username="Ana Ray!"
    )
    assert user.username == "ana-ray"


def test_username_is_exposed_on_the_public_view(db: UsersDB):
    user = db.create_user(email="a@x.com", password_hash=hash_password("pw"), name="Ana")
    assert user.public()["username"] == "ana"


# ------------------------------------------------- explicit vs auto minting


def test_typed_taken_handle_is_refused_never_suffixed(db: UsersDB):
    """An explicit handle is an identity someone chose. Suffixing it silently
    saddles them with the wrong permanent identity and never tells them."""
    db.create_user(email="a@x.com", password_hash=hash_password("pw"), name="Carlos M.")
    with pytest.raises(DuplicateUsernameError):
        db.create_user(
            email="b@x.com", password_hash=hash_password("pw"), name="Other", username="carlos-m"
        )


def test_refused_create_leaves_no_half_written_row(db: UsersDB):
    db.create_user(email="a@x.com", password_hash=hash_password("pw"), name="Ana")
    with pytest.raises(DuplicateUsernameError):
        db.create_user(email="b@x.com", password_hash=hash_password("pw"), name="B", username="ana")
    assert db.get_user_by_email("b@x.com") is None


# ---------------------------------------------------------- never-empty name


def test_blank_name_derives_a_presentable_one_from_the_handle(db: UsersDB):
    user = db.create_user(email="dana.ray@x.com", password_hash=hash_password("pw"))
    assert user.username == "dana-ray"
    assert user.name == "Dana Ray"


def test_explicit_handle_with_blank_name_names_from_that_handle(db: UsersDB):
    user = db.create_user(email="x@x.com", password_hash=hash_password("pw"), username="team-lead")
    assert (user.username, user.name) == ("team-lead", "Team Lead")


def test_name_backfill_fills_blanks_without_moving_any_principal(tmp_path):
    """Order is load-bearing: handles mint from the ORIGINAL (blank) names
    first — reproducing the principals issues.db already holds — and only then
    do the invented names appear. A name invented before the mint would feed
    slugify and re-key the audit log."""
    path = _legacy_db(
        tmp_path,
        [
            ("u1", "carlos@x.com", "Carlos M.", "2024-01-01T00:00:00"),
            ("u2", "blank@x.com", "", "2024-03-01T00:00:00"),
            ("u3", "blank2@x.com", None, "2024-04-01T00:00:00"),
        ],
    )
    users = {u.id: u for u in UsersDB(db_path=path).list_users()}
    # Principals exactly as slugify(original name) produced them.
    assert users["u1"].username == "carlos-m"
    assert users["u2"].username == "user"
    assert users["u3"].username == "user-2"
    # Names filled, never blank, and derived from the handle — not the reverse.
    assert users["u1"].name == "Carlos M."
    assert users["u2"].name == "User"
    assert users["u3"].name == "User 2"


def test_name_backfill_is_idempotent(tmp_path):
    path = _legacy_db(tmp_path, [("u1", "b@x.com", "", "2024-01-01T00:00:00")])
    first = {u.id: (u.username, u.name) for u in UsersDB(db_path=path).list_users()}
    second = {u.id: (u.username, u.name) for u in UsersDB(db_path=path).list_users()}
    assert first == second == {"u1": ("user", "User")}


def test_blank_name_update_is_dropped(db: UsersDB):
    user = db.create_user(email="a@x.com", password_hash=hash_password("pw"), name="Ana")
    db.update_user(user.id, name="   ")
    assert db.get_user_by_id(user.id).name == "Ana"  # type: ignore[union-attr]


def test_humanize_handle():
    assert humanize_handle("dana-ray") == "Dana Ray"
    assert humanize_handle("user-2") == "User 2"
    assert humanize_handle("admin") == "Admin"
