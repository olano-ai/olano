"""Rename the ``lionclaw_*`` admin tools to ``olano_*`` in deployed configs.

P4 of the lionclaw → olano rename renamed the toolkit's 53 tools and the
``lionclaw_admin`` toolkit alias. Agent configs on disk list those tools **by
name**, and :func:`olano.tools.registry.resolve_tools` treats an unknown name
as a warning, not an error — it prints "unknown tool 'x', skipping" and carries
on. Without this migration AgentFather would boot clean, report healthy, and
silently have **zero tools**.

Scope is deliberately narrow: an entry is rewritten only when the whole string
is exactly one of the names below. No substring matching, no regex over
arbitrary config values. ``mcp_servers[].disabled_tools`` is skipped outright —
those are the MCP server's own tool names, not ours.

``system_prompt`` is also updated, because AgentFather's prompt instructs the
model to call these tools by name; leaving it would tell the model to use tools
that no longer exist. That is a word-boundary replacement of the same fixed
list, so authored prose survives untouched.

Idempotent: a config with no old names is left byte-for-byte alone, and files
are only rewritten when something actually changed.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ID = "0010_olano_tool_names"

# Frozen snapshot of the P4 rename. Never regenerate this from the live
# registry — a migration is a historical record, and the old names no longer
# exist to be discovered.
RENAMES: dict[str, str] = {
    "lionclaw_add_mcp_server": "olano_add_mcp_server",
    "lionclaw_add_subagent": "olano_add_subagent",
    "lionclaw_agent_activity": "olano_agent_activity",
    "lionclaw_agent_errors": "olano_agent_errors",
    "lionclaw_agent_status": "olano_agent_status",
    "lionclaw_authorize_mcp_server": "olano_authorize_mcp_server",
    "lionclaw_create_agent": "olano_create_agent",
    "lionclaw_create_skill": "olano_create_skill",
    "lionclaw_delete_agent": "olano_delete_agent",
    "lionclaw_describe_tool": "olano_describe_tool",
    "lionclaw_diagnose_agent": "olano_diagnose_agent",
    "lionclaw_doctor": "olano_doctor",
    "lionclaw_find_tools": "olano_find_tools",
    "lionclaw_get_agent": "olano_get_agent",
    "lionclaw_get_home_config": "olano_get_home_config",
    "lionclaw_install_skill": "olano_install_skill",
    "lionclaw_list_agent_workspace": "olano_list_agent_workspace",
    "lionclaw_list_agents": "olano_list_agents",
    "lionclaw_list_archetypes": "olano_list_archetypes",
    "lionclaw_list_builtin_agents": "olano_list_builtin_agents",
    "lionclaw_list_builtin_skills": "olano_list_builtin_skills",
    "lionclaw_list_bundles": "olano_list_bundles",
    "lionclaw_list_integrations": "olano_list_integrations",
    "lionclaw_list_mcp_servers": "olano_list_mcp_servers",
    "lionclaw_list_models": "olano_list_models",
    "lionclaw_list_platforms": "olano_list_platforms",
    "lionclaw_list_sessions": "olano_list_sessions",
    "lionclaw_list_skills": "olano_list_skills",
    "lionclaw_list_subagents": "olano_list_subagents",
    "lionclaw_list_tools": "olano_list_tools",
    "lionclaw_mcp_oauth_status": "olano_mcp_oauth_status",
    "lionclaw_mcp_status": "olano_mcp_status",
    "lionclaw_memory_monitor": "olano_memory_monitor",
    "lionclaw_memory_report": "olano_memory_report",
    "lionclaw_read_agent_file": "olano_read_agent_file",
    "lionclaw_read_archetype": "olano_read_archetype",
    "lionclaw_read_session": "olano_read_session",
    "lionclaw_recent_errors": "olano_recent_errors",
    "lionclaw_remove_mcp_server": "olano_remove_mcp_server",
    "lionclaw_restart_agent": "olano_restart_agent",
    "lionclaw_run_agent_command": "olano_run_agent_command",
    "lionclaw_runtime_status": "olano_runtime_status",
    "lionclaw_scaffold_archetype": "olano_scaffold_archetype",
    "lionclaw_set_tool_enabled": "olano_set_tool_enabled",
    "lionclaw_start_agent": "olano_start_agent",
    "lionclaw_stop_agent": "olano_stop_agent",
    "lionclaw_tail_log": "olano_tail_log",
    "lionclaw_test_mcp_server": "olano_test_mcp_server",
    "lionclaw_toggle_subagent": "olano_toggle_subagent",
    "lionclaw_update": "olano_update",
    "lionclaw_update_agent": "olano_update_agent",
    "lionclaw_update_home_config": "olano_update_home_config",
    "lionclaw_write_agent_file": "olano_write_agent_file",
    # The toolkit alias, usable as a single entry in ``tools``.
    "lionclaw_admin": "olano_admin",
}

_PROMPT_RE = re.compile(r"(?<![\w])(" + "|".join(map(re.escape, RENAMES)) + r")(?![\w])")

# Keys whose list values hold OUR tool names.
_TOOL_LIST_KEYS = ("tools", "disabled_tools")


def _rename_list(values: list[Any]) -> tuple[list[Any], bool]:
    out: list[Any] = []
    changed = False
    for v in values:
        if isinstance(v, str) and v in RENAMES:
            out.append(RENAMES[v])
            changed = True
        else:
            out.append(v)
    return out, changed


def _walk(node: Any) -> bool:
    """Rewrite tool names in place. Returns True when anything changed."""
    changed = False
    if isinstance(node, dict):
        for key, value in node.items():
            # An MCP server's disabled_tools are that server's tool names.
            if key == "mcp_servers":
                continue
            if key in _TOOL_LIST_KEYS and isinstance(value, list):
                node[key], hit = _rename_list(value)
                changed |= hit
            elif key == "system_prompt" and isinstance(value, str):
                new = _PROMPT_RE.sub(lambda m: RENAMES[m.group(1)], value)
                if new != value:
                    node[key] = new
                    changed = True
            else:
                changed |= _walk(value)
    elif isinstance(node, list):
        for item in node:
            changed |= _walk(item)
    return changed


def _repair_surrogates(text: str) -> str:
    """Recombine UTF-16 surrogate pairs into the characters they encode.

    ``_load_json5`` does not join ``\\ud83e\\udd81`` into a single astral
    character the way :func:`json.loads` does — it yields two lone surrogates.
    Any config written with ``ensure_ascii=True`` therefore parses into a string
    that ``ensure_ascii=False`` cannot re-encode, and the write dies with
    "surrogates not allowed". AgentFather's prompt carries emoji, so this is a
    live path, not a theoretical one.
    """
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
        logger.info("%s: renamed olano tool names in %s", ID, config_path.parent.name)
