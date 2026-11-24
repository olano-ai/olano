"""Per-turn skill awareness — surface newly installed/learned skills mid-session.

deepagents' ``SkillsMiddleware`` loads the skill list ONCE per thread (in
``before_agent``) and freezes it into checkpointed state, so a skill installed or
learned AFTER a thread started stays invisible until a brand-new thread. This
middleware closes that gap: each model call consults a short-TTL scan of the
agent's skill source dirs and, for any skill that was not part of the thread's
starting set, appends a short "skills added since this session started" note to
the system message — ephemeral (never persisted to history). The agent can then
read the skill's ``SKILL.md`` and use it immediately in the existing session.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.agents.middleware import AgentMiddleware

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig
    from olano.config.workspace import Workspace

logger = logging.getLogger(__name__)

# Hard caps so a runaway skill set can't bloat the prompt or leak memory.
_MAX_NEW_SKILLS = 25
_BASELINE_CACHE_CAP = 2000

# A scan re-reads every SKILL.md across all source dirs (and, with
# security.skill_audit on, rglobs + regex-scans every skill file) — far too heavy
# to repeat on each model call of a multi-tool turn. Memoizing means a skill
# installed mid-session is announced only once the TTL lapses; that ≤30s delay is
# what keeps the announce feature working without a per-call rescan.
_SCAN_TTL_SECONDS = 30.0


class SkillAwarenessMiddleware(AgentMiddleware):
    """Inject skills that appeared after a thread started into each model call."""

    def __init__(self, *, config: AgentConfig, workspace: Workspace) -> None:
        super().__init__()
        self.config = config
        self.workspace = workspace
        # Per-thread snapshot of the skill names known when the thread started.
        self._baselines: dict[str, set[str]] = {}
        # (monotonic scan time, filtered skills) — see _SCAN_TTL_SECONDS.
        self._scan_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._scan_ttl = _SCAN_TTL_SECONDS

    @property
    def name(self) -> str:
        return "SkillAwarenessMiddleware"

    def _thread_id(self) -> str:
        try:
            from olano.security.request_context import get_request_context

            return get_request_context().session_id or "default"
        except Exception:
            return "default"

    def _scan_is_fresh(self) -> bool:
        cached = self._scan_cache
        return cached is not None and time.monotonic() - cached[0] < self._scan_ttl

    def _current_skills(self) -> list[dict[str, Any]]:
        cached = self._scan_cache
        now = time.monotonic()
        if cached is not None and now - cached[0] < self._scan_ttl:
            return cached[1]
        try:
            from olano.skills.loader import iter_agent_skills

            skills = iter_agent_skills(self.config, self.workspace)
        except Exception:
            # Failures are not cached — a transient scan error must not suppress
            # announcements for a whole TTL window.
            logger.debug("skill-awareness scan failed", exc_info=True)
            return []
        # Mirror deepagents' SkillsMiddleware, which loads ONLY skills with valid
        # frontmatter (non-empty name AND description). Counting the ones it skips
        # would announce them as "new" every turn forever (they're never in the
        # frozen baseline).
        filtered = [
            s
            for s in skills
            if str(s.get("name") or "").strip() and str(s.get("description") or "").strip()
        ]
        self._scan_cache = (now, filtered)
        return filtered

    def _baseline(self, thread_id: str, state: Any, current_names: set[str]) -> set[str]:
        """The set of skill names known at this thread's start.

        Prefers deepagents' frozen ``skills_metadata`` (its own thread-start
        snapshot) when that private state attr is visible; otherwise seeds from the
        current on-disk set so the very first turn announces nothing. Frozen per
        thread for the process lifetime, so anything added later is announced every
        turn for the rest of the session.
        """
        cached = self._baselines.get(thread_id)
        if cached is not None:
            return cached

        names: set[str] = set()
        try:
            md = state.get("skills_metadata") if hasattr(state, "get") else None
            if md:
                names = {str(s.get("name")) for s in md if s.get("name")}
        except Exception:
            names = set()
        if not names:
            names = set(current_names)

        if len(self._baselines) >= _BASELINE_CACHE_CAP:
            # Evict the oldest entry — NEVER clear the whole dict, which would
            # re-baseline every live thread and silently suppress announcements.
            self._baselines.pop(next(iter(self._baselines)), None)
        self._baselines[thread_id] = names
        return names

    def _read_ref(self, location: str) -> str:
        """A path the agent can pass to ``read_file`` — workspace-relative when the
        skill lives inside the workspace, else the absolute path (managed skills)."""
        if not location:
            return ""
        try:
            return str(Path(location).resolve().relative_to(self.workspace.root.resolve()))
        except ValueError:
            return location

    def _new_skill_section(self, request: Any) -> str | None:
        skills = self._current_skills()
        if not skills:
            return None
        thread_id = self._thread_id()
        baseline = self._baseline(
            thread_id, getattr(request, "state", {}), {s["name"] for s in skills}
        )
        new = [s for s in skills if s["name"] not in baseline]
        if not new:
            return None

        lines = [
            "## Skills added since this session started",
            (
                # Not "above": deepagents (0.7.23+) appends its skills section after
                # this one, because SkillsMiddleware now sits innermost in the stack.
                "These skills were installed or learned after this chat began, so they "
                "are not in your skills list. You can use them now — read "
                "the referenced SKILL.md for full instructions before invoking one."
            ),
        ]
        for s in new[:_MAX_NEW_SKILLS]:
            desc = (s.get("description") or "").strip()
            ref = self._read_ref(str(s.get("location", "")))
            suffix = f" (read `{ref}`)" if ref else ""
            lines.append(
                f"- **{s['name']}** — {desc}{suffix}" if desc else f"- **{s['name']}**{suffix}"
            )
        if len(new) > _MAX_NEW_SKILLS:
            lines.append(f"- …and {len(new) - _MAX_NEW_SKILLS} more.")
        return "\n".join(lines)

    def _inject(self, request: Any) -> Any:
        try:
            section = self._new_skill_section(request)
            if not section:
                return request
            from deepagents.middleware._utils import append_to_system_message

            new_sys = append_to_system_message(request.system_message, section)
            return request.override(system_message=new_sys)
        except Exception:
            logger.debug("skill-awareness injection failed", exc_info=True)
            return request

    def wrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return handler(self._inject(request))

    async def awrap_model_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        # A cache-miss scan is blocking disk I/O — keep it off the event loop.
        # to_thread copies contextvars, so _thread_id still sees the request ctx.
        # A hit is a few dict lookups: dispatching it would put every model call
        # of every agent behind whatever else holds the shared worker pool.
        if self._scan_is_fresh():
            return await handler(self._inject(request))
        return await handler(await asyncio.to_thread(self._inject, request))


def build_skill_awareness_middleware(
    *, config: AgentConfig, workspace: Workspace
) -> list[AgentMiddleware]:
    return [SkillAwarenessMiddleware(config=config, workspace=workspace)]
