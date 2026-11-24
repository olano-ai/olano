"""Principals — the who of the issue tracker.

Issues connect *principals*, which come in two kinds: agents and humans.
The canonical stored form carries the kind as a hyphen prefix —
``ai-<agent name>`` / ``human-<user slug>`` — because a stored reference in
an audit log must stay unambiguous forever, and agents and humans are
allowed to share a name (an owner may name an agent after themselves).

The hyphen is a hard requirement, not taste: GitHub usernames permit only
alphanumerics and hyphens (no underscores, dots, or colons) and Gitea
permits hyphens too, so canonical principals are valid usernames on git,
GitHub, and Gitea verbatim — the Phase 2/3 mapping is the identity function
(section 6 of the issue-tracking design record).

Bare names are accepted as *input* everywhere and resolved here: unique
across both rosters → canonicalized silently; present in both → an explicit
ambiguity error naming both forms, so "assign it to carlos" just works
until the day it genuinely can't. ``system`` is reserved (runtime-initiated
events; never assignable).
"""

from __future__ import annotations

import re

AGENT_PREFIX = "ai-"
HUMAN_PREFIX = "human-"
SYSTEM = "system"


def slugify(name: str) -> str:
    """Free text → username-safe slug (``Carlos M.`` → ``carlos-m``).

    This is a *minting* function, not a lookup one: it is how
    ``users.db`` derives a handle for a new account and how the
    username backfill reproduced the handles this module used to compute
    on the fly. Nothing resolves a principal by calling it any more.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "user"


def agent_principal(agent_name: str) -> str:
    """An agent's principal, built from its handle — never its display name.

    ``AgentConfig.name`` is the immutable on-disk key, so a principal built
    from it survives any rename. ``display_name`` is editable and would
    re-key the audit log the moment someone edited it.
    """
    return f"{AGENT_PREFIX}{agent_name}" if agent_name else SYSTEM


def human_principal(username: str) -> str:
    """A human's principal, built from their stored, immutable handle.

    This used to take a *display name* and slugify it on every call, which
    made the principal a function of a mutable field: renaming "Carlos" to
    "Carlos M." silently moved them from ``human-carlos`` to
    ``human-carlos-m`` and orphaned every issue already filed against the
    first — in an append-only audit log. ``users.username`` is now minted
    once at account creation and never editable, so this is a pure prefix.
    """
    return f"{HUMAN_PREFIX}{username}"


def kind(principal: str) -> str:
    if principal.startswith(AGENT_PREFIX):
        return "agent"
    if principal.startswith(HUMAN_PREFIX):
        return "human"
    if principal == SYSTEM:
        return "system"
    return "unknown"


def bare_name(principal: str) -> str:
    for prefix in (AGENT_PREFIX, HUMAN_PREFIX):
        if principal.startswith(prefix):
            return principal[len(prefix) :]
    return principal


def _agent_roster() -> list[str]:
    """All agents on disk, hidden included — a hidden agent that files or is
    assigned an issue must still resolve as an ``ai-*`` principal."""
    try:
        from olano.config.loader import list_agents

        return list_agents(include_hidden=True)
    except Exception:
        return []


def _human_roster() -> list[str]:
    """Handles of active dashboard users. Empty when users.db is absent."""
    try:
        from olano.users.store import get_user_store

        return sorted(
            {u.username for u in get_user_store().db.list_users() if u.is_active and u.username}
        )
    except Exception:
        return []


def _human_display_names() -> dict[str, str]:
    """``username -> display name`` for active users, blank names dropped.

    Lets ``resolve_assignee`` accept the name a person is actually called
    ("assign it to Carlos M.") now that the handle is no longer derivable
    from it, and lets the API label a principal without a second query.
    """
    try:
        from olano.users.store import get_user_store

        return {
            u.username: (u.name or "").strip()
            for u in get_user_store().db.list_users()
            if u.is_active and u.username and (u.name or "").strip()
        }
    except Exception:
        return {}


def resolve_assignee(raw: str) -> tuple[str, str | None]:
    """``(canonical, error)`` — exactly one side is meaningful.

    Empty input is valid (unassigned). Canonical input is verified against
    its own roster; bare input searches both rosters case-insensitively.
    """
    raw = raw.strip()
    if not raw:
        return "", None
    if raw.casefold() == SYSTEM:
        return "", f"[tool-error] {SYSTEM!r} is reserved and cannot be assigned issues."

    agents = _agent_roster()
    humans = _human_roster()
    folded_agents = {a.casefold(): a for a in agents}
    folded_humans = {h.casefold(): h for h in humans}
    lowered = raw.casefold()

    # Canonical forms verify against their own roster only.
    if lowered.startswith(AGENT_PREFIX):
        name = folded_agents.get(lowered[len(AGENT_PREFIX) :])
        if name:
            return agent_principal(name), None
    if lowered.startswith(HUMAN_PREFIX):
        slug = folded_humans.get(lowered[len(HUMAN_PREFIX) :])
        if slug:
            return f"{HUMAN_PREFIX}{slug}", None

    # Bare name: search both rosters.
    hits: list[str] = []
    if lowered in folded_agents:
        hits.append(agent_principal(folded_agents[lowered]))

    # Humans match on three things, because the handle is no longer derivable
    # from the name: the handle itself ("carlos-m"), the display name someone
    # would actually say ("Carlos M."), and the slugified input — which used to
    # be the only route and still catches near-misses ("Carlos M" → carlos-m).
    display_names = _human_display_names()
    human_hits: set[str] = set()
    if lowered in folded_humans:
        human_hits.add(folded_humans[lowered])
    for username, display in display_names.items():
        if display.casefold() == lowered:
            human_hits.add(username)
    slugged = slugify(raw)
    if slugged in folded_humans:
        human_hits.add(folded_humans[slugged])
    if len(human_hits) > 1:
        options = " or ".join(repr(f"{HUMAN_PREFIX}{h}") for h in sorted(human_hits))
        return "", (
            f"[tool-error] {raw!r} is ambiguous — more than one person matches. "
            f"Use the explicit form: {options}."
        )
    hits.extend(f"{HUMAN_PREFIX}{h}" for h in human_hits)
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        options = " or ".join(f"{h!r} ({kind(h)})" for h in hits)
        return "", (
            f"[tool-error] {raw!r} is ambiguous — both an agent and a human have "
            f"this name. Use the explicit form: {options}."
        )

    roster = ", ".join(
        [agent_principal(a) for a in agents[:10]] + [f"{HUMAN_PREFIX}{h}" for h in humans[:10]]
    )
    return "", (
        f"[tool-error] Unknown assignee {raw!r}. Known principals: {roster or 'none configured'}."
    )
