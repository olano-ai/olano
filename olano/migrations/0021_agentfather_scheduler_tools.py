"""Grant the fleet scheduler tools to existing AgentFather agents.

``olano_scheduler_list`` / ``_get`` / ``_create`` / ``_update`` / ``_delete`` /
``_run_now`` read and write every agent's scheduled jobs, across both stores —
the scheduler database and each agent's ``agent.json5 → schedule:`` block. They
ship in the archetype template; AgentFathers created earlier have an explicit
``tools`` list without them, so without this migration the only way to see or
change another agent's schedule from chat is to send the operator to the
dashboard.

Same shape and guarantees as 0015/0019: idempotent per tool, respects
``disabled_tools`` opt-outs, only touches AgentFather configs, and rewrites
serialize as plain JSON (comments dropped only on files that actually change),
with the surrogate repair and the ``isinstance(data, dict)`` guard.

The four mutating tools are ``owner_only`` and sit in HITL's ``cross_agent_exec``
/ ``write_external`` categories, so each write is approved by an admin human
individually — granting them widens nothing an operator did not already hold
through the dashboard's Scheduler tab.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0021_agentfather_scheduler_tools"

# tool name → the tool it should sit next to (falls back to append). Chained so
# the six land contiguously and in call order, whatever the existing list holds.
_TOOLS: tuple[tuple[str, str], ...] = (
    ("olano_scheduler_list", "olano_disk_monitor"),
    ("olano_scheduler_get", "olano_scheduler_list"),
    ("olano_scheduler_create", "olano_scheduler_get"),
    ("olano_scheduler_update", "olano_scheduler_create"),
    ("olano_scheduler_delete", "olano_scheduler_update"),
    ("olano_scheduler_run_now", "olano_scheduler_delete"),
)


def _is_agentfather(data: dict, dir_name: str) -> bool:
    return "agentfather" in {
        str(data.get("name") or "").lower(),
        str(data.get("archetype") or "").lower(),
        dir_name.lower(),
    }


def _repair_surrogates(text: str) -> str:
    """Recombine UTF-16 surrogate pairs into the characters they encode (frozen
    copy of the helper 0010/0013/0015/0019 carry — a migration is a historical
    record)."""
    try:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    except Exception:
        return text


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
        if not isinstance(data, dict):
            continue  # a top-level array is not an agent config
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
        payload = _repair_surrogates(json.dumps(data, indent=2, ensure_ascii=False)) + "\n"
        write_agent_config_text(config_path, payload)
        logger.info("%s: granted %s to %s", ID, ", ".join(added), config_path.parent.name)
