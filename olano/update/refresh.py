"""Refresh the olano-owned ``system/`` zone across agent workspaces.

Re-deploys bundled system prompts, knowledge docs, schemas, and policies into
existing agents without touching user-owned files. Shared by the CLI
(``olano agents refresh`` / ``olano update --all``) and the post-upgrade
self-update flow, so a new release propagates to every agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from olano.archetypes.installer import (
    emit_system_update_pulse,
    preview_system_zone,
    sync_system_zone,
)
from olano.config.agent_config import AgentConfig
from olano.config.loader import list_agents, load_agent_config


@dataclass
class AgentRefreshResult:
    """Outcome of refreshing one agent's system zone."""

    agent: str
    workspace: Path
    changed: list[str] = field(default_factory=list)
    pulse_id: str | None = None
    error: str | None = None


def _template_from_config(config: AgentConfig) -> dict[str, Any]:
    """Build the ``template`` dict that ``sync_system_zone`` expects from a config.

    Mirrors the persona/role fields the scaffolders read; the system zone is
    otherwise agent-independent.
    """
    return {
        "description": config.description,
        "system_prompt": config.system_prompt,
        "subagents": [sub.model_dump() for sub in config.subagents],
        "tools": list(config.tools),
        "emoji": config.emoji,
        "archetype": config.archetype or config.name,
    }


def refresh_agent_system_zone(
    name: str,
    *,
    source: str = "auto",
    check: bool = False,
) -> AgentRefreshResult:
    """Re-sync one agent's ``system/`` zone (or preview the diff when ``check``)."""
    config = load_agent_config(name, source=source)
    workspace_root = Path(config.workspace or ".").expanduser().resolve()
    template = _template_from_config(config)

    if check:
        changed = preview_system_zone(workspace_root, agent_name=config.name, template=template)
        return AgentRefreshResult(config.name, workspace_root, changed)

    changed = sync_system_zone(
        workspace_root,
        agent_name=config.name,
        template=template,
        overwrite=True,
    )
    pulse_id = emit_system_update_pulse(workspace_root, agent_name=config.name, changed=changed)
    return AgentRefreshResult(config.name, workspace_root, changed, pulse_id)


def refresh_all_agents(
    *,
    source: str = "auto",
    check: bool = False,
) -> list[AgentRefreshResult]:
    """Refresh every discovered agent. One broken config never aborts the sweep."""
    results: list[AgentRefreshResult] = []
    for name in list_agents(source=source, include_hidden=True):
        try:
            results.append(refresh_agent_system_zone(name, source=source, check=check))
        except Exception as exc:
            # Report per-agent and keep going — one broken config must not abort the sweep.
            results.append(AgentRefreshResult(name, Path(), error=str(exc)))
    return results
