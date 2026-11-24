"""Grant the MCP directory-read tools to existing AgentFather agents.

``olano_search_mcp_directory`` and ``olano_get_mcp_directory_entry`` expose the
cloud-synced MCP server catalog — verified endpoint URL, transport, auth mode,
and the credential names each server expects. The registry has always been
there (``core/mcp_registry``), but nothing wrapped it as a tool, so it was
reachable only from the dashboard and ``olano mcp search``. An AgentFather
without them recommends MCP servers from model memory, which is how a subtly
wrong endpoint gets written into an agent's config and fails only later, in
front of a user.

They ship in the archetype template; AgentFathers created earlier have an
explicit ``tools`` list without them.

Same shape and guarantees as migrations 0003/0004/0005: idempotent per tool,
respects ``disabled_tools`` opt-outs, only touches AgentFather configs, adds
read-only tools only, and rewrites serialize as plain JSON (comments dropped
only on files that actually change). Plus the two hardenings 0010/0013 learned
the hard way and 0005 never picked up: the surrogate repair on the payload and
the ``isinstance(data, dict)`` guard.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0015_agentfather_mcp_directory_tools"

# tool name → the tool it should sit next to (falls back to append).
_TOOLS: tuple[tuple[str, str], ...] = (
    ("olano_search_mcp_directory", "olano_list_mcp_servers"),
    ("olano_get_mcp_directory_entry", "olano_search_mcp_directory"),
)


def _is_agentfather(data: dict, dir_name: str) -> bool:
    return "agentfather" in {
        str(data.get("name") or "").lower(),
        str(data.get("archetype") or "").lower(),
        dir_name.lower(),
    }


def _repair_surrogates(text: str) -> str:
    """Recombine UTF-16 surrogate pairs into the characters they encode.

    ``_load_json5`` yields lone surrogates for ``\\ud83e\\udd81``-style escapes
    when a file takes the json5-fallback parse (unquoted keys, single quotes),
    and ``ensure_ascii=False`` passes them through — ``write_text`` then raises
    ``UnicodeEncodeError`` and the failed migration re-runs (and re-fails) on
    every boot, blocking all later migrations. Same frozen copy as 0010/0013 —
    a migration is a historical record, never regenerated from live code.
    """
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
