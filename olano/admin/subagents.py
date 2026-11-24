"""Subagent management — add/list/toggle subagents on a parent agent.

Edits the parent agent's ``subagents`` list (validated through
:class:`~olano.config.agent_config.SubAgentConfig`).
"""

from __future__ import annotations

from typing import Any

from olano.config.agent_config import AgentConfig, SubAgentConfig
from olano.config.loader import load_agent_config, mutate_agent_config


def list_subagents(agent: str) -> list[dict[str, Any]]:
    """Return the subagents already defined on ``agent``.

    For the catalog of ready-made agents that *can* be attached to any agent (or
    installed standalone), use :func:`list_builtin_agents`.
    """
    cfg = load_agent_config(agent, source="home")
    return [
        {
            "name": s.name,
            "description": s.description,
            "model": s.model,
            "tools": s.tools,
            "enabled": s.enabled,
        }
        for s in cfg.subagents
    ]


def list_builtin_agents() -> dict[str, Any]:
    """Unified catalog of every ready-made agent that ships with olano.

    One list spanning three kinds — all usable EITHER as a standalone agent OR
    as a subagent of another agent:

      * ``team``        — archetype teams (a supervisor + expert subagents).
      * ``expert``      — archetype specialists (single-domain agents).
      * ``integration`` — ToolkitAgents: one specialist per toolkit
                          (GoogleAgent, StripeAgent, GitHubAgent…).

    Plus the built-in ``ops`` OpsManager (subagent-only). Each row carries a
    ``ref`` (the identifier to reference it by), ``usable_as`` (``agent`` /
    ``subagent`` / both), and an ``install`` hint for each path:

      * As an **agent**:   archetypes via ``olano_scaffold_archetype`` (ref);
                            integration agents via ``olano_create_agent`` with
                            the toolkit's tools (or the dashboard Integrations tab).
      * As a **subagent**: archetypes via a ``subagents`` entry with
                            ``from_archetype: <ref>``; integration agents + ops
                            via the parent's ``builtin_subagents: [<ref>]``.

    Distinct from :func:`list_subagents` (the subagents already attached to one
    agent).
    """
    from olano.archetypes.installer import build_archetype_catalog
    from olano.subagents.registry import list_toolkit_agents

    agents: list[dict[str, Any]] = []
    catalog = build_archetype_catalog()

    _ARCHETYPE_INSTALL = {
        "as_agent": "olano_scaffold_archetype",
        "as_subagent": "subagents[].from_archetype",
    }
    for entry in catalog["teams"]:
        agents.append(
            {
                "name": entry.get("label") or entry["name"],
                "ref": entry["name"],
                "kind": "team",
                "description": entry.get("description", ""),
                "category": entry.get("category") or "Team",
                "emoji": entry.get("emoji", "🤖"),
                "subagent_count": entry.get("subagent_count"),
                "usable_as": ["agent", "subagent"],
                "install": _ARCHETYPE_INSTALL,
            }
        )
    for entry in catalog["specialists"]:
        agents.append(
            {
                "name": entry.get("label") or entry["name"],
                "ref": entry["name"],
                "kind": "expert",
                "description": entry.get("description", ""),
                "category": entry.get("category") or "Expert",
                "emoji": entry.get("emoji", "🤖"),
                "usable_as": ["agent", "subagent"],
                "install": _ARCHETYPE_INSTALL,
            }
        )

    agents.append(
        {
            "name": "OpsManager",
            "ref": "ops",
            "kind": "ops",
            "description": (
                "Operations manager for scheduler, heartbeat, curated PULSE, "
                "approvals, idea capture, and outbound reports."
            ),
            "category": "Operations",
            "emoji": "🛠️",
            "credential_env_vars": [],
            "usable_as": ["subagent"],
            "install": {"as_subagent": "builtin_subagents"},
        }
    )

    for defn in list_toolkit_agents():
        agents.append(
            {
                "name": defn.name,
                "ref": defn.toolkit,
                "kind": "integration",
                "description": defn.description,
                "category": defn.category,
                "emoji": defn.emoji,
                "credential_env_vars": list(defn.credential_env_vars),
                "tags": list(defn.tags),
                "catalog_visible": defn.catalog_visible,
                "usable_as": ["agent", "subagent"],
                "install": {
                    "as_agent": "olano_create_agent (+ toolkit tools) / dashboard Integrations",
                    "as_subagent": "builtin_subagents",
                },
            }
        )

    return {"agents": agents, "count": len(agents)}


def add_subagent(
    agent: str,
    name: str,
    *,
    description: str,
    system_prompt: str,
    tools: list[str] | None = None,
    skills: list[str] | None = None,
    model: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Add (or replace, with ``overwrite``) a subagent on ``agent`` and save."""
    sub = SubAgentConfig(
        name=name,
        description=description,
        system_prompt=system_prompt,
        tools=tools or [],
        skills=skills or [],
        model=model,
    )

    def _apply(cfg: AgentConfig) -> None:
        existing = next((s for s in cfg.subagents if s.name == name), None)
        if existing is not None and not overwrite:
            raise FileExistsError(
                f"Subagent '{name}' already exists on '{agent}'. Pass overwrite=True."
            )
        cfg.subagents = [s for s in cfg.subagents if s.name != name] + [sub]

    mutate_agent_config(agent, _apply)
    return {"agent": agent, "subagent": name, "added": True}


def toggle_subagent(agent: str, name: str, enabled: bool) -> dict[str, Any]:
    """Enable or disable an existing subagent on ``agent``."""

    def _apply(cfg: AgentConfig) -> None:
        for s in cfg.subagents:
            if s.name == name:
                s.enabled = enabled
                return
        raise ValueError(f"Agent '{agent}' has no subagent named {name!r}.")

    mutate_agent_config(agent, _apply)
    return {"agent": agent, "subagent": name, "enabled": enabled}
