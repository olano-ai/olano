"""`server start` bridges the keyring's vault key into the daemon's env.

On macOS a detached daemon usually can't read the login Keychain, so keyring
auto-unlock fails for it even though it works in the foreground `server start`
process. `start_daemon` therefore reads the key here (where Keychain access
works) and injects `LIONCLAW_VAULT_KEY` so the daemon can unlock via the env path.
"""

from __future__ import annotations

import pytest

# keyring is an optional extra (olano[keyring]); the runtime imports it lazily.
# Skip the whole module rather than hard-erroring collection when it isn't installed
# (CI doesn't install the extra).
keyring = pytest.importorskip("keyring")

from olano.runtime import daemon
from olano.vault.runtime import VaultUnlockResult


def test_inject_skips_when_env_key_already_set(monkeypatch) -> None:
    monkeypatch.setattr("olano.vault.keyring_backend.secure_keyring_available", lambda: True)
    monkeypatch.setattr(keyring, "get_password", lambda s, a: "from-keyring")
    env = {"LIONCLAW_VAULT_KEY": "explicit"}
    daemon._inject_vault_key_from_keyring(env)
    assert env["LIONCLAW_VAULT_KEY"] == "explicit"  # explicit env wins, untouched


def test_inject_adds_valid_keyring_key(monkeypatch) -> None:
    monkeypatch.setattr("olano.vault.keyring_backend.secure_keyring_available", lambda: True)
    monkeypatch.setattr(keyring, "get_password", lambda s, a: "good-pass")
    monkeypatch.setattr(
        "olano.vault.runtime.unlock_vault_for_session",
        lambda **kw: VaultUnlockResult(vault=object(), mode="passphrase", db_existed=True),
    )
    env: dict[str, str] = {}
    daemon._inject_vault_key_from_keyring(env)
    assert env.get("LIONCLAW_VAULT_KEY") == "good-pass"


def test_inject_skips_invalid_keyring_key(monkeypatch) -> None:
    """A drifted keyring key that doesn't unlock must not be injected."""
    monkeypatch.setattr("olano.vault.keyring_backend.secure_keyring_available", lambda: True)
    monkeypatch.setattr(keyring, "get_password", lambda s, a: "wrong-pass")
    monkeypatch.setattr(
        "olano.vault.runtime.unlock_vault_for_session",
        lambda **kw: VaultUnlockResult(vault=None, mode="passphrase", db_existed=True, error="bad"),
    )
    env: dict[str, str] = {}
    daemon._inject_vault_key_from_keyring(env)
    assert "LIONCLAW_VAULT_KEY" not in env


def test_inject_noop_when_keyring_unavailable(monkeypatch) -> None:
    monkeypatch.setattr("olano.vault.keyring_backend.secure_keyring_available", lambda: False)
    env: dict[str, str] = {}
    daemon._inject_vault_key_from_keyring(env)  # must not raise
    assert "LIONCLAW_VAULT_KEY" not in env
