"""Tool & bundle inspection plus global enable/disable.

Wraps :mod:`olano.tools.registry`. AgentFather uses this to answer "what
tools/bundles exist?", "what does this tool do?", and to globally retire a tool
that should never be offered to any agent.
"""

from __future__ import annotations

from typing import Any

from olano.tools.registry import (
    find_tools_by_tag,
    get_disabled_tools,
    get_dynamic_tool_source,
    get_tool_description,
    get_tool_tags,
    list_tool_names,
    set_tool_disabled,
)
from olano.tools.registry import (
    list_bundles as _list_bundles,
)
from olano.tools.registry import (
    list_integrations as _list_integrations,
)
from olano.tools.registry import (
    list_tool_tags as _list_tool_tags,
)


def list_tools() -> dict[str, Any]:
    """Return every known tool name, the globally-disabled set, and the tag index."""
    disabled = get_disabled_tools()
    return {
        "tools": list_tool_names(),
        "disabled": sorted(disabled),
        "tags": _list_tool_tags(),
    }


def find_tools(tag: str) -> dict[str, Any]:
    """Return every tool carrying ``tag``, with description and how it is wired.

    Tags are discovery labels (``customer_service``, ``privacy``, ``gdpr``, …).
    Results include builder-injected tools such as the per-customer memory
    surface, which never appear in :func:`list_tools` because they are bound to
    a live agent rather than resolved by name — those carry a ``provided_by``
    note instead of a bundle.
    """
    names = find_tools_by_tag(tag)
    return {
        "tag": tag,
        "count": len(names),
        "tools": [describe_tool(name) for name in names],
        "available_tags": sorted(_list_tool_tags()),
    }


def list_bundles() -> dict[str, list[str]]:
    """Return every bundle name mapped to the tool names it expands to."""
    return _list_bundles()


def list_integrations() -> dict[str, list[dict[str, Any]]]:
    """Concise catalog of integration tool-packs for an agent's ``tools`` list.

    Splits the registry into per-service ``toolkits`` and curated
    ``composite_bundles``, each with a ``tool_count`` / ``enabled_count``. This
    is the high-level "what integrations can I wire?" view; ``list_bundles`` and
    ``describe_tool`` drill into the individual tools.
    """
    return _list_integrations()


def describe_tool(name: str) -> dict[str, Any]:
    """Return a tool's one-line description, tags, and how it gets wired.

    ``provided_by`` is set only for builder-injected tools (per-customer memory
    and friends), which have no registry entry to describe.
    """
    return {
        "name": name,
        "description": get_tool_description(name),
        "tags": get_tool_tags(name),
        "provided_by": get_dynamic_tool_source(name),
    }


def set_tool_enabled(name: str, enabled: bool) -> dict[str, Any]:
    """Globally enable/disable a tool across all agents (dashboard-equivalent)."""
    disabled = set_tool_disabled(name, disabled=not enabled)
    return {"name": name, "enabled": enabled, "disabled_tools": sorted(disabled)}
