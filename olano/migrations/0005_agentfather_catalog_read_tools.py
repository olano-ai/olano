"""Grant the catalog-read tools to existing AgentFather agents.

``olano_read_archetype`` (read an archetype template in full) and
``olano_find_tools`` (find tools by discovery tag, including the
builder-injected ones that never appear in ``olano_list_tools``) ship in the
archetype template; AgentFathers created earlier have an explicit ``tools`` list
without them and can only see the picker fields of an archetype — so they answer
"what does this archetype ship with?" from generic defaults instead of from the
template, and cannot find a capability without already knowing its tool name.

Same shape and guarantees as migrations 0003/0004: idempotent per tool, respects
``disabled_tools`` opt-outs, only touches AgentFather configs, adds read-only
tools only, and rewrites serialize as plain JSON (comments dropped only on files
that actually change).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0005_agentfather_catalog_read_tools"

# tool name → the tool it should sit next to (falls back to append).
_TOOLS: tuple[tuple[str, str], ...] = (
    ("olano_read_archetype", "olano_list_archetypes"),
    ("olano_find_tools", "olano_describe_tool"),
)


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
                tools.insert(tools.index(anchor) + 1, tool_name)
            else:
                tools.append(tool_name)
            added.append(tool_name)
        if not added:
            continue
        data["tools"] = tools
        write_agent_config_text(config_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        logger.info("%s: granted %s to %s", ID, ", ".join(added), config_path.parent.name)
