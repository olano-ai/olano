"""Grant ``olano_list_models`` to existing AgentFather agents.

Without it AgentFather cannot see which providers this install can actually use,
so every agent it creates inherits ``anthropic:claude-sonnet-4-6`` — from the
``AgentConfig`` default or from an archetype template — and fails on its first
turn wherever there is no Anthropic key. The tool ships in the archetype
template; AgentFathers created earlier have a frozen ``tools`` list.

Same shape and guarantees as migrations 0003/0004/0005: idempotent per tool,
respects ``disabled_tools`` opt-outs, only touches AgentFather configs, adds a
read-only tool only, and rewrites serialize as plain JSON (comments dropped only
on files that actually change).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0008_agentfather_model_catalog_tool"

# tool name → the tool it should sit next to (falls back to append).
_TOOLS: tuple[tuple[str, str], ...] = (("olano_list_models", "olano_list_tools"),)


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
        for tool_name, anchor in _TOOLS:
            if tool_name in tools or tool_name in disabled:
                continue  # present, or the owner explicitly opted out
            if anchor in tools:
                tools.insert(tools.index(anchor), tool_name)
            else:
                tools.append(tool_name)
            added.append(tool_name)
        if not added:
            continue
        data["tools"] = tools
        write_agent_config_text(config_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        logger.info("%s: granted %s to %s", ID, ", ".join(added), config_path.parent.name)
