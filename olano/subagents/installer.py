"""Top-level installer for ToolkitAgents.

Materializes a ``ToolkitAgentDef`` into a fully-formed ``agent.json5`` with
its own workspace, mirroring ``olano.archetypes.installer.install_archetype``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from olano.config.loader import write_agent_config_text
from olano.subagents._schema import ToolkitAgentOverride
from olano.subagents.registry import (
    _expand_tools,
    effective_security,
    load_toolkit_agent,
)

logger = logging.getLogger(__name__)


def install_toolkit_agent_as_top_level(
    name: str,
    *,
    install_name: str | None = None,
    extra_tools: list[str] | None = None,
    disabled_tools: list[str] | None = None,
    model: str | None = None,
    overwrite: bool = False,
    agents_dir: Path | None = None,
    display_name: str | None = None,
    title: str | None = None,
) -> Path:
    """Install a ToolkitAgent as a top-level agent under ``<home>/agents/<name>/``.

    ``model=None`` resolves through
    :func:`olano.archetypes.installer.default_archetype_model`, exactly as an
    archetype install does — the dashboard's "New agent" modal sends no model on
    either tab, and a managed box has no provider key for the ``AgentConfig``
    default to fall back to.

    Returns the path to the written ``agent.json5``.
    """
    from olano.archetypes.installer import default_archetype_model, scaffold_agent_workspace
    from olano.config.home import get_olano_paths

    defn = load_toolkit_agent(name)
    model = model or default_archetype_model(name)
    target_name = install_name or defn.name
    base_dir = agents_dir or get_olano_paths().agents_dir
    agent_dir = base_dir / target_name
    agent_dir.mkdir(parents=True, exist_ok=True)

    config_path = agent_dir / "agent.json5"
    if config_path.exists() and not overwrite:
        raise FileExistsError(
            f"Agent '{target_name}' already exists at {config_path}. "
            "Pass overwrite=True to replace it."
        )

    override = ToolkitAgentOverride(
        name=defn.name,
        extra_tools=list(extra_tools or []),
        disabled_tools=list(disabled_tools or []),
        model=model,
    )
    resolved_tools = _expand_tools(defn, override)
    security = effective_security(defn)

    workspace_root = agent_dir / "workspace"

    template: dict = {
        "name": target_name,
        # The catalog label is the human name the picker showed; without it the
        # installed agent goes by whatever slug the toolkit definition carries.
        "display_name": (display_name or "").strip(),
        "toolkit_agent": defn.name,  # provenance — survives in agent.json5
        "description": defn.description,
        "emoji": defn.emoji,
        "system_prompt": defn.system_prompt,
        "tools": resolved_tools,
        "subagents": [],
        "skills": [],
        "mcp_servers": [],
        "channels": [],
        "schedule": [],
        "memory": {
            "markdown": True,
            "rag": False,
            "self_learning": True,
            "self_learning_interval": 3600,
        },
        "backend": {
            "type": "local_shell",
            "root_dir": "workspace",
            "virtual_mode": False,
        },
        "security": security.model_dump(),
        "pulse": {"enabled": True},
    }
    if model:
        template["model"] = model
    # The position, in the same field an archetype install writes (`org.title`)
    # so the roster, the prompt and the org chart read one answer. The picker
    # sends the catalog label here; a CLI install may send nothing.
    if (title or "").strip():
        template["org"] = {"title": (title or "").strip()}

    write_agent_config_text(config_path, json.dumps(template, indent=2, ensure_ascii=False) + "\n")

    try:
        scaffold_agent_workspace(target_name, template, workspace_root, overwrite=overwrite)
    except Exception as exc:
        logger.warning("Workspace scaffold for '%s' failed: %s", target_name, exc)

    (agent_dir / "skills").mkdir(exist_ok=True)
    logger.info("ToolkitAgent '%s' installed as '%s' at %s", defn.name, target_name, config_path)
    return config_path
