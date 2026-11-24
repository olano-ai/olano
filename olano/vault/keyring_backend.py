"""Cross-platform, *secure* detection of the OS keyring for the credentials vault.

The vault can persist its master key in the system keyring so it auto-unlocks
across restarts without ``OLANO_VAULT_KEY``. The Python ``keyring`` library is
cross-platform — macOS **Keychain**, Linux **SecretService** (gnome-keyring) /
**KWallet**, Windows **Credential Locker** — all of which encrypt secrets at rest
via the OS.

The catch is headless Linux / Docker / cloud: there is usually no secret-service
daemon, and ``keyring`` may silently select an **insecure** backend — the
``keyrings.alt`` plaintext/obfuscated *file* backend, or the null/fail backend.
Stashing the vault master key in a plaintext file would defeat the vault's
encryption entirely. So we **whitelist the known-secure OS backends** and refuse
everything else; callers then fall back to ``OLANO_VAULT_KEY`` (which cloud
deploys set) or a passphrase.

This is the single source of truth — :mod:`olano.vault.runtime`,
:mod:`olano.vault.store`, ``olano.cli.main`` and ``olano.models`` all
defer here so the policy can't drift between call sites.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Module prefixes of keyring backends that store secrets in an OS-encrypted store.
# Matching by module (not class) keeps it version-robust across keyring releases.
_SECURE_BACKEND_PREFIXES = (
    "keyring.backends.macOS",  # macOS Keychain (modern)
    "keyring.backends.OS_X",  # macOS Keychain (legacy module alias)
    "keyring.backends.SecretService",  # Freedesktop SecretService (gnome-keyring)
    "keyring.backends.kwallet",  # KDE KWallet
    "keyring.backends.Windows",  # Windows Credential Locker
)

# OS keychain service name. NEVER rename — it is the lookup key for master
# keys already stored in macOS Keychain / SecretService / KWallet. A new name
# silently finds nothing and falls back to an ephemeral key.
_VAULT_KEYRING_SERVICE = "lionclaw"
_VAULT_KEYRING_KEY = "vault_master_key"


def active_backend_name() -> str | None:
    """Return the dotted module path of the active keyring backend, or None."""
    try:
        import keyring

        return type(keyring.get_keyring()).__module__ or None
    except Exception:
        return None


def secure_keyring_available() -> bool:
    """True only when keyring's active backend is a secure OS keychain.

    Safe on every platform — returns False (so the caller falls back to
    ``OLANO_VAULT_KEY`` / passphrase) when keyring isn't installed, no secure
    backend is active (e.g. headless Linux), an insecure file backend is selected,
    or the backend errors on access.
    """
    try:
        import keyring
    except Exception:
        return False
    try:
        backend = keyring.get_keyring()
    except Exception:
        return False
    module = type(backend).__module__ or ""
    if not any(module.startswith(prefix) for prefix in _SECURE_BACKEND_PREFIXES):
        logger.debug(
            "Vault: ignoring non-secure keyring backend %r — falling back to env/passphrase",
            module or type(backend).__name__,
        )
        return False
    # A secure backend can still be non-functional (e.g. SecretService with no
    # running daemon, or a locked login keyring) — those raise on access. Probe
    # cheaply so we only claim availability when a read actually works.
    try:
        keyring.get_password(_VAULT_KEYRING_SERVICE, _VAULT_KEYRING_KEY)
    except Exception:
        return False
    return True
