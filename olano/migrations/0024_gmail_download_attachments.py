"""Grant ``gmail_download_attachments`` to agents that already read Gmail.

Gmail could list a message's body and headers but not save its attachments,
so a PDF confirmation or a spreadsheet that arrived by email never became a
file the agent could open. ``gmail_download_attachments`` closes that gap.

``self_add_toolkit`` / ``olano_add_toolkit`` store the Google toolkit as its
expanded member names, so an agent that added it before this release lists the
old Gmail tools and would never see the new one. An agent whose ``tools`` names
the toolkit itself (``"google"``) resolves it at boot and needs nothing.

"Reads Gmail" means the agent lists ``gmail_get_message``, the tool the new one
follows (it is what names the attachments). The new name lands right after it.
Idempotent, respects ``disabled_tools`` opt-outs, and rewrites serialize as
plain JSON (comments dropped only on files that actually change), with the
surrogate repair and the ``isinstance(data, dict)`` guard the earlier grant
migrations carry.

The tool writes only into the agent's own workspace, so it sits in no HITL
category, like ``drive_download`` and ``ms365_download_attachment``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0024_gmail_download_attachments"

_ANCHOR = "gmail_get_message"
_NEW_TOOL = "gmail_download_attachments"


def _repair_surrogates(text: str) -> str:
    """Recombine UTF-16 surrogate pairs into the characters they encode (frozen
    copy of the helper the earlier grant migrations carry)."""
    try:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    except Exception:
        return text


def _grant(tools: list, disabled: set) -> bool:
    """Insert the new tool after ``gmail_get_message``; return whether it changed."""
    if _ANCHOR not in tools or _NEW_TOOL in tools or _NEW_TOOL in disabled:
        return False
    tools.insert(tools.index(_ANCHOR) + 1, _NEW_TOOL)
    return True


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
        tools = data.get("tools")
        if not isinstance(tools, list):
            continue
        disabled = set(data.get("disabled_tools") or [])
        if not _grant(tools, disabled):
            continue
        data["tools"] = tools
        payload = _repair_surrogates(json.dumps(data, indent=2, ensure_ascii=False)) + "\n"
        write_agent_config_text(config_path, payload)
        logger.info("%s: granted %s to %s", ID, _NEW_TOOL, config_path.parent.name)
