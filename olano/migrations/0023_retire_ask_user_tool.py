"""Strip the retired ``ask_user`` tool from deployed agent configs.

``ask_user`` drew a picker on the controlling terminal for the interactive
terminal chat (``olano chat``). That chat was removed, and with it the tool:
on the dashboard and every messaging channel it could only ever answer
``unavailable``. The bundled merli, default and agentfather templates listed it
explicitly, so agents installed from them carry it in ``tools``, and
customer-service carried it in ``disabled_tools``.

``registry.resolve_tools`` skips the name quietly (``_RETIRED_TOOLS``), so an
un-migrated config still boots; rewriting the files means the Tools tab and a
human reading ``agent.json5`` stop seeing a tool that does not exist.

Removed from every list that holds OUR tool names (``tools``,
``disabled_tools``, ``extra_tools``, at any depth, so subagents and
``builtin_subagents`` overrides are covered), and as a key from the per-tool
security maps (``security.tools``, ``security.rate_limits.per_tool``).
``mcp_servers`` is skipped outright: those are the MCP server's own tool names.
``system_prompt`` is left alone; no shipped prompt names the tool.

Idempotent: a config without the name is left byte-for-byte alone, and files
are only rewritten (as plain JSON, like every earlier tool-name migration) when
something actually changed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ID = "0023_retire_ask_user_tool"

# Frozen: a migration is a historical record.
RETIRED: frozenset[str] = frozenset({"ask_user"})

# Keys whose list values hold OUR tool names.
_TOOL_LIST_KEYS = ("tools", "disabled_tools", "extra_tools")


def _strip_security_keys(security: Any) -> bool:
    if not isinstance(security, dict):
        return False
    changed = False
    maps = [security.get("tools")]
    rate_limits = security.get("rate_limits")
    if isinstance(rate_limits, dict):
        maps.append(rate_limits.get("per_tool"))
    for mapping in maps:
        if not isinstance(mapping, dict):
            continue
        for name in RETIRED & set(mapping):
            del mapping[name]
            changed = True
    return changed


def _walk(node: Any) -> bool:
    """Remove retired names in place. Returns True when anything changed."""
    changed = False
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "mcp_servers":
                continue
            if key in _TOOL_LIST_KEYS and isinstance(value, list):
                kept = [v for v in value if not (isinstance(v, str) and v in RETIRED)]
                if len(kept) != len(value):
                    node[key] = kept
                    changed = True
            elif key == "security":
                changed |= _strip_security_keys(value)
            else:
                changed |= _walk(value)
    elif isinstance(node, list):
        for item in node:
            changed |= _walk(item)
    return changed


def _repair_surrogates(text: str) -> str:
    """Recombine UTF-16 surrogate pairs (see migration 0010 for why)."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


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
            continue
        if not _walk(data):
            continue
        payload = _repair_surrogates(json.dumps(data, indent=2, ensure_ascii=False)) + "\n"
        write_agent_config_text(config_path, payload)
        logger.info("%s: removed the retired ask_user tool from %s", ID, config_path.parent.name)
