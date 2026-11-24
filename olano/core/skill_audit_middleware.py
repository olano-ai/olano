"""Quarantine audit-flagged skills from the main agent's prompt.

deepagents' ``SkillsMiddleware`` discovers every skill under the configured
source dirs and freezes them into ``skills_metadata`` (per thread) before
injecting that list into the system prompt. Because it works per-directory and
one level deep, a source dir can't simply drop a single flagged skill — so the
audit (``skills.loader.compute_skipped_skills``) can't exclude it upstream
without also removing its safe siblings.

This middleware closes that gap: it prunes the named skills out of
``skills_metadata`` before the first model call, so they never reach the prompt.
It does so in ``before_model``, never ``before_agent``: deepagents loads the list
in ``SkillsMiddleware.before_agent``, and since 0.7.23 that middleware sits at the
*tail* of the stack, after every middleware olano passes in. A ``before_agent``
prune therefore ran before the load on a thread's first turn, found nothing, and
let every blocked skill into that turn's prompt. All ``before_agent`` hooks run
before any ``before_model`` hook, so the prune now sees the loaded list whatever
the stack order is. The skill stays on
disk and is still listed by ``/skills`` — it is just not advertised to the
model. Flagging a skill therefore never aborts startup and never silently
disappears.

The same directory-granularity problem is why this middleware is also handed
the skills an operator (or the agent itself) turned off in ``agent.json5 →
disabled_skills``: the managed pack is one shared folder, so "off for this
agent" cannot be expressed by dropping a source dir. The builder merges the two
sets here and nowhere else — every human-facing surface keeps them apart, since
"the audit flagged this" and "you switched this off" are different answers. A
third set rides along: skills whose ``requires_tools`` frontmatter names tools
this agent does not have (``skills.loader.compute_gated_skills``) — "not written
for this agent", which is a third answer again.

``SkillQuarantineMiddleware`` (below) applies the same idea to DEGRADED learned
shadows flagged by the preservation gate (``compute_quarantined_shadows``):
instead of pruning, it swaps the shadow's entry back to the original skill it
shadows, so a lossy auto-learned copy can never hide a good original from the
prompt.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)


class SkillAuditFilterMiddleware(AgentMiddleware):
    """Remove audit-flagged and operator-disabled skills from ``skills_metadata``."""

    def __init__(self, *, blocked_skills: Mapping[str, str]) -> None:
        super().__init__()
        # name -> reason; only the names matter here.
        self._blocked = set(blocked_skills)

    @property
    def name(self) -> str:
        return "SkillAuditFilterMiddleware"

    def _pruned(self, state: Any) -> dict[str, Any] | None:
        if not self._blocked:
            return None
        try:
            md = state.get("skills_metadata") if hasattr(state, "get") else None
        except Exception:
            return None
        if not md:
            return None
        kept = [s for s in md if str(s.get("name")) not in self._blocked]
        if len(kept) == len(md):
            return None
        logger.debug("Pruned %d blocked skill(s) from the prompt", len(md) - len(kept))
        return {"skills_metadata": kept}

    # Signature mirrors ``AgentMiddleware.before_model`` (state, runtime); langgraph
    # injects ``runtime`` by name. We only need ``state`` to prune skills_metadata.
    # Runs before every model call, but returns an update only when the list
    # still holds a blocked skill: the first call of a thread, or after a reload.
    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._pruned(state)

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._pruned(state)


def build_skill_audit_filter_middleware(
    *, blocked_skills: Mapping[str, str]
) -> list[AgentMiddleware]:
    """Return the filter middleware, or ``[]`` when nothing is blocked."""
    if not blocked_skills:
        return []
    return [SkillAuditFilterMiddleware(blocked_skills=blocked_skills)]


class SkillQuarantineMiddleware(AgentMiddleware):
    """Swap degraded learned shadows out of ``skills_metadata``.

    deepagents dedupes skills by name (last source wins) when it loads them,
    so by the time we see ``skills_metadata`` (in ``before_model``, for the
    ordering reason in the module docstring) a degraded
    learned shadow has already replaced its original — plain name-pruning would
    leave a hole, not resurface the original. This middleware therefore swaps
    the shadow's entry back to the original's ``path``/``description`` (keeping
    the rest of the deepagents-parsed entry intact), or drops the entry when the
    original no longer exists. Progressive disclosure then reads the original's
    SKILL.md again.
    """

    def __init__(self, *, quarantined: Mapping[str, dict]) -> None:
        super().__init__()
        # shadow SKILL.md path -> {"name", "original_location", "original_description"}
        self._by_path = {str(path): dict(info) for path, info in quarantined.items()}

    @property
    def name(self) -> str:
        return "SkillQuarantineMiddleware"

    def _fixed(self, state: Any) -> dict[str, Any] | None:
        if not self._by_path:
            return None
        try:
            md = state.get("skills_metadata") if hasattr(state, "get") else None
        except Exception:
            return None
        if not md:
            return None
        out: list[Any] = []
        changed = False
        for entry in md:
            info = self._by_path.get(str(entry.get("path", "")))
            if info is None:
                out.append(entry)
                continue
            changed = True
            original_path = info.get("original_location") or ""
            if original_path:
                out.append(
                    {
                        **entry,
                        "path": original_path,
                        "description": info.get("original_description")
                        or entry.get("description", ""),
                    }
                )
                logger.debug(
                    "Quarantined degraded learned shadow %r — serving original %s",
                    entry.get("name"),
                    original_path,
                )
            else:
                logger.debug(
                    "Quarantined degraded learned shadow %r (no original to restore)",
                    entry.get("name"),
                )
        if not changed:
            return None
        return {"skills_metadata": out}

    def before_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._fixed(state)

    async def abefore_model(self, state: Any, runtime: Any) -> dict[str, Any] | None:
        return self._fixed(state)


def build_skill_quarantine_middleware(*, quarantined: Mapping[str, dict]) -> list[AgentMiddleware]:
    """Return the quarantine middleware, or ``[]`` when nothing is degraded."""
    if not quarantined:
        return []
    return [SkillQuarantineMiddleware(quarantined=quarantined)]
