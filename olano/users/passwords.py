"""Password hashing and session-token minting using only the Python stdlib.

No third-party auth libraries are installed (no bcrypt/passlib/argon2/PyJWT), so
password hashing uses PBKDF2-HMAC-SHA256 and session tokens are opaque random
strings. Only the SHA-256 of a session token is ever persisted; the raw token
lives solely in the user's cookie.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# Stored hash format: "pbkdf2_sha256$<iterations>$<salt_hex>$<derived_hex>"
_ALGO = "pbkdf2_sha256"
_DEFAULT_ITERATIONS = 600_000
_SALT_BYTES = 16
_TOKEN_BYTES = 32


def hash_password(password: str, *, iterations: int = _DEFAULT_ITERATIONS) -> str:
    """Return a self-describing PBKDF2 hash for ``password``."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a stored PBKDF2 hash."""
    if not password or not stored:
        return False
    try:
        algo, iterations_s, salt_hex, derived_hex = stored.split("$")
        if algo != _ALGO:
            return False
        iterations = int(iterations_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(derived_hex)
    except (ValueError, AttributeError):
        return False
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(derived, expected)


def new_session_token() -> str:
    """Mint a fresh, URL-safe session token. The caller stores only its hash."""
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(raw_token: str) -> str:
    """Return the SHA-256 hex digest of a raw session token (what we persist)."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
