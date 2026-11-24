"""The chat → dashboard magic link — bindings, mode resolution, and authorize.

``owner-login`` is the one magic-link flow that issues a **real dashboard
session** instead of storing a token or writing the vault. It lets the human who
owns an agent open the dashboard — system prompt, vault, connections, files — on
their phone, from the chat, with no separate password prompt after the first
time.

The whole design turns on one mapping that does not exist elsewhere: a chat
identity (``telegram:12345678``) → a dashboard user. This module owns it.

**The binding** (``<home>/handshake/owner_bindings.json``, 0600) maps
``<channel>:<stable-user-id>`` → ``{user_id, email, bound_at, bound_via}``. It
is kept out of ``config.yaml`` deliberately: config is declarative operator
intent (enabled / auto_bind / session_hours), a binding is runtime data written
when a user first proves themselves. It carries **no** password or session
material.

**Establishing a binding proves both ends**, since a binding is exactly as
powerful as the account it points at:

- the chat end is proven by the link itself — owner-gated mint (allowlist) + a
  single-use PIN;
- the account end is proven by the dashboard **password** the first time
  (``mode="login"``), or — only when the operator opts in and there is exactly
  one admin — by the single-admin assumption (``auto_bind``), or, on a brand-new
  single-user box with no accounts yet, by **bootstrapping** that admin
  (``mode="bootstrap"``, which also secures the previously-open dashboard).

Once bound, later links skip straight to ``mode="grant"``.

The session token never touches the link store, the audit, the logs, or the
chat — :func:`authorize` returns it to the route, which sets it as the
``__Host-`` cookie and 302s the browser to a **safe-enum** deep link (never a
caller-supplied URL, so the post-login redirect can't be abused).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from olano.config.home import get_olano_paths
from olano.config.system_settings import load_magic_link_settings
from olano.deployment.locks import deployment_write_lock
from olano.magic_links import store
from olano.magic_links.store import STATUS_DONE, STATUS_REVOKED, LinkRecord
from olano.memory.markdown_memory import atomic_write_text

logger = logging.getLogger(__name__)

# Page modes — what the owner sees after the PIN.
MODE_GRANT = "grant"  # binding known → one tap signs in
MODE_LOGIN = "login"  # prove the account once with its dashboard password
MODE_BOOTSTRAP = "bootstrap"  # no accounts yet → create the admin + secure the box
MODE_UNAVAILABLE = "unavailable"  # feature off / no admin to bind to

# How many wrong passwords on the login form before the link burns (reuses the
# PIN cap — same "credential guessing" semantics).
_MIN_PASSWORD_LEN = 8

# Deep-link targets. The section a link carries is one of these keys; the browser
# is only ever sent to a path we build, never one a caller supplied.
_SECTION_PATHS: dict[str, str] = {
    "overview": "/overview",
    "vault": "/vault",
    "connections": "/connections",
    "providers": "/providers",
    "config": "/config",
    "files": "/files",
    "agents": "/agents",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ── binding store ────────────────────────────────────────────────────────────


def binding_key(channel: str, user_id: str) -> str:
    """The stable key for a chat identity — id, never a renameable display name."""
    return f"{(channel or '').strip().lower()}:{(user_id or '').strip()}"


def _load_bindings() -> dict[str, Any]:
    path = get_olano_paths().magic_links_bindings_path
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("owner-login: owner_bindings.json unreadable — treating as empty")
        return {}
    return data.get("bindings", {}) if isinstance(data, dict) else {}


def _save_bindings(bindings: dict[str, Any]) -> None:
    path = get_olano_paths().magic_links_bindings_path
    atomic_write_text(path, json.dumps({"version": 1, "bindings": bindings}, indent=2) + "\n")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover
        pass


def get_binding(key: str) -> dict[str, Any] | None:
    record = _load_bindings().get(key)
    return record if isinstance(record, dict) else None


def set_binding(key: str, *, user_id: str, email: str, via: str) -> None:
    with deployment_write_lock():
        bindings = _load_bindings()
        bindings[key] = {
            "user_id": user_id,
            "email": email,
            "bound_at": _now_iso(),
            "bound_via": via,
        }
        _save_bindings(bindings)


def delete_binding(key: str) -> bool:
    with deployment_write_lock():
        bindings = _load_bindings()
        if key not in bindings:
            return False
        del bindings[key]
        _save_bindings(bindings)
    return True


def list_bindings() -> list[dict[str, Any]]:
    """Admin view: one row per binding (key + who it points at). No secrets."""
    return [{"key": key, **rec} for key, rec in sorted(_load_bindings().items())]


# ── mode resolution ──────────────────────────────────────────────────────────


def _resolved_user(user_id: str) -> Any | None:
    """The active dashboard user for ``user_id``, or None (deleted/deactivated)."""
    from olano.users.store import get_user_store

    user = get_user_store().db.get_user_by_id(user_id)
    return user if (user is not None and user.is_active) else None


def resolve_mode(record: LinkRecord) -> tuple[str, dict[str, Any]]:
    """Decide what the page should show, plus a small context dict.

    Returns one of MODE_* and context (e.g. the bound email, whether auto-bind
    applies). Never mutates anything — establishing a binding happens in
    :func:`authorize`.
    """
    settings = load_magic_link_settings().owner_login
    if not settings.enabled:
        return MODE_UNAVAILABLE, {"reason": "Dashboard magic links are disabled."}

    from olano.users.store import get_user_store

    store_users = get_user_store()
    if store_users.db.count_users() == 0:
        # A brand-new single-user box: the dashboard is open and there is no
        # account. The useful, secure move is to create the first admin.
        return MODE_BOOTSTRAP, {}

    key = binding_key(
        str(record.requested_by.get("channel") or ""),
        str(record.requested_by.get("user_id") or ""),
    )
    existing = get_binding(key)
    if existing and _resolved_user(str(existing.get("user_id") or "")):
        return MODE_GRANT, {"email": existing.get("email", ""), "key": key}

    # No usable binding. Auto-bind only when the operator opted in AND there is
    # exactly one admin to bind to unambiguously.
    if settings.auto_bind and store_users.db.count_admins() == 1:
        admin = next((u for u in store_users.db.list_users() if u.is_admin and u.is_active), None)
        if admin is not None:
            return MODE_GRANT, {"email": admin.email, "key": key, "auto_bind_to": admin.id}

    return MODE_LOGIN, {"key": key}


# ── authorize (route calls this; it sets the cookie from the result) ─────────


@dataclass(frozen=True)
class AuthorizeResult:
    ok: bool
    status_code: int = 200
    raw_token: str = ""  # the session token — set ONLY on ok, only for the cookie
    max_age: int = 0
    redirect: str = "/overview"
    message: str = ""  # user-facing summary (also the chat notice); no secrets
    error: str = ""


def _section_redirect(record: LinkRecord) -> str:
    section = str(record.params.get("section") or "overview").strip().lower()
    agent = str(record.params.get("agent") or "").strip()
    if section in ("prompt", "agent", "identity"):
        return f"/agents/{agent}" if agent else "/agents"
    return _SECTION_PATHS.get(section, "/overview")


def _grant(record: LinkRecord, user: Any, *, bind_key: str | None, via: str) -> AuthorizeResult:
    """Issue the session, record the binding, finish the link — the happy end."""
    from olano.users.store import get_user_store

    settings = load_magic_link_settings().owner_login
    raw = get_user_store().issue_session(user, ttl_days=max(1, settings.session_hours // 24 or 1))
    if bind_key:
        set_binding(bind_key, user_id=user.id, email=user.email, via=via)
    store.finish(
        record.id, status=STATUS_DONE, result=f"Signed in to the dashboard as {user.email}."
    )
    logger.info("owner-login: issued session for %s via %s", user.email, via)
    return AuthorizeResult(
        ok=True,
        raw_token=raw,
        max_age=settings.session_hours * 3600,
        redirect=_section_redirect(record),
        message=f"You're signed in to the dashboard as {user.email}.",
    )


def _register_password_failure(record: LinkRecord, max_attempts: int) -> int:
    """Count a wrong password; burn the link on exhaustion. Returns remaining."""
    remaining = {"v": max_attempts}

    def _apply(r: LinkRecord) -> None:
        r.grant_attempts += 1
        store.note(r, "password_failed", detail=f"attempt {r.grant_attempts}")
        left = max_attempts - r.grant_attempts
        remaining["v"] = max(0, left)
        if left <= 0:
            r.status = STATUS_REVOKED
            r.error = "Too many incorrect passwords."
            store.note(r, "burned", detail="password attempts exhausted")

    store.mutate(record.id, _apply)
    return remaining["v"]


def authorize(record: LinkRecord, fields: dict[str, str]) -> AuthorizeResult:
    """Complete an owner-login: verify → (bind) → issue session.

    Called by the route, which turns a successful result into a ``Set-Cookie`` +
    redirect. Never raises; every path returns an :class:`AuthorizeResult`.
    """
    if not record.serves_actions():
        return AuthorizeResult(False, 400, error="This link is no longer active.")

    settings = load_magic_link_settings()
    mode, ctx = resolve_mode(record)

    if mode == MODE_UNAVAILABLE:
        return AuthorizeResult(False, 400, error=ctx.get("reason", "Not available."))

    if mode == MODE_GRANT:
        # Binding already known (or auto-bind). One tap, no password.
        auto_id = ctx.get("auto_bind_to")
        user = _resolved_user(str(auto_id)) if auto_id else None
        if user is None:
            existing = get_binding(str(ctx.get("key") or ""))
            user = _resolved_user(str(existing.get("user_id") or "")) if existing else None
        if user is None:  # binding went stale between resolve and grant
            return AuthorizeResult(False, 409, error="Your account is no longer available.")
        return _grant(
            record,
            user,
            bind_key=ctx.get("key") if auto_id else None,
            via="auto" if auto_id else "binding",
        )

    if mode == MODE_BOOTSTRAP:
        return _bootstrap(record, fields, ctx)

    # mode == MODE_LOGIN: prove the account with its password, then bind.
    from olano.users.store import get_user_store

    email = str(fields.get("email") or "").strip()
    password = str(fields.get("password") or "")
    if not email or not password:
        return AuthorizeResult(False, 400, error="Enter your dashboard email and password.")
    user = get_user_store().authenticate(email, password)
    if user is None:
        remaining = _register_password_failure(record, settings.max_pin_attempts)
        if remaining <= 0:
            return AuthorizeResult(False, 403, error="Too many attempts — this link is disabled.")
        plural = "tries" if remaining != 1 else "try"
        return AuthorizeResult(
            False, 403, error=f"Wrong email or password. {remaining} {plural} left."
        )
    return _grant(record, user, bind_key=str(ctx.get("key") or ""), via="password")


def _bootstrap(record: LinkRecord, fields: dict[str, str], ctx: dict[str, Any]) -> AuthorizeResult:
    """First-run: create the admin, enable multi-user, bind, and sign in.

    Turns a wide-open single-user dashboard into a secured one with the chat
    owner as its admin — the useful thing to do when someone asks for the
    dashboard on a box that has no accounts yet.
    """
    from olano.runtime.auth import set_multi_user_enabled
    from olano.users.db import DuplicateEmailError, DuplicateUsernameError
    from olano.users.store import get_user_store

    users = get_user_store()
    if users.db.count_users() != 0:  # raced with another bootstrap
        return AuthorizeResult(False, 409, error="This dashboard has just been set up — try again.")
    email = str(fields.get("email") or "").strip()
    password = str(fields.get("password") or "")
    if not email or "@" not in email:
        return AuthorizeResult(False, 400, error="Enter a valid email address.")
    if len(password) < _MIN_PASSWORD_LEN:
        return AuthorizeResult(
            False, 400, error=f"Choose a password of at least {_MIN_PASSWORD_LEN} characters."
        )
    username = str(fields.get("username") or "").strip()
    try:
        admin = users.bootstrap_admin(email=email, password=password, username=username)
    except DuplicateEmailError:
        return AuthorizeResult(False, 409, error="That account already exists.")
    except DuplicateUsernameError:
        return AuthorizeResult(False, 409, error="That handle is already taken — pick another.")
    set_multi_user_enabled(True)
    # The first-run setup code guards the dashboard's create-account screen; an
    # owner who set the box up from a verified chat instead has used it up too.
    try:
        from olano.runtime import setup_code

        setup_code.consume()
    except Exception:  # pragma: no cover - best-effort tidy-up
        logger.debug("could not retire the setup code after owner-login bootstrap", exc_info=True)
    key = binding_key(
        str(record.requested_by.get("channel") or ""),
        str(record.requested_by.get("user_id") or ""),
    )
    result = _grant(record, admin, bind_key=key, via="bootstrap")
    logger.info("owner-login: bootstrapped admin %s and enabled multi-user from chat", admin.email)
    return result
