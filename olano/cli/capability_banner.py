"""Capability-inventory banner rendered by the ``/tools full`` chat command."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

_MAX_GROUPS_SHOWN = 3
_MAX_ITEMS_PER_GROUP = 3


def _toolset_from_name(name: str) -> str:
    """Derive toolset prefix from tool name (e.g. 'github_create_pr' → 'github')."""
    return name.split("_")[0] if "_" in name else name


def _group_by(items: list[str], key_fn) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for item in items:
        groups[key_fn(item)].append(item)
    return dict(groups)


def _render_group_lines(
    label: str,
    groups: dict[str, list[str]],
    more_label: str,
) -> list[str]:
    lines = [f"{label}:"]
    for shown_groups, (group_name, entries) in enumerate(sorted(groups.items())):
        if shown_groups >= _MAX_GROUPS_SHOWN:
            remaining = len(groups) - _MAX_GROUPS_SHOWN
            lines.append(f"  (and {remaining} more {more_label}…)")
            break
        visible = entries[:_MAX_ITEMS_PER_GROUP]
        suffix = ""
        if len(entries) > _MAX_ITEMS_PER_GROUP:
            suffix = f"  (and {len(entries) - _MAX_ITEMS_PER_GROUP} more)"
        lines.append(f"  {group_name}:  {', '.join(visible)}{suffix}")
    return lines


def render_banner(
    tool_names: list[str],
    skills: list[dict[str, str]],
    *,
    full: bool = False,
) -> str:
    """
    Render a compact capability inventory string.

    Args:
        tool_names: List of registered tool name strings.
        skills: List of skill metadata dicts from discover_skills (must have 'name', 'category').
        full: When True, no truncation (for /tools command).
    """
    max_groups = None if full else _MAX_GROUPS_SHOWN
    max_items = None if full else _MAX_ITEMS_PER_GROUP

    # --- Tools section ---
    tool_groups = _group_by(tool_names, _toolset_from_name)
    tool_lines = ["Tools:"]
    for shown_tool_groups, (group_name, entries) in enumerate(sorted(tool_groups.items())):
        if max_groups is not None and shown_tool_groups >= max_groups:
            remaining = len(tool_groups) - max_groups
            tool_lines.append(f"  (and {remaining} more toolsets…)")
            break
        visible = entries if max_items is None else entries[:max_items]
        suffix = ""
        if max_items is not None and len(entries) > max_items:
            suffix = f"  (and {len(entries) - max_items} more)"
        tool_lines.append(f"  {group_name}:  {', '.join(visible)}{suffix}")

    # --- Skills section ---
    skill_groups: dict[str, list[str]] = defaultdict(list)
    for s in skills:
        category = s.get("category", "general")
        skill_groups[category].append(s.get("name", ""))

    skill_lines = ["Skills:"]
    for shown_skill_groups, (group_name, entries) in enumerate(sorted(skill_groups.items())):
        if max_groups is not None and shown_skill_groups >= max_groups:
            remaining = len(skill_groups) - max_groups
            skill_lines.append(f"  (and {remaining} more categories…)")
            break
        visible = entries if max_items is None else entries[:max_items]
        suffix = ""
        if max_items is not None and len(entries) > max_items:
            suffix = f"  (and {len(entries) - max_items} more)"
        skill_lines.append(f"  {group_name}:  {', '.join(visible)}{suffix}")

    if not skills:
        skill_lines = ["Skills:  (none)"]

    footer = f"{len(tool_names)} tools · {len(skills)} skills · /tools for full list"

    sections = []
    if tool_names:
        sections.append("\n".join(tool_lines))
    sections.append("\n".join(skill_lines))
    sections.append(footer)
    return "\n\n".join(sections)


def get_tool_names_from_registry() -> list[str]:
    """Return all registered tool names from the tool registry."""
    try:
        from olano.tools.registry import list_tool_names

        return list_tool_names()
    except Exception:
        return []


def get_skills_from_dirs(skill_dirs: list[str], config: Any = None) -> list[dict[str, str]]:
    """Gather skill metadata dicts from a list of skill directory paths.

    ``config`` is optional and only used to drop the skills that agent turned
    off (``agent.json5 → disabled_skills``) or that are gated by
    ``requires_tools`` it lacks — the banner answers "what can I do", so a
    skill the agent will never load has no business being in it.
    """
    from olano.skills.loader import (
        disabled_skill_names,
        discover_skills,
        effective_tool_names,
        gated_skill_reason,
    )

    disabled = disabled_skill_names(config)
    effective = effective_tool_names(config)
    all_skills: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for d in skill_dirs:
        for s in discover_skills(Path(d)):
            if str(s["name"]).lower() in disabled:
                continue
            if gated_skill_reason(s, effective):
                continue
            if s["name"] not in seen_names:
                all_skills.append(s)
                seen_names.add(s["name"])
    return all_skills
