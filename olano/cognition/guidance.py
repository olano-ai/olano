"""Optional Cortex guidance — user/agent-authored creative direction.

``CORTEX.md`` is an **OPTIONAL** workspace file that steers what the Cortex
Engine pursues when it generates on its own — the creative cognition phases
(Pulse, Brainstorming, Ideas, Prototypes, Advance). It is the Cortex-specific
analogue of ``IDENTITY.md``: where IDENTITY/SOUL shape who the agent is in
*every conversation*, CORTEX.md only shapes *autonomous* generation, so it does
NOT bloat the normal chat prompt.

Three free-form sections steer generation:

- ``## Goals``  — concrete things to work toward.
- ``## Dreams`` — bigger, aspirational "north-star" directions.
- ``## Avoid``  — topics / kinds of suggestions the user does NOT want.

The file can be edited by the user (Files view) **or** written by the agent
itself during self-learning (via :func:`append_guidance` / the
``cortex_guidance_save`` tool). When it is absent or contains only the seeded
placeholder (headings + HTML-comment hints, no real content), the reader
returns ``""`` so nothing is injected and the worker context stays lean.
"""

from __future__ import annotations

import re
from pathlib import Path

# Workspace-relative filename. A root file (like IDEAS.md / BRAINSTORMING.md)
# rather than a system/ file so the user can find and edit it easily.
CORTEX_FILE = "CORTEX.md"

# The canonical sections, in display order. Free-form sections are allowed too
# (title-cased), but these are what the seed template and the tool steer toward.
SECTIONS: tuple[str, ...] = ("Goals", "Dreams", "Avoid", "Notes")

# Cap injected into a worker's CONTEXT so a runaway file can't dominate the prompt.
_MAX_CHARS = 4000

_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Seeded into fresh workspaces. Intentionally substance-free (only headings +
# commented hints) so the reader skips it until the user/agent adds real bullets.
CORTEX_TEMPLATE = """\
# CORTEX.md — Creative direction for the Cortex Engine

<!--
OPTIONAL. This file steers what your agent pursues when it thinks on its OWN —
the Cortex Engine's generative phases: Pulse, Brainstorming, Ideas, Prototypes,
and Advance. It does NOT change normal chat (that's IDENTITY.md / SOUL.md).

Fill in any section below to nudge autonomous generation toward what matters to
you; leave a section empty to ignore it. The agent may also add to this file
during self-learning as it understands your direction better.
-->

## Goals
<!-- Concrete things to work toward, e.g.
- Ship a personal-finance dashboard this quarter
- Grow the newsletter to 1,000 subscribers
-->

## Dreams
<!-- Aspirational, north-star directions for ideas & brainstorms, e.g.
- Build a tool that makes tax filing effortless
-->

## Avoid
<!-- Topics or kinds of suggestions you do NOT want, e.g.
- No crypto-trading ideas
- Nothing that requires paid advertising
-->
"""


def _strip_comments(text: str) -> str:
    return _COMMENT_RE.sub("", text)


def _has_substance(stripped: str) -> bool:
    """True when the comment-stripped text has real content (a bullet or a
    non-heading paragraph line), not just section headings."""
    for line in stripped.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        return True
    return False


def read_cortex_guidance(workspace_root: Path, *, max_chars: int = _MAX_CHARS) -> str:
    """Return the user's Cortex guidance (comments stripped), or ``""``.

    Empty when the file is missing or contains only the seeded placeholder
    (headings + HTML-comment hints with no real content)."""
    path = Path(workspace_root) / CORTEX_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    stripped = _strip_comments(raw)
    if not _has_substance(stripped):
        return ""
    text = stripped.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n[truncated]"
    return text


def cortex_guidance_context(workspace_root: Path) -> str:
    """A CONTEXT block describing the user's creative direction, or ``""``."""
    body = read_cortex_guidance(workspace_root)
    if not body:
        return ""
    return (
        "The user's creative direction for autonomous work (from CORTEX.md). "
        "Honor it: favour work that advances the Goals/Dreams below, and steer "
        "clear of anything listed under Avoid.\n"
        f"{body}"
    )


def prepend_guidance(workspace_root: Path, context: str | None) -> str:
    """Prepend the Cortex guidance block to a worker's ``context`` when present.

    Returns ``context`` unchanged when there is no guidance, so callers can wrap
    their existing context string unconditionally."""
    block = cortex_guidance_context(workspace_root)
    if not block:
        return context or ""
    return f"{block}\n\n{context}" if context else block


def _normalize_section(section: str) -> str:
    """Map a requested section to a canonical title-cased heading."""
    cleaned = " ".join((section or "").split())
    if not cleaned:
        return "Notes"
    lowered = cleaned.lower()
    aliases = {
        "goal": "Goals",
        "goals": "Goals",
        "dream": "Dreams",
        "dreams": "Dreams",
        "aspiration": "Dreams",
        "aspirations": "Dreams",
        "avoid": "Avoid",
        "avoidance": "Avoid",
        "dont want": "Avoid",
        "don't want": "Avoid",
        "do not want": "Avoid",
        "note": "Notes",
        "notes": "Notes",
    }
    if lowered in aliases:
        return aliases[lowered]
    return cleaned[:1].upper() + cleaned[1:]


def append_guidance(workspace_root: Path, content: str, section: str = "Goals") -> str:
    """Append a bullet of creative direction under ``section`` in CORTEX.md.

    Creates the file (from the seed template) if it does not exist, inserts a
    new ``## section`` heading if absent, and de-duplicates against existing
    bullets in that section (normalized compare). Returns the canonical section
    name written to, or ``""`` when ``content`` was empty.
    """
    from olano.memory.markdown_memory import atomic_write_text

    body = " ".join((content or "").split())
    if not body:
        return ""
    canonical = _normalize_section(section)
    bullet = f"- {body}"
    norm_new = body.strip().lower()

    path = Path(workspace_root) / CORTEX_FILE
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = CORTEX_TEMPLATE

    heading = f"## {canonical}"
    pattern = re.compile(rf"({re.escape(heading)}\n)(.*?)(?=\n## |\Z)", re.DOTALL)
    match = pattern.search(existing)
    if match:
        section_body = match.group(2)
        # Skip if an equivalent bullet already lives in this section.
        for line in section_body.splitlines():
            s = line.strip()
            if s.startswith(("- ", "* ")) and s[2:].strip().lower() == norm_new:
                return canonical
        new_section_body = section_body.rstrip("\n") + f"\n{bullet}\n"
        new_content = existing[: match.start(2)] + new_section_body + existing[match.end(2) :]
    else:
        new_content = existing.rstrip() + f"\n\n{heading}\n{bullet}\n"

    atomic_write_text(path, new_content)
    return canonical
