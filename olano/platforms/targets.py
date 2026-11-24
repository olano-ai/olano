"""Resolve symbolic outbound targets to real channel addresses.

Every outbound tool defaults its ``target`` to the literal string ``"owner"``
(``notification_send_photo``, ``notification_send_text``, … and the MCP OAuth
watcher). Nothing used to translate that sentinel, so it was handed straight to
the adapter, where Telegram's ``int(chat_id)`` raised ``ValueError: invalid
literal for int() with base 10: 'owner'``. ``ChannelRegistry.send`` swallowed
the exception and returned ``None``, which the caller reported as "no telegram
delivery for agent X" — so an agent asked to send an image concluded its runtime
had no Telegram configured and gave up, while the real chat sat one resolution
step away.

The sentinel is resolved here, at the single delivery choke point, in the order
a human means it:

1. **The chat this turn came from** — "send it here". Taken from the active
   request context when it belongs to the same channel type.
2. **The channel's configured chat** — ``default_chat_id`` / ``chat_id`` /
   ``channel_id`` on the agent's channel entry (what PULSE delivery already
   uses).
3. **The first allow-listed user** — the owner, for the common single-owner
   Telegram/Discord bot.

If none resolve we raise :class:`TargetResolutionError` rather than passing the
sentinel down, so the caller reports *what to configure* instead of a
``ValueError`` about base-10 literals.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

#: Targets that mean "the agent's owner / this conversation" rather than an
#: address. Compared case-insensitively after stripping surrounding whitespace.
OWNER_SENTINELS = frozenset({"", "owner", "default", "me", "self", "here"})

#: Phone-number-ish: optional ``+``, then digits with common separators.
_PHONEISH = re.compile(r"^\+?\d[\d\s().-]{4,}$")

#: Channels whose adapters require a specific address shape — a name-shaped
#: target that the directory cannot resolve is guaranteed to fail at the
#: adapter (``int('Community Group')``, a garbage JID, …), so failing here
#: with candidates is strictly more useful than the adapter's crash.
_STRICT_ADDRESS_CHANNELS = frozenset(
    {"telegram", "discord", "whatsapp", "slack", "signal", "matrix"}
)

#: Of those, the channels whose adapters address a chat by an **integer** id
#: (``int(chat_id)`` in ``telegram.py`` / ``discord.py``). Their entry in
#: :func:`looks_like_address` is therefore a *complete* grammar, not a
#: heuristic: a target it rejects is never an unconventional-but-valid id the
#: adapter might still understand, so nothing name-shaped may pass through to
#: them. Every other channel keeps the lenient pass-through — Slack takes
#: ``#channel-name``, Mattermost takes slugs, and a custom adapter may take
#: anything.
_INT_ADDRESSED_CHANNELS = frozenset({"telegram", "discord"})

#: Channel-config keys that hold an explicit outbound chat address, in the
#: precedence PULSE delivery already applies (``pulse/delivery.py``).
_TARGET_KEYS = ("default_chat_id", "chat_id", "channel_id", "default_channel")


class TargetResolutionError(RuntimeError):
    """A symbolic target could not be resolved to a real channel address."""


def is_owner_sentinel(target: str | None) -> bool:
    """Whether *target* is a placeholder rather than a real address."""
    return str(target or "").strip().lower() in OWNER_SENTINELS


def normalize_channel_type(channel_type: str) -> str:
    """``"platform:telegram"`` / ``"Telegram"`` → ``"telegram"``.

    Channel entries may carry the ``platform:`` prefix that selects the newer
    adapter, but the registry keys and tool arguments use the bare name.
    """
    value = str(channel_type or "").strip().lower()
    return value.split(":", 1)[1] if value.startswith("platform:") else value


def _from_request_context(channel_type: str) -> str:
    """The chat the current turn arrived on, when it is on this channel."""
    try:
        from olano.security.request_context import get_request_context

        ctx = get_request_context()
    except Exception:  # pragma: no cover - defensive; context is optional
        return ""
    if normalize_channel_type(ctx.channel) != channel_type:
        return ""
    metadata = ctx.metadata or {}
    chat_id = str(metadata.get("chat_id") or "").strip()
    if chat_id:
        return chat_id
    # Fall back to the platform sender: in a DM the chat id *is* the user id,
    # which is the overwhelmingly common shape for an owner notification.
    if not ctx.is_group:
        return str(metadata.get("platform_user_id") or "").strip()
    return ""


def _channel_entries(agent_name: str, channel_type: str) -> list[dict[str, Any]]:
    """This agent's channel-config entries for *channel_type*."""
    try:
        from olano.config.loader import load_agent_config

        config = load_agent_config(agent_name)
    except Exception as exc:
        logger.debug("Target resolution: no config for agent '%s': %s", agent_name, exc)
        return []
    entries: list[dict[str, Any]] = []
    for entry in config.channels or []:
        raw = entry.to_dict() if hasattr(entry, "to_dict") else {"type": str(entry)}
        if normalize_channel_type(raw.get("type") or str(entry)) == channel_type:
            entries.append(raw)
    return entries


def _scopes(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The entry itself plus its ``extra`` block.

    Adapter-specific settings land under ``extra`` for ``platform:*`` channels
    and at the top level for classic ones, so both levels are searched.
    """
    extra = entry.get("extra")
    return [entry, extra] if isinstance(extra, dict) else [entry]


def _from_channel_config(entries: list[dict[str, Any]]) -> str:
    for entry in entries:
        for scope in _scopes(entry):
            for key in _TARGET_KEYS:
                value = str(scope.get(key) or "").strip()
                if value:
                    return value
    return ""


def _from_allowed_users(entries: list[dict[str, Any]]) -> str:
    # ``owner_users`` designates the owner explicitly, so it outranks the
    # first-allow-listed-entry convention below.
    for key in ("owner_users", "allowed_users"):
        for entry in entries:
            for scope in _scopes(entry):
                raw = scope.get(key)
                if raw is None:
                    continue
                if isinstance(raw, str | int):
                    raw = [raw]
                try:
                    candidates = [str(u).strip() for u in raw if str(u).strip()]
                except TypeError:
                    continue
                if candidates:
                    # Preserve config order — the first entry is the owner by
                    # convention. ``@handle`` forms are skipped: adapters
                    # address chats by id, and a handle is not a chat id.
                    for candidate in candidates:
                        if not candidate.startswith("@"):
                            return candidate
    return ""


def looks_like_address(channel_type: str, target: str) -> bool:
    """Whether *target* is shaped like a real address on this channel.

    This is the gate that keeps cold sends working: anything address-shaped
    passes through delivery untouched — being unknown to the chat directory is
    NEVER a reason to block a send, because several platforms (WhatsApp/SMS by
    phone number, email, Matrix/Signal ids) can message users who have never
    messaged the bot. Only clearly name-shaped strings go to the directory.
    """
    t = str(target or "").strip()
    if not t:
        return False
    # Phone numbers legitimately contain spaces ("+49 170 1234567"); any other
    # spaced string is a display name, not an address.
    phoneish = bool(_PHONEISH.fullmatch(t))
    if " " in t and not phoneish:
        return False
    channel = normalize_channel_type(channel_type)
    if channel == "telegram":
        # Numeric ids (groups are negative), optionally with a forum-topic
        # thread suffix ("<chat_id>:<thread_id>"), plus the "biz:<conn>:<id>"
        # business-connection reference the adapter itself produces.
        if t.startswith("biz:"):
            t = t.rsplit(":", 1)[-1]
        return bool(re.fullmatch(r"-?\d+(:\d+)?", t))
    if channel == "whatsapp":
        # JIDs (…@s.whatsapp.net / …@g.us / …@lid) or a bare phone number.
        # A leading "@" is a handle, not a JID — JIDs never start with one.
        return "@" in t[1:] or phoneish
    if channel == "discord":
        return t.isdigit()
    if channel == "slack":
        # Workspace ids: C…/G… channels, U…/W… users, D… DMs.
        return bool(re.fullmatch(r"[CUGDW][A-Z0-9]{5,}", t))
    if channel == "matrix":
        # !room:server, @user:server, #alias:server.
        return t[0] in "!@" or (t[0] == "#" and ":" in t)
    if channel == "signal":
        return t.startswith("group:") or phoneish
    if channel in ("email", "sms", "bluebubbles", "imessage"):
        return "@" in t[1:] or phoneish
    # Unknown/lenient platforms: with no shape contract to check against,
    # treat everything as a possible address. resolve_target still consults
    # the directory for these, but a miss passes through instead of raising.
    return True


def _from_directory(agent_name: str, channel: str, target: str) -> tuple[str, list[str]]:
    """Resolve a name-shaped target via the chat directory.

    Returns ``(chat_id, [])`` on a unique match, ``("", candidates)``
    otherwise — where *candidates* are human-readable "name (id=…)" strings
    for the error message.
    """
    if not agent_name:
        return "", []
    try:
        from olano.platforms.chat_directory import get_chat_directory

        directory = get_chat_directory()
    except Exception:  # pragma: no cover - directory is best-effort
        return "", []
    query = str(target or "").strip().lstrip("@")
    resolved, matches = directory.resolve_chat_id(agent_name, channel, query)
    if resolved:
        return resolved, []
    candidates = [
        f'"{(m.get("chat_name") or "").strip() or m.get("chat_id")}" (id={m.get("chat_id")})'
        for m in matches[:5]
    ]
    if not candidates:
        # No chat by that name — maybe it names a person (DM target).
        users = directory.find_users(agent_name, query, platform=channel, limit=5)
        exact = [
            u
            for u in users
            if query.lower()
            in (
                (u.get("username") or "").lower(),
                (u.get("display_name") or "").lower(),
            )
        ]
        pool = exact or users
        if len(pool) == 1:
            return str(pool[0]["user_id"]), []
        candidates = [
            f"@{u.get('username') or u.get('display_name')} (id={u.get('user_id')})"
            for u in pool[:5]
        ]
    return "", candidates


def resolve_target(agent_name: str, channel_type: str, target: str) -> str:
    """Resolve *target* to a real channel address for ``(agent, channel)``.

    Address-shaped targets pass through untouched (see
    :func:`looks_like_address` — a send to an id the bot has never seen must
    keep working). ``"owner"``-style sentinels resolve to the current/default
    chat. Name-shaped targets ("Community Group", "@marketing") resolve
    through the chat directory when the match is unambiguous. On a miss, only
    a target that could not possibly be an address — it contains whitespace,
    or it is an ``@handle`` left over after directory resolution failed —
    fails on strict-address channels; every other unresolved token passes
    through and lets the adapter decide, so an unconventional but valid id is
    never blocked by this layer.
    """
    if not is_owner_sentinel(target):
        channel = normalize_channel_type(channel_type)
        if looks_like_address(channel, target):
            return target
        resolved, candidates = _from_directory(agent_name, channel, target)
        if resolved:
            logger.debug(
                "Target resolution: '%s' → '%s' (chat directory) for agent '%s' on '%s'",
                target,
                resolved,
                agent_name,
                channel,
            )
            return resolved
        if len(candidates) > 1:
            raise TargetResolutionError(
                f"target '{target}' is ambiguous on '{channel}' — matches: "
                f"{'; '.join(candidates)}. Pass the chat id as `target` "
                f"(chats_find shows ids)."
            )
        t = str(target).strip()
        if channel in _STRICT_ADDRESS_CHANNELS and (
            " " in t or t.startswith("@") or channel in _INT_ADDRESSED_CHANNELS
        ):
            # A spaced string or an unresolved @handle can never be a valid
            # address on these platforms (any valid @-form — a Matrix MXID, a
            # WhatsApp JID — already passed looks_like_address above), and on
            # an int-addressed channel neither can anything else non-numeric,
            # so this send is guaranteed to fail at the adapter — fail here
            # with something actionable instead of e.g. Telegram's
            # int('jrivera') ValueError. Only the "@" spelling used to be
            # caught, so the same handle written bare — which is how an agent
            # reads it back off a "[from @jrivera (…)]" label — fell through.
            hint = f" Closest match: {candidates[0]}." if candidates else ""
            raise TargetResolutionError(
                f"'{target}' is not a valid {channel} address and no known chat or "
                f"user has that name.{hint} The chat directory only knows chats and "
                f"users whose messages have already reached this agent, so someone "
                f"who has never messaged it must be addressed by their platform "
                f"chat id — ask them to message this bot first, or pass the id as "
                f"`target` (use chats_find/chats_list to look ids up by name)."
            )
        return target

    channel = normalize_channel_type(channel_type)
    resolved = _from_request_context(channel)
    if resolved:
        logger.debug(
            "Target resolution: '%s' → '%s' (current %s chat) for agent '%s'",
            target,
            resolved,
            channel,
            agent_name,
        )
        return resolved

    entries = _channel_entries(agent_name, channel)
    for source, value in (
        ("channel config", _from_channel_config(entries)),
        ("allowed_users", _from_allowed_users(entries)),
    ):
        if value:
            logger.debug(
                "Target resolution: '%s' → '%s' (%s) for agent '%s' on '%s'",
                target,
                value,
                source,
                agent_name,
                channel,
            )
            return value

    raise TargetResolutionError(
        f"cannot resolve target '{target}' for agent '{agent_name}' on '{channel}' — "
        f"pass an explicit chat id as `target`, or set `default_chat_id` (or "
        f"`allowed_users`) on the agent's '{channel}' channel entry"
    )
