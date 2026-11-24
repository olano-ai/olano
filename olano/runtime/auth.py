"""Authentication & authorization for the runtime API (multi-user control layer).

Resolves the current human from a session cookie, exposes FastAPI dependencies
(`require_user` / `require_admin` / `assert_agent_access`), and registers the
auth + admin-user-management endpoints. When multi-user mode is off (the
default) the platform behaves as a single-owner install and these guards treat
the caller as an implicit admin.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel
from starlette.requests import HTTPConnection

from olano.security.request_context import get_request_context
from olano.users import onboarding
from olano.users.db import (
    DEFAULT_UI_MODE,
    UI_MODES,
    DuplicateEmailError,
    DuplicateUsernameError,
    User,
    normalize_ui_mode,
)
from olano.users.store import get_user_store

logger = logging.getLogger(__name__)

# Session cookie names. On HTTPS installs the cookie is issued under the
# ``__Host-`` prefix (requires ``Secure``, ``Path=/``, no ``Domain``), which
# browsers refuse to accept from anywhere but this exact host — so no sibling
# subdomain under a shared parent zone (customer VMs run customer-controlled
# agents and are semi-hostile neighbours) can plant or fixate a session cookie
# on this deployment ("cookie tossing"). Plain-HTTP installs (localhost dev,
# e2e) keep the legacy un-prefixed name — ``__Host-`` cookies are dropped by
# browsers on insecure origins.
SESSION_COOKIE = "lc_session"
SESSION_COOKIE_SECURE = "__Host-lc_session"
_SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days


def secure_cookies_enabled() -> bool:
    """Whether sessions are issued as ``Secure`` ``__Host-`` cookies.

    ``LIONCLAW_SECURE_COOKIES`` (1/true/on or 0/false/off) is the explicit
    override; otherwise HTTPS-ness is inferred from ``LIONCLAW_PUBLIC_URL`` —
    every production path (managed cloud-init, deploy/env-lib.sh) pins it to
    ``https://<host>``, while dev/e2e runs on ``http://localhost`` leave it
    unset. Read per-call, not at import: the runtime overlays
    ``/opt/olano/olano.env`` onto its environment during startup.
    """
    override = os.environ.get("LIONCLAW_SECURE_COOKIES", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    return os.environ.get("LIONCLAW_PUBLIC_URL", "").strip().lower().startswith("https://")


def session_cookie_name() -> str:
    """The cookie name sessions are issued under right now."""
    return SESSION_COOKIE_SECURE if secure_cookies_enabled() else SESSION_COOKIE


def session_token_from_cookies(cookies: Mapping[str, str]) -> str | None:
    """The raw session token from a request's cookies, or ``None``.

    Prefers the ``__Host-`` cookie and falls back to the legacy name so
    sessions issued before an install upgraded to secure cookies keep working
    until they rotate; a legacy cookie can never shadow a ``__Host-`` one.
    """
    return cookies.get(SESSION_COOKIE_SECURE) or cookies.get(SESSION_COOKIE) or None


# Reachable without an authenticated session (when users already exist).
# `sign-in-hint` is read BY the sign-in screen, so it cannot sit behind it; it
# answers one box-wide fact and never anything about a person (see its handler).
PUBLIC_API_PATHS = {
    "/api/auth/login",
    # The demo deployment's passwordless sign-in (runtime/demo_access.py); it
    # answers 404 on every box whose environment does not name a demo account.
    "/api/auth/demo",
    "/api/auth/bootstrap",
    "/api/auth/sign-in-hint",
    "/api/auth/setup-status",
    "/api/status",
}
# Reachable during first-run setup (no users yet). `username-available` is
# deliberately HERE and not in PUBLIC_API_PATHS: with zero users there is
# nothing to enumerate, but once accounts exist an open handle probe would
# leak who has one — so after setup it requires a session like any other
# path, which still covers the admin form and the onboarding wizard.
SETUP_API_PATHS = {
    "/api/auth/bootstrap",
    "/api/auth/setup-status",
    "/api/auth/username-available",
    "/api/status",
}

# Synthetic principal used when multi-user mode is off — the single operator is
# an implicit admin so every admin/owner guard passes.
_OWNER = User(
    id="owner",
    email="owner@localhost",
    # "owner" and not slugify("Owner") by coincidence — they agree, and they
    # must: this synthetic account's principal is `human-owner`, which is also
    # what the CLI defaults to (`cli/issues_cmd.py`). A single-user box and its
    # own CLI have to file issues as the same person.
    username="owner",
    name="Owner",
    password_hash="",
    is_admin=True,
    is_active=True,
    timezone="UTC",
    created_at="",
    # Single-user installs have no users table to read a mode from, so the
    # implicit operator is pinned to the full dashboard. Flipping a
    # single-user box to the simple shell is a config choice, not a user
    # record — see resolve_ui_mode().
    ui_mode=DEFAULT_UI_MODE,
)


# ── config flag ────────────────────────────────────────────────────────────


def multi_user_enabled() -> bool:
    """Whether per-human login (and per-agent access control) is enforced.

    An explicit ``multi_user_enabled`` in ``config.yaml`` always wins, in both
    directions. When the key is UNSET, the default depends on how the box was
    installed: a **managed deployment** (control-plane-provisioned — posture
    "enforced" or "quarantine") defaults to **True**, so a cloud box whose
    first-boot provisioning hasn't written the flag yet gates visitors behind
    the account-setup/login screens instead of handing every anonymous
    browser the implicit owner's admin powers; a manual install defaults to
    False (the classic single-owner posture). ``deployment_status()`` never
    raises and is memoized (~2s), and any failure here reads as the manual
    default so a broken entitlements layer can't lock out a self-hosted box.

    One exception to the manual default: an unmanaged box whose environment
    carries a first-run setup code (``OLANO_SETUP_CODE``, which the self-host
    ``setup.sh`` writes) defaults to **True** too. The installer that put it
    there expects other people to be able to reach the box, so an anonymous
    visitor must meet the setup screen (code required) rather than the
    implicit owner's admin powers. See ``runtime/setup_code.py``.
    """
    value = None
    try:
        from olano.config.home import ensure_olano_home, read_home_config_doc

        paths = ensure_olano_home()
        raw = read_home_config_doc(paths.root) if paths.config_path.exists() else {}
        value = raw.get("multi_user_enabled")
    except Exception:  # pragma: no cover - unreadable config → posture default
        value = None
    if value is not None:
        return bool(value)
    try:
        from olano.deployment import resolver

        if resolver.deployment_status().posture != "open":
            return True
    except Exception:  # pragma: no cover - defensive
        return False
    from olano.runtime import setup_code

    return setup_code.configured_in_env()


def set_multi_user_enabled(value: bool) -> None:
    # update_home_config, not save_home_config: the latter rewrites the whole
    # document from the HomeConfig dataclass and would drop every block it
    # doesn't own (cognition, disk_monitor, magic_links, …). This runs during
    # first-run bootstrap — including the owner-login "secure this dashboard"
    # path — so it must preserve those regions.
    from olano.config.home import update_home_config

    update_home_config({"multi_user_enabled": value})


# ── request-context construction (called from the api.py middleware) ─────────

# Where a request memoizes the human it resolved from its session cookie.
_SESSION_USER_ATTR = "lc_session_user"
_UNRESOLVED = object()


def _resolve_session_user(request: HTTPConnection) -> User | None:
    """The authenticated human behind ``request``, resolved at most once.

    ``access_gate`` and ``context_kwargs_for_request`` both need the principal
    and each resolution is a sqlite round-trip, so the first of them memoizes it
    on ``request.state``. Connections that never pass through the gate — the
    terminal WebSocket handshake, direct calls — carry no memo and resolve here.
    """
    state = getattr(request, "state", None)
    cached = _UNRESOLVED if state is None else getattr(state, _SESSION_USER_ATTR, _UNRESOLVED)
    # A memo is a User or None; anything else (a test double's auto-attribute)
    # is not a resolution and must not be mistaken for an anonymous caller.
    if cached is None or isinstance(cached, User):
        return cached
    raw = session_token_from_cookies(request.cookies)
    user = get_user_store().resolve_session(raw) if raw else None
    if state is not None:
        setattr(state, _SESSION_USER_ATTR, user)
    return user


def context_kwargs_for_request(request: HTTPConnection, session_id: str) -> dict[str, Any]:
    """Resolve the session cookie to RequestContext kwargs.

    Single-user mode → the implicit owner/admin. Multi-user mode → the real
    authenticated human, or an anonymous context when no valid session.

    Accepts any Starlette ``HTTPConnection`` (an HTTP ``Request`` or a
    ``WebSocket``) since it only reads ``.cookies`` — the terminal WebSocket
    route reuses it to authenticate its handshake.
    """
    if not multi_user_enabled():
        return dict(
            source="api",
            channel="api",
            user_id="owner",
            session_id=session_id,
            authenticated=True,
            is_owner=True,
            is_admin=True,
            is_group=False,
        )

    user = _resolve_session_user(request)
    if user is None:
        return dict(
            source="api",
            channel="api",
            user_id="anonymous",
            session_id=session_id,
            authenticated=False,
            is_owner=False,
            is_admin=False,
            is_group=False,
        )

    store = get_user_store()
    return dict(
        source="api",
        channel="api",
        user_id=user.id,
        session_id=session_id,
        authenticated=True,
        is_owner=user.is_admin,
        is_admin=user.is_admin,
        is_group=False,
        metadata={
            "email": user.email,
            "username": user.username,
            "name": user.name,
            "timezone": user.timezone,
            "allowed_agents": store.list_agent_names(user),
        },
    )


def is_external_access_path(path: str) -> bool:
    """True for the inbound surfaces external callers reach with their OWN
    credentials (an MCP/A2A bearer token, an OAuth client), never a dashboard
    session: the MCP endpoint, the per-agent A2A endpoints, Olano's own OAuth
    authorization server and every discovery document. All of them run their
    own authentication in ``olano/mcp_server`` / ``olano/a2a/server``.

    Deliberately narrow: ``/api/mcp/directory`` and ``/api/mcp/oauth/*`` (the
    agents' *outbound* MCP sign-ins), ``/api/a2a/threads`` / ``/api/a2a/activity``
    (the Agent to Agent view) and ``/api/oauth/consent/*`` (the admin consent
    page's API) are dashboard surfaces and stay behind the dashboard gates.
    """
    if path in (
        "/api/mcp",
        "/api/mcp/",
        "/api/mcp/.well-known/oauth-protected-resource",
    ):
        return True
    if path.startswith("/.well-known/"):
        return True
    if path.startswith("/api/oauth/") and not path.startswith("/api/oauth/consent"):
        return True
    return path.startswith("/api/a2a/agents/")


# Kept for callers/tests written against the first (MCP-only) name.
is_mcp_server_path = is_external_access_path


def access_gate(request: Request) -> Response | None:
    """Return a 401 JSONResponse if the request must be blocked, else None.

    Enforced centrally in the api.py middleware (only when multi-user is on):
      * first-run (no users): only setup paths are reachable → else `setup_required`
      * users exist but no session: protected /api/ paths → `auth_required`
    """
    if not multi_user_enabled():
        return None

    from starlette.responses import JSONResponse

    path = request.url.path
    is_api = path.startswith("/api/")
    # Accepting an invite happens before a user can authenticate — always public.
    is_invite = path.startswith("/api/auth/invite/")
    # OAuth redirects (Google / connector broker) arrive from the provider's
    # browser redirect without a session cookie; they self-authenticate by
    # matching the unguessable ?state= of a pending in-memory flow and only
    # render a static status page, so they must stay reachable when gated.
    is_oauth_callback = path.startswith("/api/auth/") and path.endswith("/callback")
    # Handshake links are opened from a chat by someone who has no dashboard
    # account at all (that is the entire point). They self-authenticate on the
    # unguessable token in the path plus a PIN, and can only ever drive the one
    # flow frozen into them at mint time — see olano/magic_links/routes.py.
    is_handshake = path.startswith("/api/connect/")
    # The inbound MCP/A2A endpoints and the OAuth authorization server
    # authenticate external clients with their own tokens — never a session.
    is_mcp = is_external_access_path(path)
    user_count = get_user_store().db.count_users()

    if user_count == 0:
        if is_api and path not in SETUP_API_PATHS and not is_handshake and not is_mcp:
            return JSONResponse(status_code=401, content={"detail": "setup_required"})
        return None

    user = _resolve_session_user(request)
    if (
        is_api
        and user is None
        and path not in PUBLIC_API_PATHS
        and not is_invite
        and not is_oauth_callback
        and not is_handshake
        and not is_mcp
    ):
        return JSONResponse(status_code=401, content={"detail": "auth_required"})
    # Members are barred from admin-only surfaces (agent config, vault, system,
    # cognition, user management). Owner/admin grant checks happen in-handler.
    if user is not None and not user.is_admin and _member_forbidden(request):
        return JSONResponse(status_code=403, content={"detail": "admin_required"})
    return None


# Never the loop's default executor: this runs before *every* request reaches
# its handler, and the default pool is `min(32, cpu_count + 4)` slots (6 on a
# 2-core box) shared with agent tool calls, `shutil.rmtree` of an agent dir, npm
# installs and page-password KDFs. Queued behind a few of those, no request
# could even be gated — the whole-API stall, moved off the loop and into the
# pool. A private pool keeps the gate reachable no matter what else is running;
# it stays small because each task is one or two sqlite reads.
_AUTH_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lc-auth")


async def resolve_request_auth(
    request: Request, session_id: str, *, gate: bool = True
) -> tuple[Response | None, dict[str, Any]]:
    """Gate a request and build its RequestContext kwargs, off the event loop.

    Both halves are blocking I/O (home-config read, sqlite session + grant
    lookups) and they run on every single request, including the dashboard's
    2.5s ``/api/status`` poll. The kwargs come back *unbound* on purpose:
    ``push_request_context`` sets a contextvar the downstream handler has to
    see, so the caller binds them on the loop. ``gate=False`` skips the
    multi-user gate for self-authenticating ingress paths.

    Single-user installs stay inline: both halves short-circuit on the
    ``multi_user_enabled`` flag without touching sqlite, so a thread hop would
    cost more than the work it moves.
    """
    if not multi_user_enabled():
        return _request_auth_sync(request, session_id, gate)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_AUTH_EXECUTOR, _request_auth_sync, request, session_id, gate)


def _request_auth_sync(
    request: Request, session_id: str, gate: bool
) -> tuple[Response | None, dict[str, Any]]:
    if gate:
        blocked = access_gate(request)
        if blocked is not None:
            return blocked, {}
    return None, context_kwargs_for_request(request, session_id)


# Surfaces only the admin may touch (any method).
_ADMIN_ONLY_PREFIXES = (
    "/api/admin/",
    "/api/vault/",
    "/api/cognition/",
    "/api/onboarding",
    "/api/system",
    # Whole-OS-process lifecycle (restart / shutdown / self-update). Strictly
    # more powerful than /api/system/* (which only cycles agents in-process),
    # so it must be at least as locked down.
    "/api/platform",
    "/api/catalog/tools/disabled",
    # Page-hosting management: listing/deleting published pages and (above all)
    # setting the viewer password are owner surfaces.
    "/api/pages",
    # Magic-link management: minting a link grants whoever holds it the power
    # to connect an account, write a vault secret, or edit a file, so listing
    # and minting are owner surfaces. (The public /api/connect/<token> pages
    # that links point at are exempted above — they carry their own gate.)
    # Both prefixes are listed: `/api/handshake` is the pre-rename alias and
    # must stay just as gated as the name that replaced it.
    "/api/magic-links",
    "/api/handshake",
    # Process-wide operator surfaces: the global runtime log buffer and the
    # memory diagnostics (both span every agent and cannot be filtered
    # per-grant — log lines are free text).
    "/api/logs",
    "/api/diagnostics",
    # Minting/revoking MCP bearer tokens publishes agents to the outside world.
    "/api/mcp-server",
    # Approving an OAuth client's access to agents is an admin decision.
    "/api/oauth/consent",
    # The revision history's GitHub sync: connecting ships config and memory
    # off the box, and a pull writes into live configuration.
    "/api/revisions/remote",
    # The Olano account API key a self-hosted box spends from: pasting one
    # points the box at an account wallet. The EXACT path, because the
    # member-readable /api/olano/credits and /api/olano/services/usage share
    # the segment (routes/credits.py).
    "/api/olano/credits-key",
    # Langfuse tracing: the keys read every exported trace, and these routes
    # decide whether agent conversations leave the box at all.
    "/api/observability",
)


def _readonly_member_forbidden(request: Request) -> bool:
    """True if a non-admin member's request would write or persist.

    Non-admin members run in a *read-only environment*: they may browse the
    workspace, read memory for context, and read a file's bytes — the chart an
    agent just made for them renders inline in chat and downloads from its
    card — but must not be able to mutate the file tree or edit memory
    markdown. Reads stay open: ``GET /api/files``, ``GET /api/files/{path}``
    (JSON preview), ``GET /api/files-raw/{path}`` (bytes, scoped to the
    workspaces the member's grants cover by ``_assert_workspace_access`` in the
    handler, and served with ``nosniff`` + a CSP ``sandbox`` on script-capable
    types so a workspace file can never run as the dashboard) and
    ``GET /api/memory`` are not matched here. Chat (``/api/chat`` /
    ``/api/chat/stream``) is deliberately untouched: a member's image
    attachments are passed inline to the model for inference and never written
    to disk — the chat handlers persist attachments to the workspace
    ``uploads/`` dir only for admins (``user.is_admin`` gate; see
    ``runtime/chat_attachments.py`` and ``_build_multimodal_content``).
    """
    path = request.url.path
    method = request.method
    # Any mutation of the workspace file tree: write/create/move/duplicate/delete
    # all live under /api/files (e.g. PUT/DELETE /api/files/{path},
    # POST /api/files/create|move|duplicate). GET stays readable.
    if (path == "/api/files" or path.startswith("/api/files/")) and method in (
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
    ):
        return True
    # Memory markdown edit/write — POST (append fact/daily) and PUT (full-doc
    # overwrite). GET /api/memory stays readable.
    return path == "/api/memory" and method in ("POST", "PUT", "PATCH", "DELETE")


def _member_forbidden(request: Request) -> bool:
    """True if a non-admin member must be blocked from this request."""
    path = request.url.path
    if path.startswith(_ADMIN_ONLY_PREFIXES):
        return True
    # Read-only environment: no workspace writes, downloads, or memory edits.
    if _readonly_member_forbidden(request):
        return True
    # Agent configuration is admin-only; a member's own threads, the
    # access-grant endpoints, and the agent-scoped vault overrides
    # (/api/agents/{name}/vault/… — owner-or-admin, checked in-handler via
    # assert_agent_admin) are exempt. "/vault/" with both slashes so the
    # admin-only POST /api/agents/{name}/reload-vault stays blocked.
    is_agent_mutation = request.method in ("POST", "PUT", "PATCH", "DELETE") and (
        path == "/api/agents" or path.startswith("/api/agents/")
    )
    # Self-serve agent creation: the single narrowest exemption to the rule
    # above, opt-in and default off. It covers EXACTLY `POST /api/agents`
    # (creating a new agent), never a sub-path — editing, deleting, reloading
    # or re-keying an existing agent stays admin-only, and so does every other
    # method on the collection. The config read is reached only once the path
    # and method already matched, so the hot path is unchanged; it goes through
    # the `read_home_config_doc` parse cache either way.
    #
    # This only opens the door. The real limits (quota, archetype allow-list,
    # and the switch itself, re-checked) live in the `POST /api/agents` handler
    # — the middleware is never the only thing standing between a member and a
    # new agent.
    if is_agent_mutation and request.method == "POST" and path == "/api/agents":
        from olano.config.system_settings import self_serve_agents_enabled

        return not self_serve_agents_enabled()
    return (
        is_agent_mutation
        and "/threads" not in path
        and "/access" not in path
        and "/vault/" not in path
    )


# ── dependencies ─────────────────────────────────────────────────────────────


def current_user() -> User | None:
    """Resolve the active principal from the request context (no raise)."""
    if not multi_user_enabled():
        return _OWNER
    ctx = get_request_context()
    if not ctx.authenticated or ctx.user_id in ("", "anonymous"):
        return None
    return get_user_store().db.get_user_by_id(ctx.user_id)


async def require_user() -> User:
    user = current_user()
    if user is None:
        raise HTTPException(status_code=401, detail="auth_required")
    return user


async def require_admin() -> User:
    user = await require_user()
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin_required")
    return user


def assert_agent_access(agent_name: str) -> User:
    """Raise 401/403 unless the current human may chat with ``agent_name``."""
    user = current_user()
    if user is None:
        raise HTTPException(status_code=401, detail="auth_required")
    if not get_user_store().can_chat(user, agent_name):
        raise HTTPException(status_code=403, detail="agent_access_denied")
    return user


def assert_admin() -> User:
    """Sync twin of ``require_admin`` for handlers that gate mid-body."""
    user = current_user()
    if user is None:
        raise HTTPException(status_code=401, detail="auth_required")
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin_required")
    return user


def assert_agent_admin(agent_name: str) -> User:
    """Raise unless the current human may *manage* the agent (admin or owner).

    Used by surfaces stronger than chat access: raw config reads, connector
    token mutations (connect / disconnect / share), etc.
    """
    user = current_user()
    if user is None:
        raise HTTPException(status_code=401, detail="auth_required")
    if not can_admin_agent(user, agent_name):
        raise HTTPException(status_code=403, detail="agent_admin_required")
    return user


def can_use_terminal() -> bool:
    """Whether the active human may use the terminal / chat "!" shell.

    Single-user installs: always (the implicit owner is admin). Multi-user:
    admins always, plus members an admin has granted ``can_terminal``.
    """
    if not multi_user_enabled():
        return True
    user = current_user()
    return bool(user and (user.is_admin or user.can_terminal))


def resolve_ui_mode(user: User | None = None) -> str:
    """Which dashboard shell this human gets: ``"advanced"`` or ``"simple"``.

    **Its own knob.** The mode is read from the user record and from nowhere
    else — it is deliberately NOT derived from ``is_admin``, so an admin can
    be given the simple shell and a member the full dashboard. Unset reads as
    ``advanced``, which is what every account had before this column existed,
    so upgrading a live box changes nobody's screen.

    Single-user installs (no users table) fall back to the home-config key
    ``ui_mode``, so a solo operator can still opt into the simple shell
    without turning multi-user mode on.

    This is presentation only and is never an authorization input. Every real
    boundary — ``is_admin``, ``_member_forbidden``, ``assert_agent_access`` —
    is unchanged and enforced independently, so a user who reaches the other
    shell by any means gains exactly nothing.
    """
    if not multi_user_enabled():
        try:
            from olano.config.home import ensure_olano_home, read_home_config_doc

            paths = ensure_olano_home()
            raw = read_home_config_doc(paths.root) if paths.config_path.exists() else {}
            return normalize_ui_mode(raw.get("ui_mode"))
        except Exception:  # pragma: no cover - unreadable config → default shell
            return DEFAULT_UI_MODE
    target = user if user is not None else current_user()
    if target is None:
        return DEFAULT_UI_MODE
    return normalize_ui_mode(target.ui_mode)


def resolve_langfuse_links(user: User | None = None) -> bool:
    """Whether this human's dashboard shows "Langfuse" links.

    Multi-user: the user's own ``langfuse_links`` flag, off unless an admin
    turned it on in Admin -> Users. Single-user: on, because there is no users
    table to set it in and the implicit owner is the admin who switched
    tracing on.

    Presentation only, like :func:`resolve_ui_mode`: the links route stays
    admin-only (``/api/observability`` is an admin prefix) whatever this says,
    so the dashboard also requires an admin before it asks.
    """
    if not multi_user_enabled():
        return True
    target = user if user is not None else current_user()
    return bool(target and target.langfuse_links)


def can_admin_agent(user: User, agent_name: str) -> bool:
    """True if the user may manage access for an agent (admin or its owner)."""
    if user.is_admin:
        return True
    return get_user_store().db.get_role(user.id, agent_name) == "owner"


def _roster_agent_names(runtime: Any, store: Any, user: User) -> set[str]:
    """Agent names the access overview should list, matching GET /api/agents.

    ``store.list_agent_names`` only sees agents on disk, so an agent that is
    booted but not yet written out would be missing from the access table while
    showing up everywhere else. Union the booted (non-hidden) runners in, then
    intersect with what this user may reach.
    """
    # Imported lazily: api.py imports this module, so a top-level import would
    # be circular. _agent_is_hidden identity-tests against True, which is what
    # keeps a MagicMock config's truthy auto-attribute from hiding an agent.
    from olano.runtime.api import _agent_is_hidden

    names = set(store.list_agent_names(user))
    for name, runner in getattr(runtime, "_runners", {}).items():
        if not _agent_is_hidden(getattr(runner, "config", None)):
            names.add(name)
    if user.is_admin:
        return names
    return {n for n in names if store.db.has_access(user.id, n)}


# ── thread visibility ────────────────────────────────────────────────────────
#
# One rule: a human who may chat with an agent sees every conversation that
# agent has had — scheduled runs, heartbeats, messaging-platform chats, other
# humans' dashboard chats — and continues any of them from the list. The gate is
# per-agent access (``assert_agent_access``, already at every call site), never
# a per-human namespace on the thread id.
#
# It used to be per-human: ``namespace_thread_id`` prefixed every dashboard
# thread id with ``u:<user_id>:`` and ``thread_id_visible`` hid everything else
# from non-admin members. That had two effects nobody wanted. A member never saw
# the runs and platform chats of an agent they were granted, and — worse — any
# human (admins included) who opened a bare-id thread from the list and replied
# was silently forked onto ``u:<uid>:<id>``: an empty checkpoint, a second
# transcript, and an agent that "could not remember" the run on screen. The same
# fork was fixed for groups first (``groups_routes.group_thread_id``).


def _thread_prefix(user_id: str) -> str:
    """The ``u:<user_id>:`` naming prefix.

    Kept as a *naming scheme only*: ``groups_routes.group_thread_id`` still
    derives a group's thread id from its creator with it, so existing group
    threads keep their ids across this change. It no longer means anything for
    visibility or continuation, and chat threads no longer receive it.
    """
    return f"u:{user_id}:"


def namespace_thread_id(thread_id: str | None) -> str:
    """Resolve the thread a dashboard turn runs on.

    A client-supplied id is continued **as-is** for every caller — the endpoint
    has already asserted the human may chat with the agent, and that is the
    whole rule. A missing id mints a fresh UUID. No ``u:<uid>:`` prefix is ever
    added: a fresh UUID is already unique to the conversation that minted it,
    and a prefix on a reopened id is exactly the fork this function used to
    cause. Ids written under the old scheme keep working unchanged; they are
    plain ids now.

    Kept as the single choke point for ``/api/chat`` and ``/api/chat/stream`` so
    any future policy on thread ids has one place to live.
    """
    import uuid as _uuid

    own = _scoped_thread_prefix()
    if own is not None:
        # A demo visitor (``demo_access.scopes_threads``): its conversations
        # live under its own prefix, and an id outside it is someone else's.
        if not thread_id:
            return f"{own}{_uuid.uuid4()}"
        if not thread_id_visible(thread_id, own):
            raise HTTPException(status_code=404, detail="Thread not found")
        return thread_id
    return thread_id or str(_uuid.uuid4())


def _scoped_thread_prefix() -> str | None:
    """The active human's own ``u:<id>:`` prefix when only its own threads are
    visible to it (a demo visitor), else ``None``."""
    if not multi_user_enabled():
        return None
    user = current_user()
    if user is None:
        return None
    from olano.runtime.demo_access import scopes_threads

    return _thread_prefix(user.id) if scopes_threads(user) else None


def thread_visible_to_current_user(thread_id: str) -> bool:
    """Whether the active human may see a thread of an agent they can reach.

    ``True`` for every authenticated human and on single-user installs;
    ``False`` only for an anonymous caller. The agent gate is separate and comes
    first at every call site (``assert_agent_access``). ``thread_id`` is
    accepted for the call sites' sake — the answer no longer depends on it.
    """
    return thread_id_visible(thread_id, thread_visibility_prefix())


def thread_visibility_prefix() -> str | None:
    """Thread visibility for the active human, captured for use outside a request.

    ``None`` — single-user installs and every authenticated human: all threads
    of a reachable agent are visible. ``""`` — anonymous/unauthenticated: none.
    This is what ``/api/stream`` snapshots at connect time, the same way it
    snapshots :func:`allowed_agent_names` — the frame generator runs after the
    request's auth context is gone. (The name is historical: the value was once
    a member's ``u:<user_id>:`` namespace; the ``None``/``""`` contract is what
    the callers depend on.)
    """
    if not multi_user_enabled():
        return None
    if current_user() is None:
        return ""
    return _scoped_thread_prefix()


def thread_id_visible(thread_id: str, prefix: str | None) -> bool:
    """Pure twin of :func:`thread_visible_to_current_user` for a snapshot.

    ``None``: visible. ``""``: hidden. Anything else is a scoped human's own
    ``u:<id>:`` prefix, matched on the bare id or after an agent scope
    (``<agent>::u:<id>:...``, the form the analytics rows carry).
    """
    if prefix is None:
        return True
    if not prefix:
        return False
    return thread_id.startswith(prefix) or f"::{prefix}" in thread_id


def grant_owner_on_create(agent_name: str) -> None:
    """Make the creating human the owner of a newly created agent (multi-user)."""
    if not multi_user_enabled():
        return
    user = current_user()
    if user is None:
        return
    get_user_store().db.set_owner(user.id, agent_name, granted_by=user.id)


def filter_tasks_for_current_user(tasks: list[dict]) -> list[dict]:
    """Filter scheduler tasks to those whose agent the current human may access.

    Closes the leak where every caller saw every task: admins (and single-user
    installs) see all; members see only tasks for agents they own/were granted.
    """
    if not multi_user_enabled():
        return tasks
    user = current_user()
    if user is None:
        return []
    if user.is_admin:
        return tasks
    allowed = set(get_user_store().list_agent_names(user))
    return [t for t in tasks if (t.get("agent_name") or "") in allowed]


def filter_visible_agents(items: list[dict]) -> list[dict]:
    """Filter an agent list (dicts with a 'name') to those the human may use.

    Admins (and single-user installs) see everything; members see only agents
    they own or have been granted chat access to.
    """
    if not multi_user_enabled():
        return items
    user = current_user()
    if user is None:
        return []
    if user.is_admin:
        return items
    store = get_user_store()
    return [it for it in items if store.can_chat(user, it.get("name", ""))]


def allowed_agent_names() -> set[str] | None:
    """The agent names the active human may see, or ``None`` for "no limit".

    ``None`` — single-user installs and admins (callers skip filtering).
    ``set()`` — anonymous/unauthenticated callers (everything filters out).
    Otherwise the member's granted set. This is the shared basis for scoping
    aggregate payloads (analytics, A2A, pulse, SSE) by per-agent attribution.
    """
    if not multi_user_enabled():
        return None
    user = current_user()
    if user is None:
        return set()
    if user.is_admin:
        return None
    return set(get_user_store().list_agent_names(user))


def filter_by_agent_field(items: list[dict], key: str = "agent") -> list[dict]:
    """Scope a list of dicts carrying a per-agent attribution field.

    The generic twin of ``filter_tasks_for_current_user`` for payloads whose
    rows name their agent under ``key`` (analytics rollups use ``agent``,
    scheduler rows ``agent_name``, …).
    """
    allowed = allowed_agent_names()
    if allowed is None:
        return items
    return [it for it in items if (it.get(key) or "") in allowed]


def a2a_event_visible(data: dict, allowed: set[str] | None) -> bool:
    """A2A visibility rule: a member may see an agent↔agent exchange when at
    least ONE participant is an agent they can access; exchanges strictly
    between non-granted agents stay hidden. (Subagent delegations carry the
    owning agent in ``from`` and the subagent type in ``to``, so the same
    check covers them.)"""
    if allowed is None:
        return True
    src = str(data.get("from") or "")
    dst = str(data.get("to") or "")
    return src in allowed or dst in allowed


def event_visible_to_agents(event: dict, allowed: set[str] | None) -> bool:
    """Per-subscriber SSE frame filter for /api/stream.

    ``allowed is None`` → no filtering (admin / single-user). For members:
    raw ``log`` lines are dropped (free text spanning every agent);
    ``agent_message`` frames follow the A2A ≥1-participant rule; any frame
    attributed to an agent (``data.agent`` / ``data.name`` / ``data.agent_name``)
    must name a granted agent; unattributed frames (roster pokes, scheduler
    refresh signals, …) pass — they carry no content and only trigger
    refetches of endpoints that filter server-side.
    """
    if allowed is None:
        return True
    etype = str(event.get("type") or "")
    if etype == "log":
        return False
    data = event.get("data")
    if not isinstance(data, dict):
        return True
    if etype == "agent_message":
        return a2a_event_visible(data, allowed)
    for key in ("agent", "name", "agent_name"):
        value = data.get(key)
        if value:
            return str(value) in allowed
    return True


def event_visible_to_thread(event: dict, prefix: str | None) -> bool:
    """Per-subscriber SSE filter for frames that carry a ``thread_id``.

    The agent filter above scopes by *agent*. Every frame naming a thread
    (``thread_updated`` / ``thread_activity`` / ``thread_message`` /
    ``magic_link_updated`` / ``chat_response`` …) is additionally held to the
    same rule the thread endpoints apply (:func:`thread_visible_to_current_user`)
    against the snapshot taken at connect time: ``None`` → visible, ``""`` (an
    anonymous subscriber) → dropped. Frames without a thread id are not this
    filter's business.
    """
    if prefix is None:
        return True
    data = event.get("data")
    if not isinstance(data, dict):
        return True
    thread_id = data.get("thread_id")
    if not thread_id:
        return True
    return thread_id_visible(str(thread_id), prefix)


# ── request models ───────────────────────────────────────────────────────────


class BootstrapRequest(BaseModel):
    email: str
    password: str
    name: str = ""
    # Optional permanent handle. Empty → minted from `name` (or the email
    # local-part). Slugified either way; never editable afterwards.
    username: str = ""
    # The one-time first-run setup code (runtime/setup_code.py). Required on
    # an unmanaged box with no accounts; ignored everywhere else.
    setup_code: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


class UserCreateRequest(BaseModel):
    email: str
    password: str | None = None  # omit → invite flow (user sets their own)
    name: str = ""
    username: str = ""  # empty → minted from `name` / the email local-part
    title: str = ""  # the position, shown under the name; optional
    is_admin: bool = False


class InviteAcceptRequest(BaseModel):
    password: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class PrefsPatchRequest(BaseModel):
    # Client-side UI preferences to merge into the user's stored blob (e.g.
    # {"nav_order": ["/chat", "/overview", ...]}). Shallow-merged server-side.
    prefs: dict[str, Any]


class SelfUiModeRequest(BaseModel):
    ui_mode: str


class UserPatchRequest(BaseModel):
    # No `username` here on purpose: it is the stable key behind every
    # `human-*` string in the issues audit log. `name` is the editable half.
    name: str | None = None
    # The position. Unlike `name` an explicit "" is a valid write: it clears it.
    title: str | None = None
    password: str | None = None
    is_admin: bool | None = None
    is_active: bool | None = None
    can_terminal: bool | None = None
    # "advanced" (full dashboard) or "simple" (bot chat shell). Admin-assigned
    # only; a user cannot change their own. Rejected with 422 if unrecognized
    # rather than silently clamped, so a typo in an admin script is visible.
    ui_mode: str | None = None
    # Show this person "Langfuse" links in the dashboard. Display only.
    langfuse_links: bool | None = None
    timezone: str | None = None


class AccessGrantRequest(BaseModel):
    user_id: str
    role: str = "member"


class AccessBulkRequest(BaseModel):
    """Grant (or revoke) one role across a set of users × agents in one call.

    ``role=None`` revokes. Onboarding a teammate onto eight agents is one
    request instead of eight; offboarding them is the same call with no role.
    """

    user_ids: list[str] = []
    agent_names: list[str] = []
    role: str | None = "member"


class ClaimRequest(BaseModel):
    """POST /api/auth/claim — the owner takes over the provisioned account."""

    name: str = ""
    password: str = ""


class OnboardingCompleteRequest(BaseModel):
    # Per-step outcome summary from the wizard ({"password": "done", ...}).
    # Unknown keys/values are dropped server-side (see users/onboarding.py).
    steps: dict[str, str] = {}
    # True when the admin dismissed the whole tour without doing any step.
    skipped: bool = False


# ── cookie helpers ───────────────────────────────────────────────────────────


def issue_session_cookie(response: Response, raw_token: str, *, max_age: int | None = None) -> None:
    """Public: attach the dashboard session cookie to ``response``.

    The single place that knows the cookie's name/prefix/attributes, so any
    surface that mints a session (the login route, and the handshake
    ``owner-login`` flow that signs a chat owner in from their phone) sets it
    identically. ``max_age`` overrides the default 14-day retention — the
    handshake flow passes a shorter window, since a phone is likelier lost.
    """
    _set_session_cookie(response, raw_token, max_age=max_age)


def _set_session_cookie(response: Response, raw_token: str, *, max_age: int | None = None) -> None:
    secure = secure_cookies_enabled()
    response.set_cookie(
        session_cookie_name(),
        raw_token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=_SESSION_MAX_AGE if max_age is None else max_age,
        secure=secure,
    )
    if secure:
        # Retire any legacy un-prefixed cookie (pre-upgrade session, or one
        # tossed by a sibling host) so it can't linger beside the __Host- one.
        response.delete_cookie(SESSION_COOKIE, path="/")


def _clear_session_cookies(response: Response) -> None:
    """Expire both cookie names — whichever the browser holds, it goes."""
    # Clearing a __Host- cookie is itself a Set-Cookie and must satisfy the
    # prefix rules (Secure, Path=/, no Domain) or browsers reject the delete.
    response.delete_cookie(SESSION_COOKIE_SECURE, path="/", secure=True, httponly=True)
    response.delete_cookie(SESSION_COOKIE, path="/")


# ── deployment credential-rotation note ──────────────────────────────────────


def _note_admin_password_rotation(email: str | None, *, is_admin: bool) -> None:
    """After a successful password change: if it invalidated the deployment's
    *temporary* admin password (cloud-provisioned instances), record the
    rotation and nudge the control plane so it purges its plaintext copy.
    Best-effort — must never break the auth flow.
    """
    try:
        from olano.deployment import credential_rotation

        if credential_rotation.is_deployment_admin_email(email, is_admin=is_admin):
            credential_rotation.record_admin_password_rotation()
            credential_rotation.schedule_report()
    except Exception:  # pragma: no cover - defensive
        logger.exception("credential-rotation note failed")


def _admin_password_rotated() -> bool:
    """Recorded rotation state for /api/auth/me. Never raises: a box that
    cannot read the state file is one the wizard should treat as still holding
    a temporary password (it will offer the change step, which is harmless)."""
    try:
        from olano.deployment import credential_rotation

        return credential_rotation.admin_password_rotated()
    except Exception:  # pragma: no cover - defensive
        logger.debug("rotation state unreadable", exc_info=True)
        return False


def _vault_password_rotated() -> bool:
    """Same contract as ``_admin_password_rotated`` for the vault passphrase."""
    try:
        from olano.deployment import credential_rotation

        return credential_rotation.vault_password_rotated()
    except Exception:  # pragma: no cover - defensive
        logger.debug("rotation state unreadable", exc_info=True)
        return False


# ── route registration ───────────────────────────────────────────────────────


def register_auth_routes(app: Any, runtime: Any) -> None:
    """Attach /api/auth/* and /api/admin/users* endpoints to the FastAPI app."""

    # Password hashing is a deliberately slow KDF (~0.5s a call here). It runs
    # in a worker thread on every route that reaches one, so an unauthenticated
    # spray can't burn the event loop the whole runtime shares.

    @app.get("/api/auth/username-available")
    async def username_available(username: str = "") -> dict:
        """Live availability probe for the handle fields.

        Returns what the server would actually store (`normalized` — the form
        shows "saved as `carlos-m`"), whether that exact handle is free, and
        the next free variant to offer when it is not. Uses the same slugify +
        mint pair as account creation, so the preview can never disagree with
        the eventual insert.
        """
        from olano.issues.principals import slugify
        from olano.users.db import mint_username

        def _check() -> dict:
            normalized = slugify(username)
            store = get_user_store()
            taken = {u.username for u in store.db.list_users() if u.username}
            return {
                "available": normalized not in taken,
                "normalized": normalized,
                "suggestion": mint_username(username, taken),
            }

        return await asyncio.to_thread(_check)

    @app.post("/api/auth/bootstrap")
    async def bootstrap(body: BootstrapRequest, response: Response) -> dict:
        store = get_user_store()
        if store.db.count_users() > 0:
            raise HTTPException(status_code=409, detail="already_initialized")
        from olano.runtime import setup_code

        code_required = await asyncio.to_thread(setup_code.required)
        if code_required:
            verdict = await asyncio.to_thread(setup_code.verify, body.setup_code)
            if not verdict.ok:
                if verdict.detail == "setup_code_rate_limited":
                    raise HTTPException(
                        status_code=429,
                        detail=verdict.detail,
                        headers={"Retry-After": str(verdict.retry_after)},
                    )
                raise HTTPException(status_code=403, detail=verdict.detail)
        try:
            admin = await asyncio.to_thread(
                store.bootstrap_admin, body.email, body.password, body.name, body.username
            )
        except DuplicateEmailError as exc:
            raise HTTPException(status_code=409, detail="email_exists") from exc
        except DuplicateUsernameError as exc:
            raise HTTPException(status_code=409, detail="username_exists") from exc
        if code_required:
            await asyncio.to_thread(setup_code.consume)
        set_multi_user_enabled(True)
        _set_session_cookie(response, store.issue_session(admin))
        logger.info("Multi-user mode enabled; admin %s created", admin.email)
        # Bootstrap only runs on an empty user table, so no account ever held
        # the provisioned temporary password — the owner just chose their own.
        # Stamp it unconditionally (not via _note_admin_password_rotation,
        # whose email match would skip an owner who signed up under a different
        # address than the one the box was provisioned with): the control plane
        # drops its plaintext copy, and the wizard stops asking the owner to
        # "change" the password they set a minute ago.
        try:
            from olano.deployment import credential_rotation

            credential_rotation.record_admin_password_rotation()
            credential_rotation.schedule_report()
        except Exception:  # pragma: no cover - defensive
            logger.exception("credential-rotation note failed (bootstrap)")
        return {"user": admin.public()}

    @app.get("/api/auth/setup-status")
    async def setup_status() -> dict:
        """Whether creating the first admin needs the one-time setup code.

        Public on purpose: the create-account screen asks it before anyone has
        an account. It says only *whether* a code is needed, never the code,
        and reads ``false`` once an account exists (or on a managed box).
        """
        from olano.runtime import setup_code

        return {"setup_code_required": await asyncio.to_thread(setup_code.required)}

    @app.get("/api/auth/sign-in-hint")
    async def sign_in_hint() -> dict:
        """What the sign-in screen should tell a visitor before they sign in.

        ``temporary_password`` is set only on a just-deployed managed box whose
        provisioned admin still has the temporary password the cloud generated
        (``first_boot.temporary_password_deployment_id`` holds the rule). The
        screen then says the password is in the welcome email and on the
        deployment's Security tab in the console, which is the one thing a
        first-time owner looking at an email + password form needs to know.
        Everywhere else it is ``null`` and the screen is the ordinary one.

        Public on purpose (``PUBLIC_API_PATHS``): nobody is signed in yet. It
        carries the deployment id and nothing else: never the email, the
        password or whether an account exists for any address. The id opens
        nothing by itself; every console route keyed on it requires a session
        in the organization that owns the deployment. And it goes quiet the
        moment the temporary password is replaced.
        """
        from olano.runtime.first_boot import temporary_password_deployment_id

        deployment_id = await asyncio.to_thread(temporary_password_deployment_id)
        return {
            "temporary_password": {"deployment_id": deployment_id} if deployment_id else None,
        }

    @app.post("/api/auth/login")
    async def login(body: LoginRequest, response: Response) -> dict:
        user = await asyncio.to_thread(get_user_store().authenticate, body.email, body.password)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid_credentials")
        _set_session_cookie(response, get_user_store().issue_session(user))
        return {"user": user.public()}

    @app.post("/api/auth/password")
    async def change_password(
        body: PasswordChangeRequest, user: User = Depends(require_user)
    ) -> dict:
        if not body.new_password:
            raise HTTPException(status_code=400, detail="password_required")
        changed = await asyncio.to_thread(
            get_user_store().change_password, user.id, body.current_password, body.new_password
        )
        if not changed:
            raise HTTPException(status_code=403, detail="wrong_password")
        _note_admin_password_rotation(user.email, is_admin=user.is_admin)
        return {"ok": True}

    @app.post("/api/auth/claim")
    async def claim_provisioned_account(
        body: ClaimRequest, request: Request, user: User = Depends(require_admin)
    ) -> dict:
        """The owner takes over the provisioned ``admin`` account as their own.

        A managed box is provisioned with one sign-in — handle ``admin``, the
        owner's own email, a temporary password the cloud mailed them. The
        onboarding profile step used to walk them off it by creating a SECOND
        account, which could not reuse the email (unique) and so demanded a
        second address. Claiming rewrites the three things the owner chooses
        — name, handle (minted from the name), password — on the existing row
        and leaves its id, email, admin flag and agent grants alone.

        Single-shot by construction: the gate is "still on the provisioned
        handle AND the temporary password is still in play", and a successful
        claim stamps the rotation, which closes the gate for every later call.
        The temporary password is deliberately not re-entered (the owner typed
        it to get here); what makes that safe is that every OTHER session of
        this user is revoked on the way out, so a cookie that was not the
        claimer's cannot ride the account past the claim.

        ``is_deployment_admin_email`` is NOT the gate: the email never changes,
        so it cannot tell a claimed account from an unclaimed one.
        """
        from olano.users.db import PROVISIONED_ADMIN_HANDLE
        from olano.users.passwords import hash_token

        if user.username != PROVISIONED_ADMIN_HANDLE or _admin_password_rotated():
            raise HTTPException(status_code=409, detail="already_claimed")
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="name_required")
        # The one server-side minimum on this path: the rekey route enforces
        # eight for the vault (api.py), and the password that replaces the
        # provisioned one must not be weaker than that.
        if len(body.password) < 8:
            raise HTTPException(status_code=400, detail="password_short")
        store = get_user_store()
        try:
            updated = await asyncio.to_thread(
                store.claim_provisioned_account, user.id, name=name, password=body.password
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="name_required") from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="user_not_found") from exc
        raw = session_token_from_cookies(request.cookies)
        store.db.delete_sessions_for_user(user.id, keep_token_hash=hash_token(raw) if raw else None)
        # Unconditional, like bootstrap: this IS the rotation of the
        # provisioned password, whatever address the row carries.
        try:
            from olano.deployment import credential_rotation

            credential_rotation.record_admin_password_rotation()
            credential_rotation.schedule_report()
        except Exception:  # pragma: no cover - defensive
            logger.exception("credential-rotation note failed (claim)")
        logger.info("Provisioned admin claimed by %s as @%s", updated.email, updated.username)
        return {"user": updated.public()}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response) -> dict:
        raw = session_token_from_cookies(request.cookies)
        if raw:
            get_user_store().revoke(raw)
        _clear_session_cookies(response)
        return {"ok": True}

    @app.get("/api/auth/invite/{token}")
    async def invite_info(token: str) -> dict:
        user = get_user_store().get_invite(token)
        if user is None:
            raise HTTPException(status_code=404, detail="invalid_or_expired_invite")
        # `username` was minted at invite time and is permanent; the accept
        # screen shows it so nobody first learns their handle from an issue.
        return {"email": user.email, "name": user.name, "username": user.username}

    @app.post("/api/auth/invite/{token}/accept")
    async def invite_accept(token: str, body: InviteAcceptRequest, response: Response) -> dict:
        if not body.password:
            raise HTTPException(status_code=400, detail="password_required")
        store = get_user_store()
        user = await asyncio.to_thread(store.accept_invite, token, body.password)
        if user is None:
            raise HTTPException(status_code=404, detail="invalid_or_expired_invite")
        # Password-reset links land here too — completing one for the
        # deployment admin also replaces the temporary password.
        _note_admin_password_rotation(user.email, is_admin=user.is_admin)
        _set_session_cookie(response, store.issue_session(user))
        return {"user": user.public()}

    @app.get("/api/auth/me")
    async def me(user: User = Depends(require_user)) -> dict:
        store = get_user_store()
        if user.is_admin:
            agents = [{"agent_name": n, "role": "admin"} for n in store.list_agent_names(user)]
        else:
            agents = store.db.list_agents_for_user(user.id)
        multi_user = multi_user_enabled()
        return {
            "user": user.public(),
            "agents": agents,
            "multi_user": multi_user,
            # The RESOLVED shell for this human, which is what the dashboard
            # forks on. It rides /api/auth/me deliberately: AuthProvider
            # already awaits this call before the app renders, so the right
            # shell mounts on the first paint with no flash and no second
            # round-trip. It must never move into /api/status capabilities —
            # that payload is public and cached process-wide for 5s, so a
            # per-user value there would leak across humans.
            #
            # user.public()["ui_mode"] is the raw stored column (what the
            # admin UI edits); this is the effective value after single-user
            # and unset-column fallbacks.
            "ui_mode": resolve_ui_mode(user),
            # Whether this human's dashboard shows "Langfuse" links, resolved
            # (see resolve_langfuse_links). Rides here for the same reason
            # ui_mode does: per user, so never in the public capabilities.
            "langfuse_links": resolve_langfuse_links(user),
            # Drives the dashboard's first-run setup wizard: shown full-screen
            # to every install's admin — single-user implicit owner included —
            # until any admin completes or skips it (deployment-wide flag,
            # survives restarts).
            "onboarding": {
                "required": onboarding.required_for(user.is_admin),
                # Whether a provisioned temporary password is still in play.
                # The wizard drops its password step when this is true: the
                # owner already chose their own, and asking them to "change"
                # it (current + new) reads as though something went wrong.
                "admin_password_rotated": _admin_password_rotated(),
                # Its vault twin: the profile step shows its passphrase half
                # only while the provisioned passphrase is still in play.
                "vault_password_rotated": _vault_password_rotated(),
            },
        }

    # ── First-run admin onboarding (dashboard setup wizard) ─────────────────
    # Deployment-wide state under <lionclaw_home>/onboarding.json. Admin-only:
    # also enforced centrally via _ADMIN_ONLY_PREFIXES ("/api/onboarding").

    @app.get("/api/onboarding")
    async def onboarding_state(user: User = Depends(require_admin)) -> dict:
        return {
            "required": onboarding.required_for(user.is_admin),
            "state": onboarding.load_state(),
        }

    @app.post("/api/onboarding/complete")
    async def onboarding_complete(
        body: OnboardingCompleteRequest, user: User = Depends(require_admin)
    ) -> dict:
        state = onboarding.mark_completed(user.id, steps=body.steps, skipped=body.skipped)
        logger.info("Onboarding %s by %s", "skipped" if body.skipped else "completed", user.email)
        # Where the dashboard lands next: Agent Father's chat, on a thread the
        # runtime greets the owner on in the background. ``None`` (no Agent
        # Father on this box, or the plan withholds it) sends them to Overview.
        greeting = None
        try:
            from olano.runtime.onboarding_greeting import schedule_greeting

            greeting = schedule_greeting(runtime, user)
        except Exception:  # a missing hello must never fail the completion
            logger.exception("welcome greeting not scheduled")
        return {"ok": True, "state": state, "greeting": greeting}

    @app.delete("/api/onboarding")
    async def onboarding_reset(_admin: User = Depends(require_admin)) -> dict:
        """Forget completion so the wizard runs again on next sign-in/refresh."""
        onboarding.reset()
        return {"ok": True}

    # ── Per-user preferences (nav order, layout toggles, …) ──────────────────
    # Persisted server-side so they follow the human across devices/browsers.
    # In single-user mode the implicit owner stores under user_id "owner".

    @app.get("/api/auth/me/prefs")
    async def get_my_prefs(user: User = Depends(require_user)) -> dict:
        return {"prefs": get_user_store().db.get_prefs(user.id)}

    @app.put("/api/auth/me/prefs")
    async def update_my_prefs(body: PrefsPatchRequest, user: User = Depends(require_user)) -> dict:
        return {"prefs": get_user_store().db.merge_prefs(user.id, body.prefs)}

    @app.put("/api/auth/me/ui-mode")
    async def set_my_ui_mode(body: SelfUiModeRequest, user: User = Depends(require_user)) -> dict:
        """Switch **your own** shell between the dashboard and the simple view.

        Admins only, and that asymmetry is the point. For a member the mode is
        an operator's decision about what their day looks like — a support rep
        put in the simple view should not be one menu click from the vault
        page, not because that page would leak (it refuses them anyway) but
        because "this is your workspace" is a weaker promise if the workspace
        can be swapped for a console by accident.

        An admin has the opposite problem: they have to be able to *see* what
        they configured for everyone else, and they need a way back. So the
        control renders in **both** shells for them — an admin who switches
        into the simple view finds the same switch waiting in its account
        menu. There is no state from which an admin cannot return.

        It writes the same column the admin surface writes, so a self-switch
        and an assignment are indistinguishable afterwards; nothing here is a
        second, parallel notion of "mode".
        """
        if not user.is_admin:
            # 403 with a specific reason rather than a bare "forbidden": the
            # client renders this as an explanation, and "ask your admin" is
            # actionable where "forbidden" is not.
            raise HTTPException(status_code=403, detail="ui_mode_admin_only")
        if body.ui_mode not in UI_MODES:
            raise HTTPException(status_code=422, detail="invalid_ui_mode")
        if not multi_user_enabled():
            # Single-user installs have no users table row to write — the
            # implicit owner's id matches nothing, so update_user was a silent
            # no-op: the endpoint answered 200, the shell swapped, and the
            # next /api/auth/me reverted it. The mode lives in config.yaml
            # there (the same key resolve_ui_mode reads), so write that.
            from olano.config.home import update_home_config

            update_home_config({"ui_mode": body.ui_mode})
            return {"ui_mode": body.ui_mode}
        get_user_store().db.update_user(user.id, ui_mode=body.ui_mode)
        return {"ui_mode": body.ui_mode}

    # ── Admin: user management ──────────────────────────────────────────────

    @app.get("/api/admin/users")
    async def admin_list_users(_admin: User = Depends(require_admin)) -> list[dict]:
        return [u.public() for u in get_user_store().db.list_users()]

    @app.post("/api/admin/users")
    async def admin_create_user(
        body: UserCreateRequest, admin: User = Depends(require_admin)
    ) -> dict:
        store = get_user_store()
        # Deployment entitlements: cap registered dashboard accounts.
        from olano.deployment import enforcement as dep_enforcement
        from olano.deployment import guard as dep_guard

        dep_guard.reject(dep_enforcement.can_create_user(len(store.db.list_users())))
        try:
            if body.password:
                # Direct create with an admin-set password.
                user = store.create_user(
                    email=body.email,
                    password=body.password,
                    name=body.name,
                    is_admin=body.is_admin,
                    username=body.username,
                    title=body.title,
                )
                return {"user": user.public(), "invite": None}
            # No password → invite: a pending account the user activates via a link.
            user, token, expires_at = store.invite_user(
                email=body.email,
                name=body.name,
                is_admin=body.is_admin,
                created_by=admin.id,
                username=body.username,
                title=body.title,
            )
        except DuplicateEmailError as exc:
            raise HTTPException(status_code=409, detail="email_exists") from exc
        except DuplicateUsernameError as exc:
            raise HTTPException(status_code=409, detail="username_exists") from exc
        return {
            "user": user.public(),
            "invite": {"token": token, "path": f"/invite/{token}", "expires_at": expires_at},
        }

    @app.post("/api/admin/users/{user_id}/invite")
    async def admin_reinvite(user_id: str, admin: User = Depends(require_admin)) -> dict:
        result = get_user_store().reinvite_user(user_id, created_by=admin.id)
        if result is None:
            raise HTTPException(status_code=409, detail="not_pending")
        token, expires_at = result
        return {"invite": {"token": token, "path": f"/invite/{token}", "expires_at": expires_at}}

    @app.post("/api/admin/users/{user_id}/reset")
    async def admin_reset_password(user_id: str, admin: User = Depends(require_admin)) -> dict:
        result = get_user_store().reset_password(user_id, created_by=admin.id)
        if result is None:
            raise HTTPException(status_code=404, detail="not_found")
        token, expires_at = result
        return {"reset": {"token": token, "path": f"/invite/{token}", "expires_at": expires_at}}

    @app.patch("/api/admin/users/{user_id}")
    async def admin_update_user(
        user_id: str, body: UserPatchRequest, _admin: User = Depends(require_admin)
    ) -> dict:
        store = get_user_store()
        target = store.db.get_user_by_id(user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="not_found")

        demoting = (body.is_admin is False) or (body.is_active is False)
        if demoting and target.is_admin and store.db.count_admins() <= 1:
            raise HTTPException(status_code=409, detail="last_admin")
        if body.ui_mode is not None and body.ui_mode not in UI_MODES:
            raise HTTPException(status_code=422, detail="invalid_ui_mode")
        # Names are never empty. `None` means "not changing it"; an explicit
        # blank is a request to erase, which there is no valid form of.
        if body.name is not None and not body.name.strip():
            raise HTTPException(status_code=422, detail="name_required")

        fields = {
            k: v
            for k, v in {
                "name": body.name,
                "title": body.title,
                "is_admin": body.is_admin,
                "is_active": body.is_active,
                "can_terminal": body.can_terminal,
                "ui_mode": body.ui_mode,
                "langfuse_links": body.langfuse_links,
                "timezone": body.timezone,
            }.items()
            if v is not None
        }
        if fields:
            try:
                store.db.update_user(user_id, **fields)
            except DuplicateEmailError as exc:
                raise HTTPException(status_code=409, detail="email_exists") from exc
        if body.password:
            store.set_password(user_id, body.password)
        updated = store.db.get_user_by_id(user_id)
        if body.password and updated is not None:
            _note_admin_password_rotation(updated.email, is_admin=updated.is_admin)
        return {"user": updated.public() if updated else {}}

    @app.delete("/api/admin/users/{user_id}")
    async def admin_delete_user(user_id: str, _admin: User = Depends(require_admin)) -> dict:
        store = get_user_store()
        target = store.db.get_user_by_id(user_id)
        if target is None:
            raise HTTPException(status_code=404, detail="not_found")
        if target.is_admin and store.db.count_admins() <= 1:
            raise HTTPException(status_code=409, detail="last_admin")
        store.db.delete_user(user_id)
        return {"ok": True}

    # ── Per-agent access (owner-or-admin) ───────────────────────────────────

    def _require_agent_admin(user: User, name: str) -> None:
        if not can_admin_agent(user, name):
            raise HTTPException(status_code=403, detail="agent_admin_required")

    @app.get("/api/agents/{name}/access")
    async def get_agent_access(name: str, user: User = Depends(require_user)) -> dict:
        _require_agent_admin(user, name)
        store = get_user_store()
        members = []
        for row in store.db.list_users_for_agent(name):
            u = store.db.get_user_by_id(row["user_id"])
            if u:
                members.append({**u.public(), "role": row["role"]})
        owner_id = store.db.owner_of(name)
        owner = store.db.get_user_by_id(owner_id) if owner_id else None
        return {"owner": owner.public() if owner else None, "members": members}

    @app.post("/api/agents/{name}/access")
    async def grant_agent_access(
        name: str, body: AccessGrantRequest, user: User = Depends(require_user)
    ) -> dict:
        _require_agent_admin(user, name)
        store = get_user_store()
        if store.db.get_user_by_id(body.user_id) is None:
            raise HTTPException(status_code=404, detail="user_not_found")
        role = body.role if body.role in ("owner", "member") else "member"
        store.db.grant_access(body.user_id, name, role=role, granted_by=user.id)
        return {"ok": True}

    @app.delete("/api/agents/{name}/access/{user_id}")
    async def revoke_agent_access(
        name: str, user_id: str, user: User = Depends(require_user)
    ) -> dict:
        _require_agent_admin(user, name)
        get_user_store().db.revoke_access(user_id, name)
        return {"ok": True}

    # ── Fleet-wide access overview (admin) ──────────────────────────────────

    @app.get("/api/admin/access-overview")
    async def admin_access_overview(_admin: User = Depends(require_admin)) -> dict:
        """Every agent's access in one response, plus the implicit admin owners.

        The per-agent ``GET /api/agents/{name}/access`` reads ``agent_access``
        rows only, so a platform admin — who owns every agent by rule
        (``can_admin_agent``) without ever having a row — is invisible there.
        This endpoint reports both kinds of owner so the UI can render the
        distinction instead of silently under-reporting who holds the keys.

        Users are returned once in a flat roster and referenced by id from each
        agent, so the payload stays O(agents + users) rather than O(agents ×
        users) on a large deployment.
        """
        store = get_user_store()
        users = store.db.list_users()
        known = {u.id for u in users}
        admin_ids = [u.id for u in users if u.is_admin]

        agents = []
        for name in sorted(_roster_agent_names(runtime, store, _admin)):
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
        }

    @app.post("/api/admin/access/bulk")
    async def admin_access_bulk(
        body: AccessBulkRequest, admin: User = Depends(require_admin)
    ) -> dict:
        """Apply one role across users × agents (``role=None`` revokes)."""
        store = get_user_store()
        role = body.role
        if role is not None and role not in ("owner", "member"):
            role = "member"
        visible = _roster_agent_names(runtime, store, admin)
        targets = [n for n in body.agent_names if n in visible]
        changed = 0
        for user_id in body.user_ids:
            if store.db.get_user_by_id(user_id) is None:
                continue
            for name in targets:
                if role is None:
                    changed += bool(store.db.revoke_access(user_id, name))
                else:
                    store.db.grant_access(user_id, name, role=role, granted_by=admin.id)
                    changed += 1
        return {"ok": True, "changed": changed}
