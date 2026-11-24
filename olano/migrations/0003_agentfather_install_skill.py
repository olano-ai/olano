"""Grant the cross-agent workspace tools to existing AgentFather agents.

Freshly scaffolded AgentFathers get these from the archetype template, but
AgentFathers created before they existed have an explicit ``tools`` list in
their agent.json5 that doesn't include them — and without them there is NO
supported way to hand a skill directory to another agent or to inspect/seed
another agent's workspace files: their shell is isolated from other agents'
workspaces by design, so the observed failure mode is a dead-end ``mkdir:
cannot create directory '…/agents/<other>': Permission denied`` followed by
"no tool exposes this".

Idempotent: per tool, skips agents that already list it, that put it in
``disabled_tools`` (the owner opted out), or that aren't AgentFather. Rewrites
serialize as plain JSON — the same convention as every programmatic
``agent.json5`` write — so ``//`` comments are dropped only on files that
actually change.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0003_agentfather_install_skill"

_TOOLS = (
    "olano_install_skill",
    "olano_list_agent_workspace",
    "olano_read_agent_file",
    "olano_write_agent_file",
)
_ANCHOR = "olano_create_skill"


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
            # Insert next to the sibling skill tool when present, else append.
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
