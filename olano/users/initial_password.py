"""The one-time admin password a fresh install is born with.

``olano quick-setup`` creates the first admin account. When nobody supplied a
password, one is generated: a well-known default would make every
unconfigured install a door with the key under the mat. A generated password
still has to reach the operator, and a non-interactive bootstrap has only its
terminal, so the password is printed once and also kept here, under the home's
``run/`` directory with mode 0600, until the account's password is changed.
The deploy banners point at this file instead of printing a password into an
install log.

The file is a convenience, not a credential store: it exists only while the
generated password is the live one. Every path that sets a new password on the
account removes it (``UserStore`` calls :func:`forget`).
"""

from __future__ import annotations

import logging
import os
import secrets
import tempfile
from pathlib import Path

from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)

INITIAL_PASSWORD_FILENAME = "initial-admin-password"

# No 0/O or 1/l/I: the operator types this from a terminal or a phone screen.
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"  # gitleaks:allow
PASSWORD_LENGTH = 16


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    """A one-time sign-in password: 16 characters from an unambiguous alphabet.

    About 93 bits of entropy, which is plenty for a password that is replaced
    at the first sign-in and is only ever tried against a rate-limited login.
    """
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def generate_vault_passphrase() -> str:
    """A vault passphrase nobody types: 32 random bytes, URL-safe base64."""
    return secrets.token_urlsafe(32)


def path() -> Path:
    return get_olano_paths().run_dir / INITIAL_PASSWORD_FILENAME


def remember(email: str, password: str) -> Path:
    """Keep the generated password beside the home, readable by the owner only."""
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.")
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"email={email}\npassword={password}\n")
        tmp_path.chmod(0o600)
        tmp_path.replace(p)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return p


def read() -> tuple[str, str] | None:
    """The remembered ``(email, password)``, or ``None`` when there is none."""
    p = path()
    try:
        if p.is_symlink() or not p.is_file():
            return None
        fields: dict[str, str] = {}
        for line in p.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip()
    except OSError:
        return None
    email, password = fields.get("email", ""), fields.get("password", "")
    return (email, password) if email and password else None


def forget(email: str | None = None) -> bool:
    """Remove the file once the password it holds is no longer the live one.

    With ``email`` given, only a note recorded for that account is removed, so
    another user's password change leaves the admin's note in place. Returns
    whether a note was removed. Never raises: this runs inside the password
    paths of the user store.
    """
    try:
        current = read()
        if current is None:
            # Absent, unreadable or malformed: nothing worth keeping either way.
            path().unlink(missing_ok=True)
            return False
        if email is not None and current[0].lower() != email.strip().lower():
            return False
        path().unlink(missing_ok=True)
        return True
    except OSError as exc:
        logger.debug("initial admin password note not removed: %s", exc)
        return False
