"""Prefix every link-minting tool with ``magic_link_``.

The six tools that mint a magic link shipped under bare verbs
(``connect_service``, ``open_dashboard``, …) while their sibling was already
``magic_link_status``. They are one family — each hands off to a human in a
browser — and the shared prefix says so, in the tool list the model reads.

``registry._ALIASES`` resolves the old names, so an un-migrated config keeps
working; rewriting the files means a human reading one sees the name the tool
actually has, and AgentFather's own prompt stops teaching names that only
resolve through a compatibility shim.

Beyond migration 0013's shape, this one also rewrites tool names used as
**dict keys**:

* ``security.tools`` — per-tool approval overrides (``always``/``never`` ask).
  Left stale, an operator's deliberate "always ask" for ``open_dashboard``
  becomes dead config while the tool keeps running, which is a security
  regression that looks like nothing at all.
* ``security.rate_limits.per_tool`` — same shape, same silent-drop problem.

Idempotent: a home with none of the old names is left byte-for-byte alone, and
files are only rewritten when something actually changed.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ID = "0016_magic_link_tool_prefix"

# Frozen snapshot of this rename — never regenerate from the live registry.
RENAMES: dict[str, str] = {
    "connect_service": "magic_link_connect_service",
    "request_secret": "magic_link_request_secret",
    "edit_instructions": "magic_link_edit_instructions",
    "request_file_edit": "magic_link_request_file_edit",
    "connect_channels": "magic_link_connect_channels",
    "open_dashboard": "magic_link_open_dashboard",
}

_PROMPT_RE = re.compile(r"(?<![\w])(" + "|".join(map(re.escape, RENAMES)) + r")(?![\w])")

# Keys whose list values hold OUR tool names.
_TOOL_LIST_KEYS = ("tools", "disabled_tools")
# Keys whose dict KEYS are tool names.
_TOOL_KEYED_MAPS = ("tools", "per_tool")


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


def _rename_map_keys(mapping: dict) -> tuple[dict, bool]:
    out: dict = {}
    changed = False
    for k, v in mapping.items():
        if isinstance(k, str) and k in RENAMES:
            out[RENAMES[k]] = v
            changed = True
        else:
            out[k] = v
    return out, changed


def _walk(node: Any, *, in_security: bool = False) -> bool:
    """Rewrite tool names in place. Returns True when anything changed."""
    changed = False
    if isinstance(node, dict):
        for key, value in list(node.items()):
            # An MCP server's disabled_tools are that server's tool names.
            if key == "mcp_servers":
                continue
            # `security.tools` / `security.rate_limits.per_tool` are keyed BY
            # tool name. Elsewhere a `tools` key is a plain list (handled below).
            if key in _TOOL_KEYED_MAPS and isinstance(value, dict) and in_security:
                node[key], hit = _rename_map_keys(value)
                changed |= hit
                changed |= _walk(node[key], in_security=True)
            elif key in _TOOL_LIST_KEYS and isinstance(value, list):
                node[key], hit = _rename_list(value)
                changed |= hit
            elif key == "system_prompt" and isinstance(value, str):
                new = _PROMPT_RE.sub(lambda m: RENAMES[m.group(1)], value)
                if new != value:
                    node[key] = new
                    changed = True
            else:
                changed |= _walk(value, in_security=in_security or key == "security")
    elif isinstance(node, list):
        for item in node:
            changed |= _walk(item, in_security=in_security)
    return changed


def _repair_surrogates(text: str) -> str:
    """Recombine UTF-16 surrogate pairs into the characters they encode.

    ``_load_json5`` yields lone surrogates for ``\\ud83e\\udd81``-style escapes,
    which ``ensure_ascii=False`` then cannot encode. AgentFather's prompt
    carries emoji, so this is a live path (see migration 0010).
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
        logger.info("%s: renamed magic-link tools in %s", ID, config_path.parent.name)
