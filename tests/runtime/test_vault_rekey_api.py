"""Setting a vault passphrase for the first time.

A managed box is provisioned with a temporary vault passphrase that lives in
the cloud console and auto-unlocks here from the boot key. Requiring it to set
a new one sends the owner to another website to copy a secret they never chose,
so an *unlocked* vault may be rekeyed from the live store — the open vault
already proves key possession, and /api/vault/* is admin-only. A locked vault
still demands the old passphrase.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.deployment import credential_rotation as cr
from olano.runtime.api import create_api
from olano.vault.resolver import set_vault
from olano.vault.store import VaultStore

OLD = "provisioned-temp-pass"
NEW = "the-owners-own-passphrase"


@pytest.fixture
def vault_path(tmp_path, monkeypatch):
    path = tmp_path / "vault.db"
    monkeypatch.setattr("olano.vault.store.default_vault_path", lambda: path)
    monkeypatch.setattr("olano.runtime.api.default_vault_path", lambda: path, raising=False)
    yield path
    set_vault(None)


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr(cr, "schedule_report", lambda home=None: None)
    runtime = MagicMock()
    runtime._runners = {}
    return create_api(runtime)


def _seed(path) -> VaultStore:
    """All three row kinds — a rekey that walks only the common table used to
    strand the other two under the dead key."""
    store = VaultStore.open(passphrase=OLD, db_path=path, use_keyring=False)
    store.set(name="OPENAI_API_KEY", value="sk-secret", description="d", category="general")
    store.set(name="OPENAI_API_KEY", value="sk-alice", agent="alice")
    store.set_customer("CRM_KEY", agent="support", customer="cust-1", value="sk-cust")
    return store


def test_rekey_without_old_when_unlocked(app, vault_path, monkeypatch):
    set_vault(_seed(vault_path))

    r = TestClient(app).post("/api/vault/rekey", json={"new_passphrase": NEW})

    assert r.status_code == 200, r.text
    assert r.json()["rekeyed"] == 3
    assert r.json()["skipped"] == []
    # Every table survives, readable under the new passphrase and not the old.
    reopened = VaultStore.open(passphrase=NEW, db_path=vault_path, use_keyring=False)
    assert reopened.get("OPENAI_API_KEY") == "sk-secret"
    assert reopened.get_override("OPENAI_API_KEY", "alice") == "sk-alice"
    assert reopened.get_customer("CRM_KEY", "support", "cust-1") == "sk-cust"
    assert cr.pending_flags(cr.load_state())["vault_password_rotated"] is True


def test_rekey_without_old_is_refused_while_locked(app, vault_path, monkeypatch):
    _seed(vault_path)
    set_vault(None)
    # Nothing can unlock it unattended: no env key, no boot key.
    monkeypatch.setattr(
        "olano.vault.runtime.unlock_vault_for_session", lambda **kw: None, raising=False
    )

    r = TestClient(app).post("/api/vault/rekey", json={"new_passphrase": NEW})

    assert r.status_code == 401
    assert "locked" in r.json()["detail"].lower()


def test_wrong_old_passphrase_still_rejected(app, vault_path):
    set_vault(_seed(vault_path))

    r = TestClient(app).post(
        "/api/vault/rekey", json={"old_passphrase": "not-it", "new_passphrase": NEW}
    )

    assert r.status_code == 401


def test_wrong_old_rejected_even_without_common_rows(app, vault_path):
    """The old copy-based rekey verified the passphrase only by decrypting
    common rows — a vault holding nothing but per-agent overrides accepted any
    old passphrase and 'activated' a key it never proved."""
    store = VaultStore.open(passphrase=OLD, db_path=vault_path, use_keyring=False)
    store.set(name="ONLY_OVERRIDE", value="sk-alice", agent="alice")
    set_vault(store)

    r = TestClient(app).post(
        "/api/vault/rekey", json={"old_passphrase": "not-it", "new_passphrase": NEW}
    )

    assert r.status_code == 401
    # …and the override is untouched, still readable under the real passphrase.
    reopened = VaultStore.open(passphrase=OLD, db_path=vault_path, use_keyring=False)
    assert reopened.get_override("ONLY_OVERRIDE", "alice") == "sk-alice"


def test_new_passphrase_is_still_required(app, vault_path):
    set_vault(_seed(vault_path))

    r = TestClient(app).post("/api/vault/rekey", json={"old_passphrase": OLD})

    assert r.status_code == 400


def test_short_new_passphrase_rejected(app, vault_path):
    set_vault(_seed(vault_path))

    r = TestClient(app).post("/api/vault/rekey", json={"new_passphrase": "short"})

    assert r.status_code == 400


def test_member_cannot_rekey(app, vault_path):
    """The /api/vault prefix is global-admin-only; a plain member is refused
    before any of the above matters."""
    set_vault(_seed(vault_path))
    admin = TestClient(app)
    assert (
        admin.post(
            "/api/auth/bootstrap",
            json={"email": "admin@x.com", "password": "pw", "name": "Admin"},
        ).status_code
        == 200
    )
    admin.post("/api/admin/users", json={"email": "bob@x.com", "password": "pw", "name": "Bob"})
    member = TestClient(app)
    assert (
        member.post("/api/auth/login", json={"email": "bob@x.com", "password": "pw"}).status_code
        == 200
    )

    r = member.post("/api/vault/rekey", json={"new_passphrase": NEW})

    assert r.status_code == 403
