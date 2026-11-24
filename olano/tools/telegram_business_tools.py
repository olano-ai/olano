"""Telegram Business + managed-bot tools (secretary / management mode).

Act through the agent's connected ``platform:telegram`` adapter, so they are
only functional when that connector is configured and running. Every
account-mutating tool is double-gated:

1. a connector toggle in ``config.extra`` (``business_manage_profile``,
   ``business_manage_gifts``, ``manage_bots``) — all default OFF, and
2. the matching right the owner granted when connecting the bot in
   Telegram Settings → Business → Chatbots (``can_edit_name`` etc.).

The business-connection registry itself lives on the adapter (see
``olano/platforms/telegram.py`` — secretary mode), persisted per agent.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def _current_agent_name() -> str:
    from olano.tools.talk_to import current_agent

    return current_agent.get() or ""


def _adapter() -> Any:
    """Resolve the running TelegramAdapter for the current agent, or None."""
    from olano.platforms import get_platform_registry

    agent = _current_agent_name()
    adapter = get_platform_registry().get(agent, "telegram")
    if adapter is None or not hasattr(adapter, "_business_connections"):
        return None
    return adapter


_NO_ADAPTER = (
    "No running platform:telegram connector for this agent — Telegram Business "
    "tools need the platform Telegram adapter connected."
)


def _run(coro_factory) -> Any:
    """Run an adapter coroutine from this sync tool context (adapter's loop)."""
    from olano.tools.notification_tools import _deliver_sync

    return _deliver_sync(coro_factory)


def _toggle_enabled(adapter: Any, key: str) -> bool:
    try:
        return bool(adapter._coerce_bool_extra(key, False))
    except Exception:
        return False


def _resolve_connection(adapter: Any, connection_id: str) -> Optional[dict[str, Any]]:
    """Return the requested (or the single enabled) business connection."""
    conns: dict[str, dict[str, Any]] = getattr(adapter, "_business_connections", {}) or {}
    if connection_id:
        return conns.get(connection_id)
    enabled = [c for c in conns.values() if c.get("is_enabled")]
    return enabled[0] if len(enabled) == 1 else None


def _rights_error(entry: dict[str, Any], right: str) -> Optional[str]:
    if not (entry.get("rights") or {}).get(right):
        return (
            f"The business connection does not grant the '{right}' right. "
            "The owner must re-connect the bot in Telegram Settings → Business → "
            "Chatbots with that permission enabled."
        )
    return None


@tool
def telegram_business_status() -> str:
    """List this agent's Telegram Business connections and granted rights.

    Shows, per connection: the owner it belongs to, whether it is enabled,
    and which permissions (reply, edit profile, view gifts/stars, …) the
    owner granted. Use this before any other telegram_business_* tool to see
    what is possible.
    """
    adapter = _adapter()
    if adapter is None:
        return _NO_ADAPTER
    conns = getattr(adapter, "_business_connections", {}) or {}
    if not conns:
        return (
            "No Telegram Business connections. The account owner can connect this "
            "bot in Telegram Settings → Business → Chatbots (requires the "
            "connector's business_mode toggle to be on)."
        )
    lines = []
    for entry in conns.values():
        granted = sorted(k for k, v in (entry.get("rights") or {}).items() if v)
        lines.append(
            f"- {entry.get('id')}: owner={entry.get('user_name') or entry.get('user_id')} "
            f"enabled={entry.get('is_enabled')} rights={', '.join(granted) or '(none)'}"
        )
    return "Telegram Business connections:\n" + "\n".join(lines)


@tool
def telegram_business_set_name(
    first_name: Annotated[str, "New first name for the business account"],
    last_name: Annotated[str, "New last name (optional, empty to clear)"] = "",
    connection_id: Annotated[str, "Business connection id (optional if only one)"] = "",
) -> str:
    """Change the connected Telegram Business account's profile name.

    Requires the connector's business_manage_profile toggle AND the
    can_edit_name right on the business connection.
    """
    adapter = _adapter()
    if adapter is None:
        return _NO_ADAPTER
    if not _toggle_enabled(adapter, "business_manage_profile"):
        return (
            "Profile management is disabled for this connector — enable the "
            "'business_manage_profile' toggle in the Telegram connector settings."
        )
    entry = _resolve_connection(adapter, connection_id)
    if entry is None:
        return "No (single) enabled business connection found — pass connection_id."
    if err := _rights_error(entry, "can_edit_name"):
        return err
    try:
        _run(
            lambda: adapter._bot.set_business_account_name(
                business_connection_id=entry["id"],
                first_name=first_name,
                last_name=last_name or None,
            )
        )
        return f"Business account name set to '{first_name} {last_name}'.".strip()
    except Exception as exc:
        return f"[tool-error] telegram_business_set_name: {exc}"


@tool
def telegram_business_set_bio(
    bio: Annotated[str, "New bio text (max 140 chars, empty to clear)"],
    connection_id: Annotated[str, "Business connection id (optional if only one)"] = "",
) -> str:
    """Change the connected Telegram Business account's bio.

    Requires the connector's business_manage_profile toggle AND the
    can_edit_bio right on the business connection.
    """
    adapter = _adapter()
    if adapter is None:
        return _NO_ADAPTER
    if not _toggle_enabled(adapter, "business_manage_profile"):
        return (
            "Profile management is disabled for this connector — enable the "
            "'business_manage_profile' toggle in the Telegram connector settings."
        )
    entry = _resolve_connection(adapter, connection_id)
    if entry is None:
        return "No (single) enabled business connection found — pass connection_id."
    if err := _rights_error(entry, "can_edit_bio"):
        return err
    try:
        _run(
            lambda: adapter._bot.set_business_account_bio(
                business_connection_id=entry["id"], bio=bio or None
            )
        )
        return "Business account bio updated."
    except Exception as exc:
        return f"[tool-error] telegram_business_set_bio: {exc}"


@tool
def telegram_business_get_star_balance(
    connection_id: Annotated[str, "Business connection id (optional if only one)"] = "",
) -> str:
    """Read the connected Telegram Business account's Stars balance.

    Requires the connector's business_manage_gifts toggle AND the
    can_view_gifts_and_stars right on the business connection.
    """
    adapter = _adapter()
    if adapter is None:
        return _NO_ADAPTER
    if not _toggle_enabled(adapter, "business_manage_gifts"):
        return (
            "Gift/Stars access is disabled for this connector — enable the "
            "'business_manage_gifts' toggle in the Telegram connector settings."
        )
    entry = _resolve_connection(adapter, connection_id)
    if entry is None:
        return "No (single) enabled business connection found — pass connection_id."
    if err := _rights_error(entry, "can_view_gifts_and_stars"):
        return err
    try:
        amount = _run(
            lambda: adapter._bot.get_business_account_star_balance(
                business_connection_id=entry["id"]
            )
        )
        return f"Stars balance: {getattr(amount, 'amount', amount)}"
    except Exception as exc:
        return f"[tool-error] telegram_business_get_star_balance: {exc}"


@tool
def telegram_business_get_gifts(
    connection_id: Annotated[str, "Business connection id (optional if only one)"] = "",
) -> str:
    """List gifts owned by the connected Telegram Business account.

    Requires the connector's business_manage_gifts toggle AND the
    can_view_gifts_and_stars right on the business connection.
    """
    adapter = _adapter()
    if adapter is None:
        return _NO_ADAPTER
    if not _toggle_enabled(adapter, "business_manage_gifts"):
        return (
            "Gift/Stars access is disabled for this connector — enable the "
            "'business_manage_gifts' toggle in the Telegram connector settings."
        )
    entry = _resolve_connection(adapter, connection_id)
    if entry is None:
        return "No (single) enabled business connection found — pass connection_id."
    if err := _rights_error(entry, "can_view_gifts_and_stars"):
        return err
    try:
        owned = _run(
            lambda: adapter._bot.get_business_account_gifts(business_connection_id=entry["id"])
        )
        gifts = list(getattr(owned, "gifts", []) or [])
        if not gifts:
            return "The business account owns no gifts."
        lines = [f"- {g.type}: {getattr(g, 'owned_gift_id', '?')}" for g in gifts[:50]]
        total = getattr(owned, "total_count", len(gifts))
        return f"Gifts ({total} total, showing {len(lines)}):\n" + "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] telegram_business_get_gifts: {exc}"


@tool
def telegram_managed_bot_get_access(
    bot_user_id: Annotated[int, "User id of the managed bot"],
) -> str:
    """Read the access settings of a bot this bot manages (management mode).

    Only works for bots whose owner made this bot their bot manager in
    @BotFather. Requires the connector's manage_bots toggle.
    """
    adapter = _adapter()
    if adapter is None:
        return _NO_ADAPTER
    if not _toggle_enabled(adapter, "manage_bots"):
        return (
            "Managed-bot administration is disabled for this connector — enable "
            "the 'manage_bots' toggle in the Telegram connector settings."
        )
    try:
        settings = _run(lambda: adapter._bot.get_managed_bot_access_settings(int(bot_user_id)))
        added = [str(getattr(u, "id", "?")) for u in (getattr(settings, "added_users", None) or [])]
        return (
            f"Managed bot {bot_user_id}: "
            f"is_access_restricted={getattr(settings, 'is_access_restricted', None)}, "
            f"added_users={', '.join(added) or '(none)'}"
        )
    except Exception as exc:
        return f"[tool-error] telegram_managed_bot_get_access: {exc}"


@tool
def telegram_managed_bot_set_access(
    bot_user_id: Annotated[int, "User id of the managed bot"],
    is_access_restricted: Annotated[bool, "True → only selected users can access the bot"],
    allowed_user_ids: Annotated[
        str, "Comma-separated user ids granted access when restricted (optional)"
    ] = "",
) -> str:
    """Change the access settings of a bot this bot manages (management mode).

    Only works for bots whose owner made this bot their bot manager in
    @BotFather. Requires the connector's manage_bots toggle.
    """
    adapter = _adapter()
    if adapter is None:
        return _NO_ADAPTER
    if not _toggle_enabled(adapter, "manage_bots"):
        return (
            "Managed-bot administration is disabled for this connector — enable "
            "the 'manage_bots' toggle in the Telegram connector settings."
        )
    try:
        ids = [int(x.strip()) for x in allowed_user_ids.split(",") if x.strip()]
        ok = _run(
            lambda: adapter._bot.set_managed_bot_access_settings(
                int(bot_user_id),
                bool(is_access_restricted),
                added_user_ids=ids or None,
            )
        )
        return (
            f"Managed bot {bot_user_id} access updated "
            f"(restricted={is_access_restricted}, allowed={ids or 'unchanged'})."
            if ok
            else "Telegram declined the access-settings update."
        )
    except Exception as exc:
        return f"[tool-error] telegram_managed_bot_set_access: {exc}"


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
