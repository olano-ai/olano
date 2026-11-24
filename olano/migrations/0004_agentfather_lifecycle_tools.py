"""Grant the agent lifecycle tools to existing AgentFather agents.

Freshly scaffolded AgentFathers get ``olano_start_agent`` /
``olano_stop_agent`` / ``olano_restart_agent`` from the archetype
template; AgentFathers created earlier have an explicit ``tools`` list without
them and could diagnose a stopped agent but not bring it back (or pause a
misbehaving one) without the human switching to the dashboard.

Same shape and guarantees as migration 0003: idempotent per tool, respects
``disabled_tools`` opt-outs, only touches AgentFather configs, and rewrites
serialize as plain JSON (comments dropped only on files that actually change).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0004_agentfather_lifecycle_tools"

_TOOLS = (
    "olano_start_agent",
    "olano_stop_agent",
    "olano_restart_agent",
)
_ANCHOR = "olano_delete_agent"


def _is_agentfather(data: dict, dir_name: str) -> bool:
    return "agentfather" in {
        str(data.get("name") or "").lower(),
        str(data.get("archetype") or "").lower(),
        dir_name.lower(),
    }


def apply(home: Path) -> None:
    from olano.config.loader import _load_json5, write_agent_config_text

    agents_dir = home / "agents"
    if not agents_dir.is_dir():
        return
    for config_path in sorted(agents_dir.glob("*/agent.json5")):
        try:
            data = _load_json5(config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("%s: skipping unparseable %s: %s", ID, config_path, exc)
            continue
        if not _is_agentfather(data, config_path.parent.name):
            continue
        tools = data.get("tools")
        if not isinstance(tools, list):
            continue
        disabled = set(data.get("disabled_tools") or [])
        added: list[str] = []
        for tool_name in _TOOLS:
            if tool_name in tools or tool_name in disabled:
                continue  # present, or the owner explicitly opted out
            # Insert next to the agent CRUD block when present, else append.
            if _ANCHOR in tools:
                tools.insert(tools.index(_ANCHOR) + 1 + len(added), tool_name)
            else:
                tools.append(tool_name)
            added.append(tool_name)
        if not added:
            continue
        data["tools"] = tools
        write_agent_config_text(config_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        logger.info("%s: granted %s to %s", ID, ", ".join(added), config_path.parent.name)
