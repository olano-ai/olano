"""Grant the expanded Microsoft 365 toolkit to agents that already hold it.

The ``microsoft365`` toolkit grew from twelve tools to fifty-one (drafts,
replies, forwards, mail folders and attachments; meeting invitations,
availability and responses; people lookup, contacts, automatic replies;
OneDrive/SharePoint download, sharing and search; Excel; Teams chats; To Do).
``self_add_toolkit`` / ``olano_add_toolkit`` store a toolkit as its expanded
member names, so an agent that added Microsoft 365 before this release lists
only the original twelve and would never see the rest. An agent whose
``tools`` names the toolkit itself (``"microsoft365"``) resolves it at boot and
needs nothing.

"Holds the toolkit" means every one of the ten original tools the
Microsoft365Agent subagent carried is listed (the add-toolkit expansion lists
all twelve; the decode helper and ``ms365_delete_event`` are the two an owner
might reasonably have dropped). An agent holding a hand-picked subset is left
alone: it chose those tools, not the toolkit.

New names land right after the agent's last ``ms365_*`` entry, in toolkit
order, so the list stays grouped. Idempotent per tool, respects
``disabled_tools`` opt-outs, and rewrites serialize as plain JSON (comments
dropped only on files that actually change), with the surrogate repair and the
``isinstance(data, dict)`` guard the earlier grant migrations carry.

Every sending, inviting, sharing or deleting tool among them sits in HITL's
``write_external`` category, so under the ``safe`` and ``production`` profiles
each one still asks before it acts.

The lists are frozen: a migration is a historical record, never regenerated
from live code.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0022_microsoft365_toolkit_tools"

# The Microsoft365Agent subagent's default_tools before this release.
_ORIGINAL_CORE: frozenset[str] = frozenset(
    {
        "ms365_list_messages",
        "ms365_send_mail",
        "ms365_list_events",
        "ms365_create_event",
        "ms365_list_drive_items",
        "ms365_search_drive",
        "ms365_upload_drive_file",
        "ms365_list_teams",
        "ms365_list_channels",
        "ms365_send_channel_message",
    }
)

# Added in this release, in toolkit order.
_NEW_TOOLS: tuple[str, ...] = (
    "ms365_get_message",
    "ms365_create_draft",
    "ms365_update_draft",
    "ms365_send_draft",
    "ms365_reply",
    "ms365_forward",
    "ms365_update_message",
    "ms365_move_message",
    "ms365_list_mail_folders",
    "ms365_download_attachment",
    "ms365_get_event",
    "ms365_update_event",
    "ms365_respond_to_event",
    "ms365_find_meeting_times",
    "ms365_get_availability",
    "ms365_list_calendars",
    "ms365_find_people",
    "ms365_get_profile",
    "ms365_set_auto_reply",
    "ms365_list_contacts",
    "ms365_create_contact",
    "ms365_search_files",
    "ms365_download_drive_file",
    "ms365_create_folder",
    "ms365_share_drive_item",
    "ms365_move_drive_item",
    "ms365_delete_drive_item",
    "ms365_find_sites",
    "ms365_excel_list_sheets",
    "ms365_excel_read_range",
    "ms365_excel_write_range",
    "ms365_excel_add_rows",
    "ms365_list_chats",
    "ms365_read_chat",
    "ms365_send_chat_message",
    "ms365_list_task_lists",
    "ms365_list_tasks",
    "ms365_create_task",
    "ms365_update_task",
)


def _repair_surrogates(text: str) -> str:
    """Recombine UTF-16 surrogate pairs into the characters they encode (frozen
    copy of the helper 0010/0013/0015/0019/0021 carry)."""
    try:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    except Exception:
        return text


def _grant(tools: list, disabled: set) -> list[str]:
    """Insert the missing new tools after the last ``ms365_*`` entry; return them."""
    if not _ORIGINAL_CORE.issubset({t for t in tools if isinstance(t, str)}):
        return []
    missing = [t for t in _NEW_TOOLS if t not in tools and t not in disabled]
    if not missing:
        return []
    last = max(i for i, t in enumerate(tools) if isinstance(t, str) and t.startswith("ms365_"))
    tools[last + 1 : last + 1] = missing
    return missing


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
        added = _grant(tools, disabled)
        if not added:
            continue
        data["tools"] = tools
        payload = _repair_surrogates(json.dumps(data, indent=2, ensure_ascii=False)) + "\n"
        write_agent_config_text(config_path, payload)
        logger.info(
            "%s: granted %d Microsoft 365 tools to %s", ID, len(added), config_path.parent.name
        )
