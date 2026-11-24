"""High-level user service: authentication, sessions, and access checks.

Wraps :class:`UsersDB` with password hashing/verification, session token
minting, and the binary access model (one global admin; each agent has an owner
plus a flat allow-list of members who may chat with it).
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from olano.config.home import get_olano_paths
from olano.users.db import DEFAULT_SESSION_TTL_DAYS, DuplicateEmailError, User, UsersDB
from olano.users.passwords import (
    hash_password,
    hash_token,
    new_session_token,
    verify_password,
)

logger = logging.getLogger(__name__)

_DUMMY_HASH: str | None = None
_DUMMY_HASH_LOCK = threading.Lock()


def _dummy_password_hash() -> str:
    """A throwaway PBKDF2 hash used to equalise login timing for unknown emails.

    Derived once per process (the derivation costs the full iteration count)
    from a random value nobody holds, so it can never verify. ``get_user_store``
    warms it off the request path so the first unknown-email login after a
    restart does not pay for the derivation on top of the check.
    """
    global _DUMMY_HASH
    with _DUMMY_HASH_LOCK:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
        return _DUMMY_HASH


def warm_dummy_password_hash() -> None:
    """Derive the timing-equalisation hash in a daemon thread."""
    if _DUMMY_HASH is None:
        threading.Thread(target=_dummy_password_hash, name="olano-dummy-hash", daemon=True).start()


__all__ = [
    "DuplicateEmailError",
    "User",
    "UserStore",
    "get_user_store",
    "reset_user_store",
]


@dataclass
class UserStore:
    """Service layer over :class:`UsersDB`."""

    db: UsersDB

    # ------------------------------------------------------------- accounts

    def create_user(
        self,
        email: str,
        password: str,
        name: str = "",
        is_admin: bool = False,
        timezone: str = "UTC",
        username: str = "",
        title: str = "",
    ) -> User:
        return self.db.create_user(
            email=email,
            password_hash=hash_password(password),
            name=name,
            is_admin=is_admin,
            timezone=timezone,
            username=username,
            title=title,
        )

    def set_password(self, user_id: str, password: str) -> bool:
        user = self.db.get_user_by_id(user_id)
        changed = self.db.update_user(user_id, password_hash=hash_password(password))
        if changed and user is not None:
            _forget_initial_password(user.email)
        return changed

    def change_password(self, user_id: str, current_password: str, new_password: str) -> bool:
        """Verify the current password, then set a new one. Returns False on mismatch."""
        user = self.db.get_user_by_id(user_id)
        if user is None or not verify_password(current_password, user.password_hash):
            return False
        changed = self.db.update_user(user_id, password_hash=hash_password(new_password))
        if changed:
            _forget_initial_password(user.email)
        return changed

    def claim_provisioned_account(self, user_id: str, *, name: str, password: str) -> User:
        """Owner takes over the provisioned ``admin`` row — see ``UsersDB``."""
        claimed = self.db.claim_provisioned_account(
            user_id, name=name, password_hash=hash_password(password)
        )
        _forget_initial_password(claimed.email)
        return claimed

    def authenticate(self, email: str, password: str) -> User | None:
        """Return the user if email+password match and the account is active."""
        user = self.db.get_user_by_email(email)
        if user is None or not user.is_active:
            # Burn the same PBKDF2 cost as a real check, so the response time
            # does not reveal whether the address has an account.
            verify_password(password, _dummy_password_hash())
            return None
        if not verify_password(password, user.password_hash):
            return None
        return user

    # ------------------------------------------------------------- sessions

    def issue_session(self, user: User, ttl_days: int = DEFAULT_SESSION_TTL_DAYS) -> str:
        """Mint a raw session token (returned to the caller) and store its hash."""
        raw = new_session_token()
        self.db.create_session(user.id, hash_token(raw), ttl_days=ttl_days)
        return raw

    def resolve_session(self, raw_token: str) -> User | None:
        """Resolve a raw session token to an active user, or None.

        Expired sessions are deleted; inactive users are rejected.
        """
        if not raw_token:
            return None
        session = self.db.get_session(hash_token(raw_token))
        if session is None:
            return None
        if _is_expired(session.get("expires_at", "")):
            self.db.delete_session(session["token_hash"])
            return None
        user = self.db.get_user_by_id(session["user_id"])
        if user is None or not user.is_active:
            return None
        if _should_touch(session["token_hash"]):
            self.db.touch_session(session["token_hash"])
        return user

    def revoke(self, raw_token: str) -> bool:
        if not raw_token:
            return False
        token_hash = hash_token(raw_token)
        _LAST_TOUCH.pop(token_hash, None)
        return self.db.delete_session(token_hash)

    # ------------------------------------------------------------- invites

    def invite_user(
        self,
        email: str,
        name: str = "",
        is_admin: bool = False,
        created_by: str | None = None,
        ttl_days: int = 7,
        username: str = "",
        title: str = "",
    ) -> tuple[User, str, str]:
        """Create a pending (inactive) user and mint a single-use invite token.

        The account can't log in until the invite is accepted and a password set.
        Returns (user, raw_token, expires_at); only the token's hash is persisted.
        """
        user = self.db.create_user(
            email=email,
            password_hash=hash_password(new_session_token()),  # unusable placeholder
            name=name,
            is_admin=is_admin,
            is_active=False,
            username=username,
            title=title,
        )
        raw = new_session_token()
        expires_at = self.db.create_invite(
            user.id, hash_token(raw), created_by=created_by, ttl_days=ttl_days
        )
        return user, raw, expires_at

    def reinvite_user(
        self, user_id: str, created_by: str | None = None, ttl_days: int = 7
    ) -> tuple[str, str] | None:
        """Regenerate an invite for a pending user. Returns (raw_token, expires_at)."""
        user = self.db.get_user_by_id(user_id)
        if user is None or user.is_active:
            return None
        raw = new_session_token()
        expires_at = self.db.create_invite(
            user_id, hash_token(raw), created_by=created_by, ttl_days=ttl_days
        )
        return raw, expires_at

    def reset_password(
        self, user_id: str, created_by: str | None = None, ttl_days: int = 7
    ) -> tuple[str, str] | None:
        """Issue a set-password token for an existing user (admin-driven reset).

        Works for active or pending accounts; returns (raw_token, expires_at).
        """
        if self.db.get_user_by_id(user_id) is None:
            return None
        raw = new_session_token()
        expires_at = self.db.create_invite(
            user_id, hash_token(raw), created_by=created_by, ttl_days=ttl_days
        )
        return raw, expires_at

    def get_invite(self, raw_token: str) -> User | None:
        """Return the user for a valid, unexpired, unaccepted set-password token.

        Serves both invites (pending users) and admin password resets (active
        users) — the token only exists if an admin/system created it.
        """
        if not raw_token:
            return None
        inv = self.db.get_invite(hash_token(raw_token))
        if inv is None or inv.get("accepted_at") or _is_expired(inv.get("expires_at", "")):
            return None
        user = self.db.get_user_by_id(inv["user_id"])
        if user is None:
            return None
        return user

    def accept_invite(self, raw_token: str, password: str) -> User | None:
        """Set the password, activate the account, and consume the token."""
        user = self.get_invite(raw_token)
        if user is None:
            return None
        self.db.update_user(user.id, password_hash=hash_password(password), is_active=True)
        self.db.mark_invite_accepted(hash_token(raw_token))
        _forget_initial_password(user.email)
        return self.db.get_user_by_id(user.id)

    # ------------------------------------------------------------- access

    def can_chat(self, user: User, agent_name: str) -> bool:
        """Admins can chat with any agent; others need an access grant."""
        return bool(user.is_admin) or self.db.has_access(user.id, agent_name)

    def list_agent_names(self, user: User) -> list[str]:
        """Agents this user may see/use. Admins see every agent on disk."""
        if user.is_admin:
            return _all_agent_names()
        return [a["agent_name"] for a in self.db.list_agents_for_user(user.id)]

    # ------------------------------------------------------------- bootstrap

    def bootstrap_admin(
        self, email: str, password: str, name: str = "", username: str = ""
    ) -> User:
        """Create the first admin and grant them ownership of every existing agent.

        Pre-release seeding: the prior single operator becomes admin + owner of
        all agents already on disk so nothing is locked out.
        """
        admin = self.create_user(
            email=email, password=password, name=name, is_admin=True, username=username
        )
        for agent_name in _all_agent_names():
            self.db.set_owner(admin.id, agent_name, granted_by=admin.id)
        logger.info("Bootstrapped admin %s as owner of all existing agents", admin.email)
        return admin


# --------------------------------------------------------------------- helpers


def _forget_initial_password(email: str | None) -> None:
    """Drop quick-setup's one-time password note once that account has a new password.

    The note (``initial_password``) exists only while the generated password is
    the live one; any path that sets a password on the account retires it.
    Best-effort by construction: it must never fail a password change.
    """
    try:
        from olano.users import initial_password

        initial_password.forget(email)
    except Exception:  # pragma: no cover - defensive
        logger.debug("initial-password note cleanup skipped", exc_info=True)


# ``sessions.last_seen`` is a liveness breadcrumb nothing reads back, but
# writing it is an UPDATE + COMMIT — an fsync — and every authenticated request
# resolves a session. Remember the last write per token and skip the rest.
# The memo is bounded so a spray of distinct tokens can't grow it without limit;
# entries are only hints, so losing one costs a redundant write, never
# correctness.
_TOUCH_INTERVAL_SECONDS = 60.0
_TOUCH_MEMO_MAX = 1024
_LAST_TOUCH: dict[str, float] = {}


def _should_touch(token_hash: str) -> bool:
    now = time.monotonic()
    last = _LAST_TOUCH.get(token_hash)
    if last is not None and now - last < _TOUCH_INTERVAL_SECONDS:
        return False
    if len(_LAST_TOUCH) >= _TOUCH_MEMO_MAX:
        for key, seen in list(_LAST_TOUCH.items()):
            if now - seen >= _TOUCH_INTERVAL_SECONDS:
                _LAST_TOUCH.pop(key, None)
        if len(_LAST_TOUCH) >= _TOUCH_MEMO_MAX:
            _LAST_TOUCH.clear()
    _LAST_TOUCH[token_hash] = now
    return True


def _is_expired(expires_at_iso: str) -> bool:
    from datetime import datetime, timezone

    if not expires_at_iso:
        return True
    try:
        expires = datetime.fromisoformat(expires_at_iso)
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(tz=timezone.utc) >= expires


def _all_agent_names() -> list[str]:
    """All agent names on disk; isolated so a loader import error can't break auth."""
    try:
        from olano.config.loader import list_agents

        return list_agents()
    except Exception:  # pragma: no cover - defensive
        logger.debug("list_agents() failed during user-store access lookup", exc_info=True)
        return []


# --------------------------------------------------- process-wide singleton

_STORES: dict[str, UserStore] = {}


def get_user_store() -> UserStore:
    """Return a per-home cached :class:`UserStore`.

    Keyed by the resolved users.db path so each Olano home (and each test
    home) gets its own store.
    """
    path = str(get_olano_paths().users_db_path)
    store = _STORES.get(path)
    if store is None:
        store = UserStore(UsersDB(Path(path)))
        _STORES[path] = store
        warm_dummy_password_hash()
    return store


def reset_user_store() -> None:
    """Drop cached stores (used by tests and after home changes)."""
    _STORES.clear()
    _LAST_TOUCH.clear()
