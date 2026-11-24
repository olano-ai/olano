"""Human-user management: accounts, invites/resets, roles, per-agent access.

Runtime-free wrapper over :mod:`olano.users.store` for the AgentFather tool
surface, mirroring the dashboard admin handlers in ``runtime/auth.py`` (same
guards: user-cap quota, last-admin protection, immutable usernames).

Deliberately **password-less**: creating a user always mints a pending account
plus a single-use, expiring invite link (and a QR image of it), and a password
reset re-issues the same kind of link — the person sets their own password in
the browser and is signed in. A password must never pass through a chat
transcript, which is where every tool argument and result ends up.
"""

from __future__ import annotations

import time
from typing import Any

from olano.users.db import UI_MODES, User
from olano.users.store import get_user_store

#: Matches the store's invite/reset default.
INVITE_TTL_DAYS = 7

_IDENT_HELP = "pass an email, @username, or user id (list_users shows all three)"


def _resolve_user(ident: str) -> User:
    """Resolve an email / ``@username`` / user id to a :class:`User`.

    All three are unique in users.db, so one flexible argument beats three
    optional ones. Raises :class:`ValueError` naming the accepted forms when
    nothing matches.
    """
    ident = str(ident or "").strip()
    if not ident:
        raise ValueError(f"no user given — {_IDENT_HELP}")
    db = get_user_store().db
    user = db.get_user_by_id(ident)
    if user is not None:
        return user
    if "@" in ident and not ident.startswith("@"):
        user = db.get_user_by_email(ident)
        if user is not None:
            return user
    handle = ident.lstrip("@").strip().lower()
    if handle:
        for candidate in db.list_users():
            if candidate.username.lower() == handle:
                return candidate
    raise ValueError(f"no user matches {ident!r} — {_IDENT_HELP}")


def _user_view(user: User) -> dict[str, Any]:
    db = get_user_store().db
    data = user.public()
    data["status"] = "active" if user.is_active else "pending"
    data["agents"] = db.list_agents_for_user(user.id)
    return data


def _save_qr_png(agent: str, url: str, prefix: str = "invite-qr") -> str:
    """Render the invite/reset URL as a QR into the agent's workspace.

    An absolute path inside the workspace is what the chat's media detection
    picks up from the tool result, so the QR shows as an image in the
    dashboard conversation and ships as a picture on messaging channels
    (mirrors ``self_config_tools._save_qr_png``).
    """
    import segno

    from olano.config.home import get_olano_paths

    out_dir = get_olano_paths().agent_workspace_dir(agent) / "uploads"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{prefix}-{int(time.time())}.png"
    segno.make(url, error="m").save(str(path), scale=6, border=2)
    return str(path)


def _invite_payload(raw_token: str, expires_at: str, *, qr_agent: str = "") -> dict[str, Any]:
    """The deliverable of an invite/reset: link (absolute when the deployment's
    public origin is known), expiry, and a QR PNG rendered into ``qr_agent``'s
    workspace. Origin resolution is the same one magic links use."""
    path = f"/invite/{raw_token}"
    origin: str | None = None
    try:
        from olano.core.mcp_setup import public_origin

        origin = public_origin()
    except Exception:
        origin = None

    payload: dict[str, Any] = {
        "path": path,
        "url": f"{origin}{path}" if origin else None,
        "expires_at": expires_at,
        "single_use": True,
        "ttl_days": INVITE_TTL_DAYS,
        "qr_png": None,
        "notes": [],
    }
    if payload["url"] is None:
        payload["notes"].append(
            "no public origin is known for this deployment, so no absolute URL or QR "
            "could be built — set LIONCLAW_PUBLIC_URL (or open the dashboard once from "
            "a browser) and re-issue; the path above still works against the "
            "dashboard's own address"
        )
    elif qr_agent:
        try:
            payload["qr_png"] = _save_qr_png(qr_agent, payload["url"])
        except Exception as exc:  # QR is a convenience — the link still stands
            payload["notes"].append(f"QR image could not be generated: {exc}")
    return payload


def list_users() -> list[dict[str, Any]]:
    """Every dashboard human: public fields, active/pending status, and their
    per-agent access grants."""
    return [_user_view(u) for u in get_user_store().db.list_users()]


def get_user(user: str) -> dict[str, Any]:
    """One human's details: public fields, status, and per-agent access."""
    resolved = _resolve_user(user)
    data = _user_view(resolved)
    if resolved.is_admin:
        data["note"] = "platform admin — implicitly owns every agent, no per-agent grants needed"
    return data


def create_user(
    email: str,
    *,
    name: str = "",
    username: str = "",
    is_admin: bool = False,
    timezone: str = "UTC",
    created_by: str = "agentfather",
    qr_agent: str = "",
    title: str = "",
) -> dict[str, Any]:
    """Invite a new human: a pending account + a single-use set-password link.

    Never takes or sets a password. Refuses to create the very FIRST account —
    bootstrap (password entry, enabling multi-user login, seeding agent
    ownership) is an interactive flow owned by the dashboard first-run setup
    and ``olano users create-admin``. Enforces the deployment's user quota
    exactly as the dashboard does.
    """
    email = str(email or "").strip()
    if not email or "@" not in email:
        raise ValueError("a valid login email is required")

    store = get_user_store()
    if store.db.count_users() == 0:
        raise ValueError(
            "no accounts exist yet — the first admin is created interactively "
            "(dashboard first-run setup, or `olano users create-admin`), not by "
            "invite; this tool adds users to an already-initialized deployment"
        )
    from olano.deployment import enforcement as dep_enforcement

    decision = dep_enforcement.can_create_user(len(store.db.list_users()))
    if not decision.allowed:
        raise PermissionError(f"{decision.message} {decision.remediation}".strip())

    user, raw_token, expires_at = store.invite_user(
        email=email,
        name=str(name or ""),
        is_admin=bool(is_admin),
        created_by=created_by,
        username=str(username or ""),
        title=str(title or ""),
    )
    tz = str(timezone or "").strip()
    if tz and tz != "UTC":
        store.db.update_user(user.id, timezone=tz)
        user = store.db.get_user_by_id(user.id) or user

    result: dict[str, Any] = {
        "user": user.public(),
        "status": "pending",
        "invite": _invite_payload(raw_token, expires_at, qr_agent=qr_agent),
    }
    try:  # guarded: the resolved flag lives in the runtime auth layer
        from olano.runtime.auth import multi_user_enabled

        if not multi_user_enabled():
            result["notes"] = [
                "multi-user login is currently disabled (config.yaml "
                "multi_user_enabled) — the invite works, but this user cannot "
                "sign in until it is enabled"
            ]
    except Exception:
        pass
    return result


def reset_user_password(
    user: str,
    *,
    created_by: str = "agentfather",
    qr_agent: str = "",
) -> dict[str, Any]:
    """Issue a fresh single-use set-password link (+ QR) for an existing human.

    Works for active accounts (a reset) and pending ones (a re-invite) — the
    person opens the link, sets a new password, and is signed in. Any previous
    invite/reset link for the same user is voided.
    """
    resolved = _resolve_user(user)
    minted = get_user_store().reset_password(resolved.id, created_by=created_by)
    if minted is None:  # pragma: no cover — the user was just resolved
        raise ValueError(f"user {resolved.email} no longer exists")
    raw_token, expires_at = minted
    return {
        "user": resolved.public(),
        "status": "active" if resolved.is_active else "pending",
        "invite": _invite_payload(raw_token, expires_at, qr_agent=qr_agent),
        "notes": [
            "single-use link: opening it sets a new password and signs the person "
            "in; any earlier invite/reset link for this user is now void"
        ],
    }


def set_user_role(user: str, role: str) -> dict[str, Any]:
    """Make a human a platform ``admin`` or a plain ``member``.

    Refuses to demote the last active admin (a deployment must never lock
    itself out).
    """
    role = str(role or "").strip().lower()
    if role not in ("admin", "member"):
        raise ValueError(f"role must be 'admin' or 'member', got {role!r}")
    resolved = _resolve_user(user)
    store = get_user_store()
    make_admin = role == "admin"
    if not make_admin and resolved.is_admin and store.db.count_admins() <= 1:
        raise ValueError("cannot demote the last active admin — promote someone else first")
    store.db.update_user(resolved.id, is_admin=make_admin)
    updated = store.db.get_user_by_id(resolved.id) or resolved
    return {"user": updated.public(), "role": "admin" if updated.is_admin else "member"}


def update_user(user: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Update a human's editable fields: ``name``, ``title``, ``email``,
    ``is_active``, ``can_terminal``, ``ui_mode`` (advanced|simple),
    ``langfuse_links`` (show Langfuse links in the dashboard), ``timezone``.

    Refuses — with the right tool named — the fields that must not go through
    here: ``username`` (permanent; it anchors the ``human-<username>`` issue
    principal), ``password`` (use :func:`reset_user_password`), ``is_admin``
    (use :func:`set_user_role`).
    """
    resolved = _resolve_user(user)
    if not isinstance(updates, dict) or not updates:
        raise ValueError("updates must be a non-empty object of fields to change")

    refusals = {
        "username": (
            "usernames are permanent (they anchor the human-<username> issue "
            "principal); change 'name' instead"
        ),
        "password": (
            "passwords never pass through chat — use reset_user_password to "
            "issue a set-password link"
        ),
        "is_admin": "role changes go through set_user_role",
    }
    allowed = {
        "name",
        "title",
        "email",
        "is_active",
        "can_terminal",
        "ui_mode",
        "langfuse_links",
        "timezone",
    }
    for key in updates:
        if key in refusals:
            raise ValueError(f"'{key}' cannot be updated here: {refusals[key]}")
        if key not in allowed:
            raise ValueError(f"unknown field {key!r} (allowed: {', '.join(sorted(allowed))})")

    if "name" in updates and not str(updates["name"] or "").strip():
        raise ValueError("name cannot be blank — it may change but never vanish")
    if "ui_mode" in updates and updates["ui_mode"] not in UI_MODES:
        raise ValueError(f"ui_mode must be one of: {', '.join(UI_MODES)}")

    store = get_user_store()
    disabling = updates.get("is_active") is False or (
        "is_active" in updates and not updates["is_active"]
    )
    if disabling and resolved.is_admin and store.db.count_admins() <= 1:
        raise ValueError("cannot disable the last active admin")

    fields = dict(updates)
    for flag in ("is_active", "can_terminal", "langfuse_links"):
        if flag in fields:
            fields[flag] = bool(fields[flag])
    store.db.update_user(resolved.id, **fields)
    updated = store.db.get_user_by_id(resolved.id) or resolved

    result: dict[str, Any] = {"user": updated.public()}
    if disabling:
        result["notes"] = [
            "account disabled — the user's existing dashboard sessions stop "
            "resolving immediately; re-enable with {'is_active': true}"
        ]
    return result


def delete_user(user: str) -> dict[str, Any]:
    """Permanently delete a human account.

    Sessions, invites, per-agent access grants and preferences go with it;
    issue history keeps referencing the retired ``human-<username>`` principal.
    Refuses to delete the last active admin.
    """
    resolved = _resolve_user(user)
    store = get_user_store()
    if resolved.is_admin and store.db.count_admins() <= 1:
        raise ValueError("cannot delete the last active admin — promote someone else first")
    store.db.delete_user(resolved.id)
    return {
        "deleted": resolved.public(),
        "notes": [
            "sessions, invites, per-agent access grants and preferences were removed "
            f"with the account; issue history keeps naming human-{resolved.username}"
        ],
    }


def grant_agent_access(
    user: str,
    agent: str,
    role: str = "member",
    *,
    granted_by: str = "agentfather",
) -> dict[str, Any]:
    """Give a human access to one agent: ``owner`` (may manage that agent's
    access and settings) or ``member`` (may chat with it).

    Role and agent are validated loudly — a typo must not silently downgrade a
    grant (the dashboard API coerces; this surface refuses instead).
    """
    role = str(role or "").strip().lower()
    if role not in ("owner", "member"):
        raise ValueError(f"role must be 'owner' or 'member', got {role!r} — refusing to guess")
    resolved = _resolve_user(user)
    agent = str(agent or "").strip()
    from olano.config.loader import find_agent_config_path

    if not agent or find_agent_config_path(agent) is None:
        raise ValueError(f"unknown agent: {agent!r} (list_agents shows every agent)")

    store = get_user_store()
    store.db.grant_access(resolved.id, agent, role=role, granted_by=granted_by)
    result: dict[str, Any] = {
        "granted": {"user": resolved.username, "agent": agent, "role": role},
        "user": _user_view(store.db.get_user_by_id(resolved.id) or resolved),
    }
    if resolved.is_admin:
        result["notes"] = [
            "this user is a platform admin and already reaches every agent; the "
            "explicit grant is recorded anyway"
        ]
    return result


def revoke_agent_access(user: str, agent: str) -> dict[str, Any]:
    """Remove a human's access grant to one agent."""
    resolved = _resolve_user(user)
    agent = str(agent or "").strip()
    store = get_user_store()
    removed = store.db.revoke_access(resolved.id, agent)
    result: dict[str, Any] = {
        "ok": bool(removed),
        "revoked": {"user": resolved.username, "agent": agent},
        "user": _user_view(store.db.get_user_by_id(resolved.id) or resolved),
    }
    notes: list[str] = []
    if not removed:
        notes.append("no grant existed for that user/agent pair — nothing changed")
    if resolved.is_admin:
        notes.append("this user is a platform admin and keeps implicit access to every agent")
    if notes:
        result["notes"] = notes
    return result


def access_overview() -> dict[str, Any]:
    """Every agent's human access in one response.

    ``agents`` lists per-agent explicit ``owners``/``users`` (grant rows);
    ``admin_ids`` are the platform admins, who implicitly own every agent
    without any row — report both kinds so nobody under-counts who holds the
    keys. Users appear once in a flat roster and are referenced by id.
    """
    from olano.admin.agents import list_agents as _list_agents

    store = get_user_store()
    users = store.db.list_users()
    known = {u.id for u in users}
    admin_ids = [u.id for u in users if u.is_admin]

    names = sorted({str(a.get("name") or "") for a in _list_agents()} - {""})
    agents = []
    for name in names:
        owners: list[str] = []
        members: list[str] = []
        for row in store.db.list_users_for_agent(name):
            uid = row["user_id"]
            if uid not in known:  # stale grant for a deleted account
                continue
            (owners if row["role"] == "owner" else members).append(uid)
        agents.append({"name": name, "owners": owners, "users": members})

    return {
        "agents": agents,
        "admin_ids": admin_ids,
        "users": [u.public() for u in users],
        "note": "platform admins (admin_ids) implicitly own every agent",
    }
