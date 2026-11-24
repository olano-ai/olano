"""The agent handle: its shape, and how one is minted from a display name.

``AgentConfig.name`` is the permanent key — the on-disk folder, the checkpoint
thread scope, every ``/api/agents/{name}/…`` path, the ``ai-<name>`` issue
principal. Nobody should have to *choose* it: a person names an agent
("Hillary"), and the handle follows from that name the way a human's
``username`` follows from theirs (``users/db.py:mint_username``). This module
is that derivation, shared by every create path (``POST /api/agents``, the
archetype and integration installers, the AgentFather tool) so a handle minted
from one surface is the handle every other surface would have minted.

Two rules are load-bearing:

* The slug rule mirrors the dashboard's preview (``helpers.slugifyAgentName``)
  character for character, so what the form shows under the name field is
  what the server writes — the client is a preview, never the authority.
* A collision gets a numeric suffix (``hillary``, ``hillary-2``) rather than a
  refusal: the person never asked for a specific handle, so there is nothing
  to refuse, and a "name taken" error on a field they cannot edit would be a
  dead end.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# The server's rule for a handle: lowercase letters, digits, dash and
# underscore, starting with a letter or digit, at most 64 characters. The
# dashboard's AGENT_NAME_RE is the unbounded client-side mirror.
AGENT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
MAX_AGENT_NAME_LEN = 64

_INVALID_RUN = re.compile(r"[^a-z0-9_-]+")


def is_valid_agent_name(name: str | None) -> bool:
    """True when *name* is a handle the create endpoints accept verbatim."""
    return bool(name) and AGENT_NAME_RE.fullmatch(name or "") is not None


def slugify_agent_name(text: str | None) -> str:
    """Free text → candidate handle: ``"Merli the Analyst"`` → ``merli-the-analyst``.

    Empty when nothing survives (an all-CJK or all-punctuation name), so the
    caller can fall back to something meaningful — the archetype slug, say —
    instead of shipping an empty string into a path.
    """
    slug = _INVALID_RUN.sub("-", (text or "").strip().lower())
    # A handle must start with a letter or digit; it may end with `_` but a
    # trailing dash is just noise from a stripped suffix.
    slug = slug.lstrip("-_").rstrip("-")
    if len(slug) > MAX_AGENT_NAME_LEN:
        slug = slug[:MAX_AGENT_NAME_LEN].rstrip("-")
    return slug


def mint_agent_name(desired: str | None, taken: Iterable[str], fallback: str = "agent") -> str:
    """Slugify *desired* into a handle not already in *taken*.

    ``fallback`` is used when *desired* slugifies to nothing — pass the
    archetype or toolkit slug so an unslugifiable display name still yields
    a handle that says what the agent is. Collisions are suffixed ``-2``,
    ``-3``, … (compared case-insensitively, since agent lookups are).
    """
    base = slugify_agent_name(desired) or slugify_agent_name(fallback) or "agent"
    seen = {t.strip().casefold() for t in taken if t}
    if base not in seen:
        return base
    n = 2
    while True:
        suffix = f"-{n}"
        candidate = f"{base[: MAX_AGENT_NAME_LEN - len(suffix)].rstrip('-')}{suffix}"
        if candidate not in seen:
            return candidate
        n += 1


def taken_agent_names() -> set[str]:
    """Every handle already in use on this box, hidden agents included.

    Hidden agents are exactly the ones a minted handle must not collide with:
    their folder exists, so a create under the same name would either clobber
    it or 409 on a field the user never typed. This feeds no listing — it is
    a collision set — which is why it may ask for the complete on-disk set.
    """
    from olano.config.loader import list_agents

    return set(list_agents(include_hidden=True))
