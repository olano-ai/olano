"""Grant ``olano_test_agent`` to existing AgentFather agents.

AgentFather builds and changes agents but could only predict how they would
behave. ``olano_test_agent`` runs a real turn of another agent on a separate
``agenttest::`` thread and returns its reply and tool calls. It ships in the
archetype template; AgentFathers created earlier have an explicit ``tools``
list without it.

Same shape and guarantees as 0021: idempotent, respects ``disabled_tools``
opt-outs, only touches AgentFather configs, and rewrites serialize as plain JSON
(comments dropped only on files that actually change), with the surrogate repair
and the ``isinstance(data, dict)`` guard.

The tool is ``owner_only``, refuses any turn without an administrator on it, and
sits in HITL's ``cross_agent_exec`` category, so each test is approved by an
admin individually.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0026_agentfather_test_agent"

# tool name → the tool it should sit next to (falls back to append).
_TOOLS: tuple[tuple[str, str], ...] = (("olano_test_agent", "olano_run_agent_command"),)


def _is_agentfather(data: dict, dir_name: str) -> bool:
    return "agentfather" in {
        str(data.get("name") or "").lower(),
        str(data.get("archetype") or "").lower(),
        dir_name.lower(),
    }


def _repair_surrogates(text: str) -> str:
    """Recombine UTF-16 surrogate pairs into the characters they encode (frozen
    copy of the helper 0010/0013/0015/0019 carry; a migration is a historical
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
