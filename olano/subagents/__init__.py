"""ToolkitAgents — one specialist agent per integration toolkit.

A ToolkitAgent is a thin, declarative wrapper around a single toolkit in
``olano.tools``. It can be composed as a subagent (via
``builtin_subagents`` in agent.json5) or installed as a top-level agent via
``olano subagents install <Name>`` / the dashboard "Integrations" tab.

Hand-authored definitions live in ``definitions/*.json5``; toolkits without a
hand-authored file are auto-generated from the toolkit registry on first
lookup. See ``_schema.py`` for the def shape and ``registry.py`` for the
resolution API.
"""

from __future__ import annotations

from olano.subagents._schema import (
    ToolkitAgentDef,
    ToolkitAgentOverride,
)
from olano.subagents.registry import (
    list_toolkit_agents,
    load_toolkit_agent,
    resolve_as_subagent,
    resolve_for_toolkit,
)

__all__ = [
    "ToolkitAgentDef",
    "ToolkitAgentOverride",
    "list_toolkit_agents",
    "load_toolkit_agent",
    "resolve_as_subagent",
    "resolve_for_toolkit",
]
