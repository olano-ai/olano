"""Vault auto-unlock must survive an owner rekey (managed key-on-box posture).

Managed deployments bake ``LIONCLAW_VAULT_KEY`` into the container env at
provision time; the runtime cannot rewrite that env, so after the owner rekeys
the vault the baked key is stale forever. These pin the self-healing added in
``vault/runtime.py``: the boot-key file written beside the vault DB on rekey /
manual unlock, the unlock chain's verify-and-fall-through (stale env → boot
key), and the strict "never introduce auto-unlock where none was configured"
gating for desktops.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import olano.vault.resolver as resolver
from olano.vault import runtime as vr
from olano.vault.store import VaultStore, default_vault_path

OLD = "temp-vault-pw"
NEW = "owner-chosen-pw"


@pytest.fixture(autouse=True)
def _vault_env(monkeypatch):
    """Isolate the resolver global and the unlock-relevant env per test."""
    monkeypatch.delenv("LIONCLAW_VAULT_KEY", raising=False)
    monkeypatch.delenv("OLANO_VAULT_KEY", raising=False)
    original = resolver.get_vault()
    resolver.set_vault(None)
    try:
        yield
    finally:
        resolver.set_vault(original)


def _make_vault(passphrase: str) -> None:
    """A vault keyed to ``passphrase`` with one entry (so wrong keys verify-fail)."""
    store = VaultStore.open(passphrase=passphrase, db_path=default_vault_path(), use_keyring=False)
    store.set("PROBE", "probe-value")


# ── unlock chain fall-through ────────────────────────────────────────────────


def test_stale_env_falls_through_to_boot_key(monkeypatch) -> None:
    _make_vault(NEW)
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", OLD)  # baked env, now stale
    vr._write_boot_key(NEW)

    result = vr.unlock_vault_for_session()
    assert result.vault is not None
    assert result.mode == "bootfile"
    assert result.vault.get("PROBE") == "probe-value"
    assert resolver.get_vault() is result.vault


def test_valid_env_wins_over_boot_key(monkeypatch) -> None:
    _make_vault(NEW)
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", NEW)
    vr._write_boot_key(OLD)  # stale file must not shadow a working env key

    result = vr.unlock_vault_for_session()
    assert result.vault is not None
    assert result.mode == "env"


def test_a_passphrase_less_open_reads_the_boot_key_before_minting_a_key(monkeypatch) -> None:
    """quick-setup leaves a generated passphrase in the boot-key file. The
    fallback paths that call ``VaultStore.open()`` with no passphrase (outside
    the runtime's unlock chain) must open that vault, not an ephemeral twin
    whose rows nothing else can read."""
    _make_vault(NEW)
    vr.write_boot_key(NEW)  # the public writer quick-setup uses
    monkeypatch.setattr("olano.vault.store.secure_keyring_available", lambda: False)

    store = VaultStore.open(db_path=default_vault_path(), use_keyring=False)
    assert store.get("PROBE") == "probe-value"


def test_stale_env_without_boot_key_reports_env_failure(monkeypatch) -> None:
    _make_vault(NEW)
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", OLD)

    result = vr.unlock_vault_for_session()
    assert result.vault is None
    assert result.mode == "env"  # first (configured) failure is the reported one
    assert result.error


@pytest.mark.parametrize("kind", ["override", "customer"])
def test_wrong_key_verify_fails_whichever_table_holds_the_rows(monkeypatch, kind) -> None:
    """One key encrypts every table, so any row is a valid probe. Probing only
    the common table let a vault whose rows lived elsewhere 'unlock' under any
    passphrase — the first real read then failed instead, and the first WRITE
    under the wrong key locked the correct passphrase out of its own vault."""
    store = VaultStore.open(passphrase=NEW, db_path=default_vault_path(), use_keyring=False)
    if kind == "override":
        store.set("ONLY_OVERRIDE", "alice-value", agent="alice")
    else:
        store.set_customer("CRM_KEY", agent="support", customer="cust-1", value="cust-value")
    assert store.list_names() == []  # nothing in the common table
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", OLD)

    result = vr.unlock_vault_for_session()

    assert result.vault is None
    assert result.error


def test_corrupt_boot_key_file_is_ignored(monkeypatch) -> None:
    _make_vault(NEW)
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", NEW)
    vr.boot_key_path().parent.mkdir(parents=True, exist_ok=True)
    vr.boot_key_path().write_text("")  # empty → read_boot_key() None

    assert vr.read_boot_key() is None
    assert vr.unlock_vault_for_session().mode == "env"


# ── self-heal on explicit passphrase unlock ──────────────────────────────────


def test_passphrase_unlock_rearms_boot_key_when_env_configured(monkeypatch) -> None:
    _make_vault(NEW)
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", OLD)  # stale baked key

    result = vr.unlock_vault_for_session(passphrase=NEW)
    assert result.vault is not None and result.mode == "passphrase"
    assert vr.read_boot_key() == NEW

    # Simulated restart: stale env again, no explicit passphrase → bootfile.
    resolver.set_vault(None)
    assert vr.unlock_vault_for_session().mode == "bootfile"


def test_passphrase_unlock_never_creates_boot_key_on_desktop() -> None:
    """No env key, no keyring, no existing file → posture unchanged."""
    _make_vault(NEW)
    result = vr.unlock_vault_for_session(passphrase=NEW)
    assert result.vault is not None
    assert vr.read_boot_key() is None


def test_register_false_probe_never_persists(monkeypatch) -> None:
    _make_vault(NEW)
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", OLD)
    result = vr.unlock_vault_for_session(passphrase=NEW, register=False)
    assert result.vault is not None
    assert vr.read_boot_key() is None
    assert resolver.get_vault() is None


# ── keyring refresh gating ───────────────────────────────────────────────────


class _FakeKeyring:
    def __init__(self, stored: str | None) -> None:
        self.stored = stored
        self.sets: list[str] = []

    def get_password(self, service: str, key: str) -> str | None:
        return self.stored

    def set_password(self, service: str, key: str, value: str) -> None:
        self.stored = value
        self.sets.append(value)


def _with_fake_keyring(monkeypatch, stored: str | None) -> _FakeKeyring:
    fake = _FakeKeyring(stored)
    monkeypatch.setattr(vr, "secure_keyring_available", lambda: True)
    monkeypatch.setitem(
        sys.modules,
        "keyring",
        SimpleNamespace(get_password=fake.get_password, set_password=fake.set_password),
    )
    return fake


def test_stale_keyring_entry_is_refreshed(monkeypatch) -> None:
    _make_vault(NEW)
    fake = _with_fake_keyring(monkeypatch, stored=OLD)  # drifted: no longer decrypts

    refreshed = vr.persist_auto_unlock_key(NEW)
    assert "keyring" in refreshed
    assert fake.stored == NEW


def test_missing_keyring_entry_is_never_created(monkeypatch) -> None:
    _make_vault(NEW)
    fake = _with_fake_keyring(monkeypatch, stored=None)

    refreshed = vr.persist_auto_unlock_key(NEW)
    assert refreshed == []
    assert fake.stored is None and fake.sets == []


def test_stale_env_falls_through_to_keyring(monkeypatch) -> None:
    _make_vault(NEW)
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", OLD)
    _with_fake_keyring(monkeypatch, stored=NEW)

    result = vr.unlock_vault_for_session()
    assert result.vault is not None
    assert result.mode == "keyring"


# ── detect_unlock_mode / rekey endpoint integration ──────────────────────────


def test_detect_unlock_mode_reports_bootfile(monkeypatch) -> None:
    assert vr.detect_unlock_mode() == "ephemeral"
    vr._write_boot_key(NEW)
    assert vr.detect_unlock_mode() == "bootfile"
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", OLD)
    assert vr.detect_unlock_mode() == "env"


def test_rekey_endpoint_rearms_auto_unlock(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from olano.runtime.api import create_api

    _make_vault(OLD)
    # Both spellings through monkeypatch BEFORE the rekey: the endpoint writes
    # both into os.environ, and monkeypatch only restores keys it set itself, so
    # a spelling it never touched would leak into every later test on this
    # worker (which is how test_agent_vault_api's locked-vault test read
    # "unlocked" on CI).
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", OLD)  # the baked, soon-stale key
    monkeypatch.setenv("OLANO_VAULT_KEY", OLD)

    client = TestClient(create_api(MagicMock(_runners={})))
    r = client.post("/api/vault/rekey", json={"old_passphrase": OLD, "new_passphrase": NEW})
    assert r.status_code == 200
    body = r.json()
    assert body["rekeyed"] == 1
    assert "bootfile" in body["auto_unlock"]
    assert vr.read_boot_key() == NEW
    # In-process env re-pointed so this session stays coherent.
    import os

    assert os.environ["LIONCLAW_VAULT_KEY"] == NEW
    assert os.environ["OLANO_VAULT_KEY"] == NEW  # both spellings, every reader sees it

    # Simulated restart with the container's stale baked env: cloud-init
    # writes the key under both spellings, and both come back stale.
    monkeypatch.setenv("LIONCLAW_VAULT_KEY", OLD)
    monkeypatch.setenv("OLANO_VAULT_KEY", OLD)
    resolver.set_vault(None)
    result = vr.unlock_vault_for_session()
    assert result.vault is not None
    assert result.mode == "bootfile"
    assert result.vault.get("PROBE") == "probe-value"
