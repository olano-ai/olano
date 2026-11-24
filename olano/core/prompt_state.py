"""Prompt-input change detection for live agents.

The system prompt is assembled once per graph build (see
``builder._build_system_prompt``) and baked into the compiled LangGraph, so a
running agent — and every conversation thread on it — keeps whatever prompt
was current at build time. This module provides the pieces that let the
runtime notice when the *stable* prompt inputs change (config prompts, the
customer-service extension, the agent roster, identity/policy files, the tool
surface) and tell in-flight conversations about it:

- :func:`compute_prompt_state` — snapshot the stable sections
  (``PromptAssembler.stable_sections()``) + a per-section sha256 fingerprint.
  Computed right after each graph build and held on the ``AgentRunner``; the
  runtime's prompt refresh sweep recomputes it from disk and hot-swaps the
  graph when they diverge.
- :func:`diff_seen_map` — compare a thread's persisted "sections I've seen"
  marker (``prompt_sections_seen`` in ``<thread>.state.json``) against the
  current state.
- :func:`render_prompt_update_notice` — the one-shot system message injected
  into a thread's next turn, carrying ONLY the changed sections' new text.

Volatile self-written context (MEMORY.md, daily logs, PULSE items, workspace
map/bridge, notes, the MCP status block) is never fingerprinted — see
``PromptAssembler.stable_sections``.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig
    from olano.config.workspace import Workspace

logger = logging.getLogger(__name__)

# Human-facing titles for the stable section keys, in render order. Keys match
# PromptAssembler.stable_sections().
SECTION_TITLES: dict[str, str] = {
    "identity": "Identity",
    "instructions": "Instructions",
    "roster": "Agents you can collaborate with",
    "policies": "Safety & execution policies",
    "environment": "Workspace environment",
    "tools": "Tools guidance & inventory",
}

# Per-section body cap inside a notice; the full text is already live in the
# rebuilt graph's system prompt, so the notice only needs enough to be salient.
_SECTION_MAX_CHARS = 3000

# Total notice cap. Oversized notices collapse their largest sections to a
# one-line marker (largest first) until they fit.
_NOTICE_MAX_CHARS = 8000

_TRUNCATION_MARKER = (
    "…[truncated — the full text is already in effect in your current instructions]"
)
_COLLAPSED_BODY = "(Updated — the new version is in effect in your current instructions.)"
_REMOVED_BODY = "(This section was removed from your instructions and no longer applies.)"

_NOTICE_HEADER = (
    "[System update — your operating instructions changed]\n"
    "Parts of your operating instructions were updated after this conversation "
    "started. The sections below are the new authoritative versions: they replace "
    "the same-named sections you were originally given, and they override anything "
    "earlier in this conversation that conflicts with them."
)
_NOTICE_FOOTER = (
    "Apply these updates from now on. Do not announce or explain this update "
    "mechanism to the user; only discuss it if they ask about your instructions."
)


@dataclass(frozen=True)
class PromptState:
    """Snapshot of an agent's stable prompt sections + their fingerprint.

    ``fingerprint`` is ``{section_key: sha256hex}``. An **empty** fingerprint
    means "state unavailable" (compute failed, or the agent runs a non-``full``
    prompt mode where the stable sections aren't in the prompt) — consumers
    must treat it as "feature inert", never as "everything changed". A
    successful ``full``-mode compute always carries every non-roster key, even
    for empty section text, so clearing a file still registers as a change.
    """

    sections: dict[str, str] = field(default_factory=dict)
    fingerprint: dict[str, str] = field(default_factory=dict)
    built_at: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_prompt_state(
    config: AgentConfig,
    workspace: Workspace | None = None,
) -> PromptState:
    """Snapshot the stable prompt sections for ``config``. Never raises.

    Cheap by design — file reads plus the in-process peer registry; no MCP
    connections, no PULSE, no cartography. Returns an empty (inert) state on
    any failure and for agents whose ``prompt_mode`` is not ``full`` (their
    prompt doesn't carry the stable sections, so "your instructions changed"
    notices would be wrong).
    """
    try:
        from olano.config.workspace import get_workspace
        from olano.core.prompt_assembler import PromptAssembler

        ws = workspace or get_workspace(config.workspace)
        assembler = PromptAssembler(config, ws)
        if assembler.resolve_mode() != "full":
            return PromptState(built_at=_now_iso())
        sections = assembler.stable_sections()
        fingerprint = {
            key: hashlib.sha256(text.encode("utf-8")).hexdigest() for key, text in sections.items()
        }
        return PromptState(
            sections=dict(sections),
            fingerprint=fingerprint,
            built_at=_now_iso(),
        )
    except Exception as e:
        logger.debug("compute_prompt_state failed for %s: %s", getattr(config, "name", "?"), e)
        return PromptState(built_at=_now_iso())


def diff_seen_map(
    seen: dict[str, str],
    current: PromptState,
) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """Compare a thread's seen-fingerprint map against the current state.

    Returns ``(changed, removed, updated_seen)``:

    - ``changed`` — ``{section_key: new_text}`` for sections whose hash moved
      and whose new text is non-empty.
    - ``removed`` — section keys whose hash moved to empty text (the section
      was cleared/removed).
    - ``updated_seen`` — the map to persist back to the thread state.

    Comparison is intersection-only: keys present in ``current`` but not in
    ``seen`` are adopted silently (new section appearing, or first sight after
    rollout), and keys present in ``seen`` but not in ``current`` are kept
    untouched (e.g. a CLI process that cannot compute the ``roster`` section
    must not clobber the daemon's marker for it).
    """
    changed: dict[str, str] = {}
    removed: list[str] = []
    updated_seen = dict(seen)
    for key, digest in current.fingerprint.items():
        if key not in seen:
            updated_seen[key] = digest
            continue
        if seen[key] == digest:
            continue
        updated_seen[key] = digest
        text = current.sections.get(key, "")
        if text.strip():
            changed[key] = text
        else:
            removed.append(key)
    return changed, removed, updated_seen


def _section_order(key: str) -> int:
    try:
        return list(SECTION_TITLES).index(key)
    except ValueError:
        return len(SECTION_TITLES)


def render_prompt_update_notice(changed: dict[str, str], removed: list[str]) -> str:
    """Render the one-shot in-thread system message for changed sections.

    Empty string when there is nothing to say. Bodies are capped at
    ``_SECTION_MAX_CHARS``; if the assembled notice still exceeds
    ``_NOTICE_MAX_CHARS``, the largest bodies collapse to a one-line marker
    (largest first) until it fits — the rebuilt system prompt already carries
    the full text, the notice only makes the change salient.
    """
    if not changed and not removed:
        return ""

    keys = sorted(set(changed) | set(removed), key=_section_order)
    bodies: dict[str, str] = {}
    for key in keys:
        if key in changed:
            body = changed[key].strip()
            if len(body) > _SECTION_MAX_CHARS:
                body = body[:_SECTION_MAX_CHARS] + "\n" + _TRUNCATION_MARKER
            bodies[key] = body
        else:
            bodies[key] = _REMOVED_BODY

    def _assemble() -> str:
        parts = [_NOTICE_HEADER]
        for key in keys:
            title = SECTION_TITLES.get(key, key)
            label = "Updated" if key in changed else "Removed"
            parts.append(f"### {label}: {title}\n{bodies[key]}")
        parts.append(_NOTICE_FOOTER)
        return "\n\n".join(parts)

    notice = _assemble()
    if len(notice) <= _NOTICE_MAX_CHARS:
        return notice
    # Collapse largest updated bodies first until the notice fits.
    for key in sorted(changed, key=lambda k: len(bodies.get(k, "")), reverse=True):
        bodies[key] = _COLLAPSED_BODY
        notice = _assemble()
        if len(notice) <= _NOTICE_MAX_CHARS:
            break
    return notice
