"""Secure, cross-platform keyring backend detection for the vault.

Verifies we only trust a real OS keychain (macOS Keychain, Linux SecretService /
KWallet, Windows) and never an insecure plaintext file backend — the headless
Linux footgun that would silently store the vault master key unencrypted.
"""

from __future__ import annotations

import sys
import types

from olano.vault import keyring_backend as kb


def _install_fake_keyring(
    monkeypatch,
    *,
    backend_module: str,
    get_keyring_raises: bool = False,
    get_password_raises: bool = False,
) -> None:
    """Put a fake ``keyring`` module in sys.modules with a chosen backend."""
    fake = types.ModuleType("keyring")

    def get_keyring():
        if get_keyring_raises:
            raise RuntimeError("no usable backend")
        cls = type("Keyring", (), {})
        cls.__module__ = backend_module
        return cls()

    def get_password(service, key):
        if get_password_raises:
            raise RuntimeError("keyring locked / no daemon")
        return None

    fake.get_keyring = get_keyring  # type: ignore[attr-defined]
    fake.get_password = get_password  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", fake)


def test_macos_keychain_is_secure(monkeypatch) -> None:
    _install_fake_keyring(monkeypatch, backend_module="keyring.backends.macOS")
    assert kb.secure_keyring_available() is True


def test_linux_secretservice_is_secure(monkeypatch) -> None:
    _install_fake_keyring(monkeypatch, backend_module="keyring.backends.SecretService")
    assert kb.secure_keyring_available() is True


def test_windows_is_secure(monkeypatch) -> None:
    _install_fake_keyring(monkeypatch, backend_module="keyring.backends.Windows")
    assert kb.secure_keyring_available() is True


def test_plaintext_alt_backend_is_rejected(monkeypatch) -> None:
    # keyrings.alt plaintext/obfuscated file backend — must NOT be trusted.
    _install_fake_keyring(monkeypatch, backend_module="keyrings.alt.file")
    assert kb.secure_keyring_available() is False


def test_fail_backend_is_rejected(monkeypatch) -> None:
    _install_fake_keyring(monkeypatch, backend_module="keyring.backends.fail")
    assert kb.secure_keyring_available() is False


def test_chainer_backend_is_rejected(monkeypatch) -> None:
    # Conservative: a chainer isn't a concrete secure backend → fall back to env.
    _install_fake_keyring(monkeypatch, backend_module="keyring.backends.chainer")
    assert kb.secure_keyring_available() is False


def test_secure_backend_but_unreadable_is_rejected(monkeypatch) -> None:
    # Secure backend selected but no live session (e.g. headless SecretService).
    _install_fake_keyring(
        monkeypatch, backend_module="keyring.backends.SecretService", get_password_raises=True
    )
    assert kb.secure_keyring_available() is False


def test_get_keyring_error_is_safe(monkeypatch) -> None:
    _install_fake_keyring(
        monkeypatch, backend_module="keyring.backends.macOS", get_keyring_raises=True
    )
    assert kb.secure_keyring_available() is False


def test_keyring_not_installed_is_safe(monkeypatch) -> None:
    # `import keyring` raising ImportError must yield False, never crash.
    monkeypatch.setitem(sys.modules, "keyring", None)
    assert kb.secure_keyring_available() is False
    assert kb.active_backend_name() is None
