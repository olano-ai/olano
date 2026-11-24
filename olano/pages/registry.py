"""Host-wide registry for agent-published pages + the owner-set viewer credential.

Agents publish a static page (a directory inside their workspace) at
``https://<host>/pages/<slug>/``, served by the runtime API
(``olano/runtime/pages_routes.py``). This module owns the two pieces of
shared state behind that:

* ``<home>/pages/registry.json``: the slug registry (desired state only: what
  each page serves and, for a deployed page, the build/start commands it runs;
  what is actually running lives with the supervisor, ``pages/services.py``).
  Every slug is claimed by
  exactly one agent, so multiple agents on the same host can never publish over
  each other. Mutations run under ``deployment_write_lock`` (the interprocess
  lock shared by the CLI and runtime API), so two agents claiming the same slug
  concurrently cannot both win.
* ``<home>/pages/auth.json`` — the HTTP Basic-Auth viewer credentials, **one
  per agent**: for each agent, a username plus a PBKDF2 hash
  (``olano/users/passwords.py`` format). Each agent's credential guards every
  page that agent publishes; an agent with no credential has all its pages
  answer 503. Credentials are written ONLY by the human owner/admin
  (``olano pages set-password --agent <name>``, the dashboard Agent → Pages
  tab, or the admin-gated ``POST /api/pages/{agent}/auth``) — agent tools can
  neither set nor read them, so the plaintext never appears in a transcript,
  shell command, or process list. Pages are never served unauthenticated.

  Schema v2: ``{"version": 2, "agents": {"<agent>": {"username", "password_hash",
  "updated_at"}}}``. A legacy v1 file (a single flat ``{username, password_hash}``
  that guarded the whole deployment) is migrated on first read to v2 by assigning
  that credential to every agent that currently owns a page.
"""

from __future__ import annotations

import hmac
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, cast

from olano.config.home import get_olano_paths
from olano.deployment.locks import deployment_write_lock
from olano.memory.markdown_memory import atomic_write_text
from olano.pages.spec import KIND_SERVICE, KIND_STATIC, DeploySpec
from olano.users.passwords import hash_password, verify_password

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_SLUG_MIN = 2
_SLUG_MAX = 63
_MIN_PASSWORD_LEN = 8

# Kept out of the slug namespace so a page can never shadow (or be confused
# with) a runtime surface, even though pages are served under /pages/.
RESERVED_SLUGS = frozenset(
    {"api", "admin", "assets", "auth", "dashboard", "openapi", "pages", "static", "www"}
)


class PagesError(ValueError):
    """Actionable failure in a page-registry operation (bad slug, collision, …)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── slug / directory validation ──────────────────────────────────────────────


def normalize_slug(slug: str) -> str:
    """Lowercase/trim ``slug`` and validate it; raise ``PagesError`` if invalid."""
    normalized = (slug or "").strip().lower()
    if len(normalized) < _SLUG_MIN or len(normalized) > _SLUG_MAX:
        raise PagesError(f"slug must be {_SLUG_MIN}-{_SLUG_MAX} characters, got '{normalized}'")
    if not _SLUG_RE.match(normalized):
        raise PagesError(
            f"invalid slug '{normalized}': use lowercase letters, digits and hyphens "
            "(must start and end with a letter or digit)"
        )
    if normalized in RESERVED_SLUGS:
        raise PagesError(f"slug '{normalized}' is reserved — choose a different one")
    return normalized


def _validate_directory(workspace_root: Path, directory: str) -> str:
    """Validate a workspace-relative page directory; return its normalized form."""
    rel = (directory or "").strip().strip("/") or "."
    pure = PurePosixPath(rel)
    if pure.is_absolute() or ".." in pure.parts:
        raise PagesError("directory must be a workspace-relative path without '..'")
    target = (workspace_root / rel).resolve()
    try:
        target.relative_to(workspace_root.resolve())
    except ValueError:
        raise PagesError("directory resolves outside the agent workspace") from None
    if not target.is_dir():
        raise PagesError(
            f"directory '{rel}' does not exist in the workspace — create it "
            "(with an index.html) before publishing"
        )
    return str(pure)


# ── registry file IO ─────────────────────────────────────────────────────────


def _load() -> dict[str, Any]:
    path = get_olano_paths().pages_registry_path
    if not path.exists():
        return {"version": 1, "pages": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "pages": {}}
    if not isinstance(data, dict) or not isinstance(data.get("pages"), dict):
        return {"version": 1, "pages": {}}
    return data


def _save(data: dict[str, Any]) -> None:
    path = get_olano_paths().pages_registry_path
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _with_slug(slug: str, record: dict[str, Any]) -> dict[str, Any]:
    return {"slug": slug, **record}


def _require_owned(data: dict[str, Any], slug: str, agent: str) -> dict[str, Any]:
    record = data["pages"].get(slug)
    if record is None:
        raise PagesError(f"no page registered under slug '{slug}'")
    if record.get("agent") != agent:
        raise PagesError(
            f"slug '{slug}' belongs to agent '{record.get('agent')}' — "
            "you can only manage your own pages"
        )
    return cast("dict[str, Any]", record)


# ── page lifecycle (agent-facing) ────────────────────────────────────────────


def _claim(
    agent: str,
    slug: str,
    directory: str,
    workspace_root: Path,
    *,
    deploy: DeploySpec | None,
    spa: bool,
) -> dict[str, Any]:
    slug = normalize_slug(slug)
    rel = _validate_directory(workspace_root, directory)
    now = _now_iso()
    with deployment_write_lock():
        data = _load()
        record = data["pages"].get(slug)
        if record is not None and record.get("agent") != agent:
            raise PagesError(
                f"slug '{slug}' is already taken by agent '{record.get('agent')}' — "
                "choose a different slug (page_status reports availability)"
            )
        if record is None:
            record = {"agent": agent, "created_at": now}
        record.update(
            {
                "directory": rel,
                "enabled": True,
                "updated_at": now,
                "spa": bool(spa),
                "kind": deploy.kind if deploy is not None else KIND_STATIC,
                # Every start/deploy/restart moves the generation, which is
                # what tells the supervisor (in whatever process it runs) to
                # rebuild and restart even when the spec did not change.
                "generation": int(record.get("generation") or 0) + 1,
            }
        )
        if deploy is not None:
            record["deploy"] = deploy.to_dict()
        else:
            record.pop("deploy", None)
        data["pages"][slug] = record
        _save(data)
    return _with_slug(slug, record)


def start_page(
    agent: str, slug: str, directory: str, workspace_root: Path, *, spa: bool = False
) -> dict[str, Any]:
    """Claim ``slug`` for ``agent`` (or update an existing claim) and enable serving.

    Publishes the directory as it is. Re-publishing a deployed page this way
    turns it back into a plain static page: the supervisor stops its service.

    Collision-safe across agents and processes: a slug owned by another agent
    raises ``PagesError`` instead of being overwritten.
    """
    return _claim(agent, slug, directory, workspace_root, deploy=None, spa=spa)


def deploy_page(
    agent: str,
    slug: str,
    directory: str,
    workspace_root: Path,
    spec: DeploySpec,
    *,
    spa: bool = False,
) -> dict[str, Any]:
    """Claim ``slug`` for a page that runs commands (a build, a server, or both).

    Only records the desired state; the supervisor (``pages/services.py``)
    does the building and running. ``spa`` applies to static builds only.
    """
    if spec.kind == KIND_SERVICE and spa:
        raise PagesError("spa applies to static pages; a service answers its own routes")
    return _claim(agent, slug, directory, workspace_root, deploy=spec, spa=spa)


def stop_page(agent: str, slug: str) -> dict[str, Any]:
    """Disable serving for ``slug`` (the claim is kept so nobody else takes it)."""
    slug = normalize_slug(slug)
    with deployment_write_lock():
        data = _load()
        record = _require_owned(data, slug, agent)
        record.update({"enabled": False, "updated_at": _now_iso()})
        _save(data)
    return _with_slug(slug, record)


def restart_page(agent: str, slug: str, workspace_root: Path) -> dict[str, Any]:
    """Re-validate the page directory and (re-)enable serving for ``slug``."""
    slug = normalize_slug(slug)
    with deployment_write_lock():
        data = _load()
        record = _require_owned(data, slug, agent)
        _validate_directory(workspace_root, record.get("directory", "."))
        record.update(
            {
                "enabled": True,
                "updated_at": _now_iso(),
                "generation": int(record.get("generation") or 0) + 1,
            }
        )
        _save(data)
    return _with_slug(slug, record)


def delete_page(agent: str, slug: str) -> None:
    """Release ``slug`` entirely so another agent may claim it."""
    slug = normalize_slug(slug)
    with deployment_write_lock():
        data = _load()
        _require_owned(data, slug, agent)
        del data["pages"][slug]
        _save(data)


def admin_delete_page(slug: str) -> None:
    """Owner/admin cleanup: release any agent's slug (dashboard/CLI surface)."""
    slug = normalize_slug(slug)
    with deployment_write_lock():
        data = _load()
        if slug not in data["pages"]:
            raise PagesError(f"no page registered under slug '{slug}'")
        del data["pages"][slug]
        _save(data)


def admin_set_enabled(
    slug: str, enabled: bool, workspace_root: Path | None = None
) -> dict[str, Any]:
    """Owner/admin: stop or restart any agent's page (dashboard surface).

    Enabling re-validates the directory when the workspace root is known and
    moves the generation, so a deployed page rebuilds and restarts exactly as
    an agent's ``pages_restart`` would.
    """
    slug = normalize_slug(slug)
    with deployment_write_lock():
        data = _load()
        record = data["pages"].get(slug)
        if record is None:
            raise PagesError(f"no page registered under slug '{slug}'")
        changes: dict[str, Any] = {"enabled": bool(enabled), "updated_at": _now_iso()}
        if enabled:
            if workspace_root is not None:
                _validate_directory(workspace_root, record.get("directory", "."))
            changes["generation"] = int(record.get("generation") or 0) + 1
        record.update(changes)
        _save(data)
    return _with_slug(slug, record)


def get_page(slug: str) -> dict[str, Any] | None:
    record = _load()["pages"].get(slug)
    return _with_slug(slug, record) if record is not None else None


def list_pages(agent: str | None = None) -> list[dict[str, Any]]:
    pages = _load()["pages"]
    return [
        _with_slug(slug, record)
        for slug, record in sorted(pages.items())
        if agent is None or record.get("agent") == agent
    ]


# ── viewer credentials, per agent (owner-facing only) ────────────────────────


def _agents_with_pages() -> set[str]:
    """Every agent that currently owns a page (used to migrate a v1 credential)."""
    return {str(record["agent"]) for record in _load()["pages"].values() if record.get("agent")}


def _load_auth() -> dict[str, Any]:
    """The per-agent credential map (schema v2), migrating a legacy v1 file.

    v1 was a single flat ``{username, password_hash}`` guarding the whole
    deployment. On first read we assign it to every agent that currently owns a
    page and persist the v2 form, so existing pages keep working unchanged.
    """
    path = get_olano_paths().pages_auth_path
    if not path.exists():
        return {"version": 2, "agents": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 2, "agents": {}}
    if not isinstance(data, dict):
        return {"version": 2, "agents": {}}
    if isinstance(data.get("agents"), dict):
        return data
    # Legacy v1 → migrate to per-agent.
    username = data.get("username")
    password_hash = data.get("password_hash")
    if not username or not password_hash:
        return {"version": 2, "agents": {}}
    legacy = {
        "username": username,
        "password_hash": password_hash,
        "updated_at": data.get("updated_at") or _now_iso(),
    }
    migrated = {
        "version": 2,
        "agents": {agent: dict(legacy) for agent in _agents_with_pages()},
    }
    with deployment_write_lock():
        _save_auth(migrated)
    return migrated


def _save_auth(data: dict[str, Any]) -> None:
    path = get_olano_paths().pages_auth_path
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - permission quirks on exotic filesystems
        pass


def set_page_auth(agent: str, username: str, password: str) -> None:
    """Store ``agent``'s viewer credential. Owner surfaces only (CLI/dashboard).

    The plaintext is hashed immediately (PBKDF2) and never persisted.
    """
    agent = (agent or "").strip()
    if not agent:
        raise PagesError("agent must not be empty")
    username = (username or "").strip()
    if not username:
        raise PagesError("username must not be empty")
    if ":" in username:
        raise PagesError("username must not contain ':' (the HTTP Basic-Auth separator)")
    if len(password or "") < _MIN_PASSWORD_LEN:
        raise PagesError(f"password must be at least {_MIN_PASSWORD_LEN} characters")
    with deployment_write_lock():
        data = _load_auth()
        data.setdefault("agents", {})[agent] = {
            "username": username,
            "password_hash": hash_password(password),
            "updated_at": _now_iso(),
        }
        _save_auth(data)


def clear_page_auth(agent: str) -> bool:
    """Remove ``agent``'s viewer credential (its pages revert to 503). Owner-only.

    Returns True if a credential was removed, False if there was none.
    """
    agent = (agent or "").strip()
    with deployment_write_lock():
        data = _load_auth()
        if agent not in data.get("agents", {}):
            return False
        del data["agents"][agent]
        _save_auth(data)
    return True


def get_page_auth(agent: str) -> dict[str, Any] | None:
    """``agent``'s stored credential record (username + hash), or None if unset."""
    record = _load_auth().get("agents", {}).get((agent or "").strip())
    if not record or not record.get("username") or not record.get("password_hash"):
        return None
    return cast("dict[str, Any]", record)


def page_auth_configured(agent: str) -> bool:
    return get_page_auth(agent) is not None


def list_page_auth() -> dict[str, dict[str, Any]]:
    """Per-agent credential state for admin display: ``{agent: {"username": ...}}``.

    Never exposes password hashes — only which agents have a credential and the
    username a viewer must type.
    """
    return {
        agent: {"username": record.get("username", ""), "updated_at": record.get("updated_at")}
        for agent, record in _load_auth().get("agents", {}).items()
        if record.get("username") and record.get("password_hash")
    }


def verify_page_auth(agent: str, username: str, password: str) -> bool:
    """Constant-time check of a Basic-Auth credential against ``agent``'s record."""
    record = get_page_auth(agent)
    if record is None:
        return False
    user_ok = hmac.compare_digest(
        (username or "").encode("utf-8"), str(record["username"]).encode("utf-8")
    )
    pass_ok = verify_password(password or "", str(record["password_hash"]))
    return user_ok and pass_ok
