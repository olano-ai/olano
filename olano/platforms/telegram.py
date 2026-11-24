"""
Telegram platform adapter.

Uses python-telegram-bot library for:
- Receiving messages from users/groups
- Sending responses back
- Handling media and commands
"""

import asyncio
import html as _html
import json
import logging
import os
import re
import time
from collections import deque
from collections.abc import Callable
from typing import Any, Optional, cast

logger = logging.getLogger(__name__)

try:
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update

    try:
        from telegram import LinkPreviewOptions
    except ImportError:
        LinkPreviewOptions = None  # type: ignore[misc,assignment]  # optional library symbol
    from telegram.constants import ChatType, ParseMode
    from telegram.ext import (
        Application,
        BusinessConnectionHandler,
        BusinessMessagesDeletedHandler,
        CallbackQueryHandler,
        ChatMemberHandler,
        CommandHandler,
        ContextTypes,
        PreCheckoutQueryHandler,
        filters,
    )
    from telegram.ext import (
        MessageHandler as TelegramMessageHandler,
    )
    from telegram.request import HTTPXRequest

    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    Update = Any  # type: ignore[misc,assignment]  # library-unavailable fallback
    Bot = Any  # type: ignore[misc,assignment]  # library-unavailable fallback
    Message = Any  # type: ignore[misc,assignment]  # library-unavailable fallback
    InlineKeyboardButton = Any  # type: ignore[misc,assignment]  # library-unavailable fallback
    InlineKeyboardMarkup = Any  # type: ignore[misc,assignment]  # library-unavailable fallback
    LinkPreviewOptions = None  # type: ignore[misc,assignment]  # library-unavailable fallback
    Application = Any  # type: ignore[misc,assignment]  # library-unavailable fallback
    CommandHandler = Any  # type: ignore[misc,assignment]  # library-unavailable fallback
    CallbackQueryHandler = Any  # type: ignore[misc,assignment]  # library-unavailable fallback
    PreCheckoutQueryHandler = Any  # type: ignore[misc,assignment]  # library-unavailable fallback
    BusinessConnectionHandler = Any  # type: ignore[misc,assignment]  # library-unavailable fallback
    BusinessMessagesDeletedHandler = Any  # type: ignore[misc,assignment]  # library-unavailable fallback
    ChatMemberHandler = Any  # type: ignore[misc,assignment]  # library-unavailable fallback
    TelegramMessageHandler = Any  # type: ignore[misc,assignment]  # library-unavailable fallback
    HTTPXRequest = Any  # type: ignore[misc,assignment]  # library-unavailable fallback
    filters = None  # type: ignore[assignment]  # library-unavailable fallback
    ParseMode = None  # type: ignore[misc,assignment]  # library-unavailable fallback
    ChatType = None  # type: ignore[misc,assignment]  # library-unavailable fallback

    # Mock ContextTypes so type annotations using ContextTypes.DEFAULT_TYPE
    # don't crash during class definition when the library isn't installed.
    class _MockContextTypes:
        DEFAULT_TYPE = Any

    ContextTypes = _MockContextTypes  # type: ignore[misc,assignment]  # library-unavailable fallback

import sys
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from olano.platforms.base import (
    SUPPORTED_DOCUMENT_TYPES,
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    _prefix_within_utf16_limit,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    resolve_proxy_url,
    utf16_len,
)
from olano.platforms.config import Platform, PlatformConfig
from olano.platforms.telegram_network import (
    TelegramFallbackTransport,
    discover_fallback_ips,
    parse_fallback_ip_env,
)


def check_telegram_requirements() -> bool:
    """Check if Telegram dependencies are available."""
    return TELEGRAM_AVAILABLE


def _dt_to_epoch(value: Any) -> Optional[float]:
    """Normalize Telegram's ``subscription_expiration_date`` to Unix seconds.

    python-telegram-bot hands this back as a timezone-aware ``datetime``;
    older or raw payloads may carry a plain int. ``None`` stays ``None``.
    """
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value.timestamp())
    except Exception:
        return None


# Matches every character that MarkdownV2 requires to be backslash-escaped
# when it appears outside a code span or fenced code block.
_MDV2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#\+\-=|{}.!\\])")


def _escape_mdv2(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters with a preceding backslash."""
    return _MDV2_ESCAPE_RE.sub(r"\\\1", text)


def _strip_mdv2(text: str) -> str:
    """Strip MarkdownV2 escape backslashes to produce clean plain text.

    Also removes MarkdownV2 formatting markers so the fallback
    doesn't show stray syntax characters from format_message conversion.
    """
    # Remove escape backslashes before special characters
    cleaned = re.sub(r"\\([_*\[\]()~`>#\+\-=|{}.!\\])", r"\1", text)
    # Remove MarkdownV2 bold markers that format_message converted from **bold**
    cleaned = re.sub(r"\*([^*]+)\*", r"\1", cleaned)
    # Remove MarkdownV2 italic markers that format_message converted from *italic*
    # Use word boundary (\b) to avoid breaking snake_case like my_variable_name
    cleaned = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", cleaned)
    # Remove MarkdownV2 strikethrough markers (~text~ → text)
    cleaned = re.sub(r"~([^~]+)~", r"\1", cleaned)
    # Remove MarkdownV2 spoiler markers (||text|| → text)
    cleaned = re.sub(r"\|\|([^|]+)\|\|", r"\1", cleaned)
    return cleaned


# ---------------------------------------------------------------------------
# Markdown table → code block conversion
# ---------------------------------------------------------------------------
# Telegram's MarkdownV2 has no table syntax — '|' is just an escaped literal,
# so pipe tables render as noisy backslash-pipe text with no alignment.
# Wrapping the table in a fenced code block makes Telegram render it as
# monospace preformatted text with columns intact.

# Matches a GFM table delimiter row: optional outer pipes, cells containing
# only dashes (with optional leading/trailing colons for alignment) separated
# by '|'.  Requires at least one internal '|' so lone '---' horizontal rules
# are NOT matched.
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*){1,}\|?\s*$")


def _is_table_row(line: str) -> bool:
    """Return True if *line* could plausibly be a table data row."""
    stripped = line.strip()
    return bool(stripped) and "|" in stripped


def _wrap_markdown_tables(text: str) -> str:
    """Wrap GFM-style pipe tables in ``` fences so Telegram renders them.

    Detected by a row containing '|' immediately followed by a delimiter
    row matching :data:`_TABLE_SEPARATOR_RE`.  Subsequent pipe-containing
    non-blank lines are consumed as the table body and included in the
    wrapped block.  Tables inside existing fenced code blocks are left
    alone.
    """
    if "|" not in text or "-" not in text:
        return text

    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()

        # Track existing fenced code blocks — never touch content inside.
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if in_fence:
            out.append(line)
            i += 1
            continue

        # Look for a header row (contains '|') immediately followed by a
        # delimiter row.
        if "|" in line and i + 1 < len(lines) and _TABLE_SEPARATOR_RE.match(lines[i + 1]):
            table_block = [line, lines[i + 1]]
            j = i + 2
            while j < len(lines) and _is_table_row(lines[j]):
                table_block.append(lines[j])
                j += 1
            out.append("```")
            out.extend(table_block)
            out.append("```")
            i = j
            continue

        out.append(line)
        i += 1

    return "\n".join(out)


class TelegramAdapter(BasePlatformAdapter):
    """
    Telegram bot adapter.

    Handles:
    - Receiving messages from users and groups
    - Sending responses with Telegram markdown
    - Forum topics (thread_id support)
    - Media messages
    """

    # Telegram message limits
    MAX_MESSAGE_LENGTH = 4096
    # Threshold for detecting Telegram client-side message splits.
    # When a chunk is near this limit, a continuation is almost certain.
    _SPLIT_THRESHOLD = 4000
    MEDIA_GROUP_WAIT_SECONDS = 0.8
    _GENERAL_TOPIC_THREAD_ID = "1"
    # Telegram edits messages in place, so a long turn's progress can live in
    # one bubble — the same one the "💭 Thinking…" placeholder already
    # occupies (see _adopt_progress_message).
    PROGRESS_MODE = "edit"

    def __init__(self, config: PlatformConfig, agent_name: str = ""):
        super().__init__(config, Platform.TELEGRAM, agent_name=agent_name)
        self._app: Optional[Application] = None
        self._bot: Optional[Bot] = None
        self._webhook_mode: bool = False
        # Last menu published via setMyCommands, so a re-publish on an unchanged
        # config costs nothing (the Commands tab re-publishes on every save).
        self._published_menu: Optional[list[tuple[str, str]]] = None
        self._mention_patterns = self._compile_mention_patterns()
        self._reply_to_mode: str = getattr(config, "reply_to_mode", "first") or "first"
        self._disable_link_previews: bool = self._coerce_bool_extra("disable_link_previews", False)
        # Buffer rapid/album photo updates so Telegram image bursts are handled
        # as a single MessageEvent instead of self-interrupting multiple turns.
        self._media_batch_delay_seconds = float(
            os.getenv("LIONCLAW_TELEGRAM_MEDIA_BATCH_DELAY_SECONDS", "0.8")
        )
        self._pending_photo_batches: dict[str, MessageEvent] = {}
        self._pending_photo_batch_tasks: dict[str, asyncio.Task] = {}
        self._media_group_events: dict[str, MessageEvent] = {}
        self._media_group_tasks: dict[str, asyncio.Task] = {}
        # Buffer rapid text messages so Telegram client-side splits of long
        # messages are aggregated into a single MessageEvent.
        self._text_batch_delay_seconds = float(
            os.getenv("LIONCLAW_TELEGRAM_TEXT_BATCH_DELAY_SECONDS", "0.6")
        )
        self._text_batch_split_delay_seconds = float(
            os.getenv("LIONCLAW_TELEGRAM_TEXT_BATCH_SPLIT_DELAY_SECONDS", "2.0")
        )
        self._pending_text_batches: dict[str, MessageEvent] = {}
        self._pending_text_batch_tasks: dict[str, asyncio.Task] = {}
        self._polling_error_task: Optional[asyncio.Task] = None
        self._polling_conflict_count: int = 0
        self._polling_network_error_count: int = 0
        self._polling_error_callback_ref: Optional[Callable[[Exception], None]] = None
        # DM Topics: map of topic_name -> message_thread_id (populated at startup)
        self._dm_topics: dict[str, int] = {}
        # DM Topics config from extra.dm_topics
        self._dm_topics_config: list[dict[str, Any]] = self.config.extra.get("dm_topics", [])
        # "Thinking…" placeholder shown while the agent works on a reply,
        # edited in place with the final response when it's ready.
        # Disable with extra.thinking_message: false; customize the text
        # with extra.thinking_message_text.
        self._thinking_message_enabled: bool = self._coerce_bool_extra("thinking_message", True)
        _thinking_text = (
            self.config.extra.get("thinking_message_text")
            if getattr(self.config, "extra", None)
            else None
        )
        self._thinking_message_text: str = str(_thinking_text or "Thinking…").strip() or "Thinking…"
        # (chat_id, triggering message_id) key → placeholder message_id.
        # Keyed per triggering message so concurrent turns in one group
        # chat each get their own bubble.
        self._thinking_placeholders: dict[str, int] = {}
        # Interactive model picker state per chat
        self._model_picker_state: dict[str, dict] = {}
        # Approval button state: message_id → session_key
        self._approval_state: dict[int, str] = {}
        # Inbound sender allowlist from config.extra.allowed_users.
        # Entries can be numeric Telegram user IDs (int or numeric string)
        # or usernames (with or without leading "@"). Empty/missing → open bot.
        self._allowed_ids: set[str] = set()
        self._allowed_usernames: set[str] = set()
        self._parse_user_entries(
            self.config.extra.get("allowed_users"), self._allowed_ids, self._allowed_usernames
        )
        # Owner designation from config.extra.owner_users — trust WITHOUT
        # admission. Owners get owner treatment (runtime/server.py unions this
        # list into sender trust) and clear the gated-callback check below,
        # but the list never closes the bot: admission stays governed by
        # allowed_users alone, so an open bot can have a designated owner.
        # Owners are implicitly admitted when an allowlist IS configured —
        # designating an owner must never lock that owner out.
        self._owner_ids: set[str] = set()
        self._owner_usernames: set[str] = set()
        self._parse_user_entries(
            self.config.extra.get("owner_users"), self._owner_ids, self._owner_usernames
        )

        # ── Telegram Business ("secretary mode") state ────────────────
        # Connections the owner granted via Telegram Settings → Business →
        # Chatbots, keyed by business_connection_id. Persisted so replies
        # keep working across restarts without waiting for a fresh
        # business_connection update.
        self._business_connections: dict[str, dict[str, Any]] = {}
        self._business_auto_reply: bool = self._coerce_bool_extra("business_auto_reply", False)
        # Off by default — see _send_thinking_placeholder.
        self._business_thinking_message: bool = self._coerce_bool_extra(
            "business_thinking_message", False
        )
        # Seconds of complete silence before a secretary reply starts — see
        # quiet_reply_delay. On by default (30s): the whole point of secretary
        # mode is that the customer sees the account owner answering, and a
        # reply that lands two seconds after every message never reads as one.
        self._business_reply_delay: float = self._coerce_float_extra(
            "business_reply_delay_seconds",
            self._env_float("LIONCLAW_TELEGRAM_BUSINESS_REPLY_DELAY", 30.0),
            minimum=0.0,
        )
        # Optional customer allowlist for business chats. The regular
        # allowed_users list intentionally does NOT apply there — it names
        # the bot's operators, while business chats are the owner's own
        # customer conversations (chat selection primarily happens in the
        # owner's Telegram Business settings).
        self._biz_allowed_ids: set[str] = set()
        self._biz_allowed_usernames: set[str] = set()
        self._parse_user_entries(
            self.config.extra.get("business_allowed_customers"),
            self._biz_allowed_ids,
            self._biz_allowed_usernames,
        )
        if self._business_mode_enabled():
            self._load_business_connections()

        # ── Bot-to-bot (agent-to-agent) messaging ─────────────────────
        # Off by default: messages authored by other bots are dropped.
        # Telegram only delivers them at all when Bot-to-Bot Communication
        # Mode is also enabled for this bot in @BotFather. The loop guards
        # exist because two agents pointed at each other will otherwise
        # ping-pong forever.
        self._allow_bot_messages: bool = self._coerce_bool_extra("allow_bot_messages", False)
        self._allowed_bot_usernames: set[str] = {
            str(u).strip().lstrip("@").lower()
            for u in (self.config.extra.get("allowed_bot_usernames") or [])
            if str(u).strip()
        }
        try:
            self._bot_loop_max_turns = int(self.config.extra.get("bot_loop_max_turns", 6) or 6)
        except (TypeError, ValueError):
            self._bot_loop_max_turns = 6
        try:
            self._bot_rate_limit_per_minute = int(
                self.config.extra.get("bot_rate_limit_per_minute", 10) or 10
            )
        except (TypeError, ValueError):
            self._bot_rate_limit_per_minute = 10
        # chat_id → consecutive accepted bot-authored messages (reset by
        # any human message in the chat)
        self._bot_turn_counts: dict[str, int] = {}
        # chat_id → monotonic timestamps of recently accepted bot messages
        self._bot_msg_times: dict[str, deque[float]] = {}

    @staticmethod
    def _parse_user_entries(raw: Any, ids: set[str], usernames: set[str]) -> None:
        """Split a config user list into numeric-id and username sets.

        Entries can be numeric Telegram user IDs (int or numeric string) or
        usernames (with or without a leading ``@``, matched case-insensitively).
        A bare scalar is treated as a one-entry list.
        """
        if isinstance(raw, str | int):
            raw = [raw]
        for entry in raw or []:
            if isinstance(entry, int):
                ids.add(str(entry))
                continue
            s = str(entry).strip().lstrip("@")
            if not s:
                continue
            if s.isdigit():
                ids.add(s)
            else:
                usernames.add(s.lower())

    def _sender_is_allowed(self, message: Message) -> bool:
        """Return True when the message sender passes the allowed_users allowlist.

        Open bot when no allowlist is configured — ``owner_users`` alone never
        closes the bot (it designates owners, it does not gate admission).
        With an allowlist configured, the sender's numeric ID or username must
        match it or the owner list: designating an owner must never lock that
        owner out.
        """
        if not self._allowed_ids and not self._allowed_usernames:
            return True
        user = getattr(message, "from_user", None)
        if user is None:
            return False
        uid = str(getattr(user, "id", ""))
        if uid in self._allowed_ids or uid in self._owner_ids:
            return True
        uname = (getattr(user, "username", "") or "").lower()
        return bool(uname) and (uname in self._allowed_usernames or uname in self._owner_usernames)

    def _is_callback_user_authorized(self, user_id: str) -> bool:
        """Return whether a Telegram inline-button caller may perform gated actions.

        Honors ``config.extra.owner_users`` / ``config.extra.allowed_users``
        and the legacy ``TELEGRAM_ALLOWED_USERS`` env var. When any of them is
        configured, gated buttons (exec approvals, update prompts) are theirs
        alone — on an open bot with only ``owner_users`` set, that means
        owner-only. When nothing is configured the bot is open. Only numeric
        IDs are honored here: a callback must not be approvable by username,
        which the account owner can change at will.
        """
        if self._allowed_ids or self._allowed_usernames or self._owner_ids or self._owner_usernames:
            return str(user_id) in self._allowed_ids or str(user_id) in self._owner_ids
        allowed_csv = os.getenv("TELEGRAM_ALLOWED_USERS", "").strip()
        if not allowed_csv:
            return True
        allowed_ids = {uid.strip() for uid in allowed_csv.split(",") if uid.strip()}
        return "*" in allowed_ids or user_id in allowed_ids

    @classmethod
    def _metadata_thread_id(cls, metadata: Optional[dict[str, Any]]) -> Optional[str]:
        if not metadata:
            return None
        thread_id = metadata.get("thread_id") or metadata.get("message_thread_id")
        return str(thread_id) if thread_id is not None else None

    @classmethod
    def _message_thread_id_for_send(cls, thread_id: Optional[str]) -> Optional[int]:
        if not thread_id or str(thread_id) == cls._GENERAL_TOPIC_THREAD_ID:
            return None
        return int(thread_id)

    @classmethod
    def _message_thread_id_for_typing(cls, thread_id: Optional[str]) -> Optional[int]:
        if not thread_id:
            return None
        return int(thread_id)

    @staticmethod
    def _is_thread_not_found_error(error: Exception) -> bool:
        return "thread not found" in str(error).lower()

    def _fallback_ips(self) -> list[str]:
        """Return validated fallback IPs from config (populated by _apply_env_overrides)."""
        configured = (
            self.config.extra.get("fallback_ips", []) if getattr(self.config, "extra", None) else []
        )
        if isinstance(configured, str):
            configured = configured.split(",")
        return parse_fallback_ip_env(",".join(str(v) for v in configured) if configured else None)

    @staticmethod
    def _looks_like_polling_conflict(error: Exception) -> bool:
        text = str(error).lower()
        return (
            error.__class__.__name__.lower() == "conflict"
            or "terminated by other getupdates request" in text
            or "another bot instance is running" in text
        )

    @staticmethod
    def _looks_like_network_error(error: Exception) -> bool:
        """Return True for transient network errors that warrant a reconnect attempt."""
        name = error.__class__.__name__.lower()
        if name in ("networkerror", "timedout", "connectionerror"):
            return True
        try:
            from telegram.error import NetworkError, TimedOut

            if isinstance(error, NetworkError | TimedOut):
                return True
        except ImportError:
            pass
        return isinstance(error, OSError)

    # ── Inbound update surface ────────────────────────────────────────
    # Telegram only delivers the update types getUpdates/setWebhook asks
    # for, so this list is the first line of containment: update types the
    # adapter has no handler for (chat_member, message_reaction, and — the
    # important case — the Telegram Business "secretary" updates) are never
    # requested unless the matching feature is enabled in config.
    _BASE_UPDATE_TYPES: tuple[str, ...] = ("message", "callback_query", "pre_checkout_query")
    _BUSINESS_UPDATE_TYPES: tuple[str, ...] = (
        "business_connection",
        "business_message",
        "edited_business_message",
        "deleted_business_messages",
    )

    def _business_mode_enabled(self) -> bool:
        """Secretary mode: process Telegram Business connections/messages."""
        return self._coerce_bool_extra("business_mode", False)

    def _allowed_update_types(self) -> list[str]:
        """Update types to request from Telegram, derived from config.

        Changing ``extra.business_mode`` takes effect on the next
        connect (agent restart) — the subscription is sent with
        start_polling/set_webhook.
        """
        allowed = list(self._BASE_UPDATE_TYPES)
        if self._business_mode_enabled():
            allowed.extend(self._BUSINESS_UPDATE_TYPES)
        return allowed

    # ── Telegram Business ("secretary mode") ─────────────────────────
    # With extra.business_mode enabled, the owner can connect this bot to
    # their personal Telegram Business account (Settings → Business →
    # Chatbots). Customer messages then arrive as business_message updates
    # carrying a business_connection_id, and replies sent WITH that id go
    # out as the owner, inside the owner↔customer chat.
    #
    # Business chats are namespaced as ``biz:<connection_id>:<chat_id>``
    # in SessionSource.chat_id, which (a) keeps their agent sessions apart
    # from a direct bot-DM with the same person, and (b) lets every
    # outbound path recover the business_connection_id from the chat ref.
    #
    # Two postures, gated on extra.business_auto_reply (default OFF):
    #   observe  — the customer message is forwarded to the owner's own
    #              chat with the bot; the agent briefs the owner and
    #              drafts a reply, but never messages the customer.
    #   auto-reply — the agent answers the customer directly on the
    #              owner's behalf (requires the can_reply right).

    _BIZ_CHAT_PREFIX = "biz:"

    #: Telegram addresses every chat by a signed integer id. Anything else
    #: reaching a send is a routing bug upstream (``targets.resolve_target``
    #: owns turning names into ids), and ``int()`` reports it as "invalid
    #: literal for int() with base 10: 'jrivera'" — which tells neither the
    #: operator reading the log nor the agent reading the tool result what to
    #: do about it.
    _PLAIN_CHAT_ID_RE = re.compile(r"-?\d+")

    @classmethod
    def _parse_chat_ref(cls, chat_id: str) -> tuple[str, Optional[str]]:
        """Split a chat reference into (numeric chat id, business_connection_id).

        Plain chat ids pass through as ``(chat_id, None)``.
        """
        raw = str(chat_id or "")
        if raw.startswith(cls._BIZ_CHAT_PREFIX):
            rest = raw[len(cls._BIZ_CHAT_PREFIX) :]
            conn_id, sep, plain = rest.partition(":")
            if sep and plain:
                return plain, conn_id or None
        return raw, None

    def quiet_reply_delay(self, event: MessageEvent) -> float:
        """Hold secretary replies for ``extra.business_reply_delay_seconds``.

        In secretary mode the customer believes they are messaging the account
        owner, and a human does not answer the instant the message lands — nor
        do they start typing the instant they read it.  So for business chats
        the adapter stays entirely silent for this many seconds (no typing
        status, no placeholder bubble, no agent turn), and only then runs the
        normal pipeline: typing indicator, then the reply.

        Scoped to auto-reply chats by the ``biz:`` chat-ref prefix
        (see _parse_chat_ref).  Observe-mode briefings go to the owner's own
        chat with the bot under a plain chat id, and the owner is waiting on
        them — those keep answering immediately.
        """
        _plain_chat, conn_id = self._parse_chat_ref(str(event.source.chat_id))
        if not conn_id or not self._business_mode_enabled():
            return 0.0
        return self._business_reply_delay

    _TOPIC_REF_RE = re.compile(r"^(-?\d+):(\d+)$")

    @classmethod
    def _split_topic_ref(cls, target: str) -> tuple[str, Optional[str]]:
        """Split a forum-topic target ``<chat_id>:<thread_id>`` → (chat, thread).

        ``-1004366376806:3`` addresses topic (message thread) 3 of that
        supergroup; plain chat ids pass through as ``(target, None)``. The
        ``biz:`` business-connection namespace never collides — those refs
        start with a letter prefix, not a numeric chat id.
        """
        m = cls._TOPIC_REF_RE.match(str(target or ""))
        if m:
            return m.group(1), m.group(2)
        return str(target or ""), None

    def _apply_topic_ref(self, target: str, kwargs: dict) -> tuple[str, dict]:
        """Fold a ``<chat_id>:<thread_id>`` target into metadata thread routing."""
        plain, thread = self._split_topic_ref(target)
        if thread is None:
            return target, kwargs
        metadata = dict(kwargs.get("metadata") or {})
        metadata.setdefault("thread_id", thread)
        return plain, {**kwargs, "metadata": metadata}

    # ChannelRegistry shim overrides: proactive sends (notification tools,
    # PULSE, scheduled reminders) address forum topics as ``<chat_id>:<thread>``
    # — every internal send path already routes metadata["thread_id"] into
    # ``message_thread_id``, so translating the target here covers text and
    # every media kind at once.
    async def send_text(self, text: str, target: str, **kwargs: Any) -> str:
        target, kwargs = self._apply_topic_ref(target, kwargs)
        return await super().send_text(text, target, **kwargs)

    async def send_media(
        self,
        media_url: str,
        media_type: str,
        target: str,
        caption: str = "",
        **kwargs: Any,
    ) -> str:
        target, kwargs = self._apply_topic_ref(target, kwargs)
        return await super().send_media(media_url, media_type, target, caption=caption, **kwargs)

    def outbound_thread_key(self, target: str, metadata: dict[str, Any] | None = None) -> str:
        """Base-class contract plus Telegram's forum-topic target grammar.

        An inbound message in forum topic 3 derives its base thread id from
        ``source.thread_id`` ("3"), so a proactive send addressed
        ``<chat_id>:3`` must record under "3" too. Plain chat ids and
        ``biz:``-namespaced business refs pass through — they match the
        inbound ``source.chat_id`` verbatim.
        """
        md_thread = str((metadata or {}).get("thread_id") or "")
        if md_thread:
            return md_thread
        _plain, thread = self._split_topic_ref(str(target or ""))
        return thread if thread is not None else str(target or "")

    def _business_state_path(self) -> "_Path":
        from olano.config.home import get_olano_home

        run_dir = get_olano_home() / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir / f"telegram-business-{self.agent_name or 'default'}.json"

    def _load_business_connections(self) -> None:
        try:
            raw = json.loads(self._business_state_path().read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._business_connections = {
                    str(k): v for k, v in raw.items() if isinstance(v, dict)
                }
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("[%s] Unreadable business-connection state: %s", self.name, e)

    def _save_business_connections(self) -> None:
        try:
            self._business_state_path().write_text(
                json.dumps(self._business_connections, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("[%s] Could not persist business connections: %s", self.name, e)

    @staticmethod
    def _business_connection_entry(conn: Any) -> dict[str, Any]:
        """Normalize a telegram.BusinessConnection into a plain JSON dict."""
        user = getattr(conn, "user", None)
        rights = getattr(conn, "rights", None)
        rights_dict: dict[str, Any] = {}
        if rights is not None:
            try:
                rights_dict = {k: v for k, v in rights.to_dict().items() if v is not None}
            except Exception:
                rights_dict = {"can_reply": bool(getattr(rights, "can_reply", False))}
        date = getattr(conn, "date", None)
        date_str = date.isoformat() if date is not None and hasattr(date, "isoformat") else None
        return {
            "id": str(getattr(conn, "id", "")),
            "user_id": str(getattr(user, "id", "") or ""),
            "user_name": str(getattr(user, "full_name", "") or ""),
            "username": str(getattr(user, "username", "") or ""),
            "user_chat_id": str(getattr(conn, "user_chat_id", "") or ""),
            "is_enabled": bool(getattr(conn, "is_enabled", False)),
            "date": date_str,
            "rights": rights_dict,
        }

    async def _handle_business_connection(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Track the owner connecting/disconnecting this bot to their account."""
        conn = update.business_connection
        if conn is None:
            return
        entry = self._business_connection_entry(conn)
        self._business_connections[entry["id"]] = entry
        self._save_business_connections()
        logger.info(
            "[%s] Business connection %s %s by %s (can_reply=%s)",
            self.name,
            entry["id"],
            "enabled" if entry["is_enabled"] else "disabled",
            entry["user_name"] or entry["user_id"],
            entry["rights"].get("can_reply", False),
        )

    async def _handle_deleted_business_messages(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        deleted = update.deleted_business_messages
        if deleted is None:
            return
        logger.info(
            "[%s] %d business message(s) deleted in chat %s (connection %s)",
            self.name,
            len(deleted.message_ids or ()),
            getattr(deleted.chat, "id", "?"),
            deleted.business_connection_id,
        )

    async def _get_business_connection_cached(self, conn_id: str) -> Optional[dict[str, Any]]:
        """Return the cached connection entry, fetching from Telegram on a miss.

        A miss happens when the owner connected the bot before this process
        booted and no fresh business_connection update has arrived yet.
        """
        entry = self._business_connections.get(conn_id)
        if entry is not None:
            return entry
        if self._bot is None:
            return None
        try:
            conn = await self._bot.get_business_connection(conn_id)
        except Exception as e:
            logger.warning("[%s] Could not fetch business connection %s: %s", self.name, conn_id, e)
            return None
        entry = self._business_connection_entry(conn)
        self._business_connections[entry["id"]] = entry
        self._save_business_connections()
        return entry

    def _business_customer_allowed(self, user: Any) -> bool:
        """Apply extra.business_allowed_customers (empty = all customers)."""
        if not self._biz_allowed_ids and not self._biz_allowed_usernames:
            return True
        if user is None:
            return False
        if str(getattr(user, "id", "")) in self._biz_allowed_ids:
            return True
        uname = (getattr(user, "username", "") or "").lower()
        return bool(uname) and uname in self._biz_allowed_usernames

    async def _handle_business_message(self, update: Update) -> None:
        """Route one inbound business (secretary-mode) message."""
        msg = update.business_message
        if msg is None:
            # edited_business_message — an edit must not re-trigger the agent.
            return
        if not self._business_mode_enabled():
            # Defense-in-depth: with business_mode off these updates are not
            # requested from Telegram, but drop them if one arrives anyway.
            return
        conn_id = getattr(msg, "business_connection_id", None)
        if not conn_id:
            return
        conn = await self._get_business_connection_cached(str(conn_id))
        if conn is not None and not conn.get("is_enabled", True):
            return
        user = msg.from_user
        # The owner's own messages in the connected chat also arrive as
        # business_message updates — never treat those as customer input. They
        # are not noise either: fold them into the conversation the agent
        # serves for this chat, so its next turn knows the owner already
        # weighed in (an owner reply like "actually, very similar to my other
        # project" otherwise leaves the agent blind to half the thread).
        owner_id = str((conn or {}).get("user_id") or "")
        if user is not None and owner_id and str(user.id) == owner_id:
            await self._record_owner_business_message(msg, conn, str(conn_id), update.update_id)
            return
        if not self._business_customer_allowed(user):
            logger.info(
                "[%s] Dropping business message: customer id=%s not in business_allowed_customers",
                self.name,
                getattr(user, "id", "?"),
            )
            return
        text = (msg.text or msg.caption or "").strip()
        if not text:
            logger.debug(
                "[%s] Ignoring non-text business message in chat %s "
                "(secretary mode is text-only for now)",
                self.name,
                getattr(msg.chat, "id", "?"),
            )
            return

        rights = (conn or {}).get("rights") or {}
        can_reply = bool(rights.get("can_reply"))

        if self._business_auto_reply and can_reply:
            event = self._build_message_event(
                msg,
                MessageType.TEXT,
                update_id=update.update_id,
                business_connection_id=str(conn_id),
            )
            await self.handle_message(event)
            return

        if self._business_auto_reply and not can_reply:
            logger.warning(
                "[%s] business_auto_reply is on but connection %s lacks the "
                "can_reply right — falling back to observe mode. Re-connect the "
                "bot in Telegram Business settings with reply permission.",
                self.name,
                conn_id,
            )

        # Observe mode: brief the owner in their own chat with the bot.
        owner_chat = str((conn or {}).get("user_chat_id") or "")
        if not owner_chat:
            logger.info(
                "[%s] Cannot route business message: connection %s has no "
                "user_chat_id (owner has never opened a chat with the bot)",
                self.name,
                conn_id,
            )
            return
        event = self._build_business_observe_event(msg, owner_chat, update.update_id, can_reply)
        await self.handle_message(event)

    def _build_business_observe_event(
        self,
        msg: Message,
        owner_chat_id: str,
        update_id: Optional[int],
        can_reply: bool,
    ) -> MessageEvent:
        """Wrap a customer message as a briefing delivered to the owner's chat."""
        user = msg.from_user
        customer = str(getattr(user, "full_name", "") or "") or "Unknown customer"
        handle = getattr(user, "username", None)
        who = f"{customer} (@{handle})" if handle else customer
        if can_reply:
            posture = (
                "Secretary observe mode: business_auto_reply is off, so do NOT "
                "attempt to contact the customer. Brief the owner and draft a "
                "suggested reply they can copy."
            )
        else:
            posture = (
                "Secretary observe mode: the business connection does not grant "
                "reply permission, so the customer cannot be contacted directly. "
                "Brief the owner and draft a suggested reply they can copy."
            )
        text = (
            "[Telegram Business message received via your connected account]\n"
            f"From: {who} (id={getattr(user, 'id', '?')}, "
            f"chat={getattr(msg.chat, 'id', '?')})\n"
            f"Message: {msg.text or msg.caption or ''}\n\n"
            f"({posture})"
        )
        source = self.build_source(
            chat_id=owner_chat_id,
            chat_name="Telegram Business inbox",
            chat_type="dm",
            user_id=str(getattr(user, "id", "") or "") or None,
            user_name=str(getattr(user, "full_name", "") or "") or None,
            username=handle,
        )
        # message_id is intentionally None: the customer's message id does not
        # exist in the owner's chat, so replying-to it there would 400.
        return MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=msg,
            platform_update_id=update_id,
            timestamp=msg.date,
        )

    async def _record_owner_business_message(
        self,
        msg: Message,
        conn: Optional[dict[str, Any]],
        conn_id: str,
        update_id: Optional[int],
    ) -> None:
        """Fold the owner's own message in a business chat into agent context.

        Routed as a *record-only* event to wherever the agent's conversation
        for this chat lives — the ``biz:``-namespaced chat session in
        auto-reply mode, the owner's briefing chat in observe mode. The agent
        never responds to it and no active run is interrupted; the runtime
        appends it to the thread's history so the next turn (typically the
        customer's reply) has the full exchange.
        """
        raw_text = (msg.text or msg.caption or "").strip()
        if not raw_text:
            return
        chat_id = str(getattr(msg.chat, "id", "") or "")
        # Respect the customer allowlist: a chat the agent is not serving gets
        # no context recorded either. The peer of a private business chat is
        # the customer, so gate on the chat's id / username.
        if self._biz_allowed_ids or self._biz_allowed_usernames:
            chat_uname = (getattr(msg.chat, "username", "") or "").lower()
            if chat_id not in self._biz_allowed_ids and (
                not chat_uname or chat_uname not in self._biz_allowed_usernames
            ):
                return

        rights = (conn or {}).get("rights") or {}
        owner_name = str((conn or {}).get("user_name") or "") or "The account owner"
        owner_handle = str((conn or {}).get("username") or "")
        who = f"{owner_name} (@{owner_handle})" if owner_handle else owner_name

        if self._business_auto_reply and bool(rights.get("can_reply")):
            # Auto-reply posture: the conversation lives in the biz-namespaced
            # chat session — the same one the customer's messages run on.
            event = self._build_message_event(
                msg, MessageType.TEXT, update_id=update_id, business_connection_id=conn_id
            )
            event.text = (
                f"[Telegram Business] {who} — the connected account owner, whom you "
                f'assist in this chat — sent this message themselves: "{raw_text}"'
            )
            event.record_only = True
            await self.handle_message(event)
            return

        # Observe posture: briefings for this customer chat go to the owner's
        # own chat with the bot, so the owner's direct reply belongs there too.
        owner_chat = str((conn or {}).get("user_chat_id") or "")
        if not owner_chat:
            return
        chat_label = str(
            getattr(msg.chat, "title", None) or getattr(msg.chat, "full_name", None) or chat_id
        )
        source = self.build_source(
            chat_id=owner_chat,
            chat_name="Telegram Business inbox",
            chat_type="dm",
            user_id=str((conn or {}).get("user_id") or "") or None,
            user_name=owner_name,
            username=owner_handle or None,
        )
        event = MessageEvent(
            text=(
                f"[Telegram Business] In your connected account's chat with "
                f'"{chat_label}" (chat={chat_id}), you (the owner) replied to the '
                f'customer directly: "{raw_text}"'
            ),
            message_type=MessageType.TEXT,
            source=source,
            raw_message=msg,
            platform_update_id=update_id,
            timestamp=msg.date,
            record_only=True,
        )
        await self.handle_message(event)

    def _coerce_bool_extra(self, key: str, default: bool = False) -> bool:
        value = self.config.extra.get(key) if getattr(self.config, "extra", None) else None
        if value is None:
            return default
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "1", "yes", "on"):
                return True
            if lowered in ("false", "0", "no", "off"):
                return False
            return default
        return bool(value)

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        """Read a float from the environment, ignoring unparseable values."""
        raw = os.getenv(name)
        if raw is None or not str(raw).strip():
            return default
        try:
            return float(str(raw).strip())
        except ValueError:
            logger.warning("Ignoring non-numeric %s=%r", name, raw)
            return default

    def _coerce_float_extra(self, key: str, default: float, minimum: float = 0.0) -> float:
        """Read a numeric ``config.extra`` value, falling back on garbage.

        Dashboard number fields arrive as strings, and an empty one means
        "unset" — both resolve to *default* rather than to zero, so clearing
        the field restores the default instead of silently disabling it.
        """
        value = self.config.extra.get(key) if getattr(self.config, "extra", None) else None
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        try:
            parsed = float(str(value).strip())
        except (TypeError, ValueError):
            logger.warning(
                "[%s] Ignoring non-numeric extra.%s=%r (using %s)", self.name, key, value, default
            )
            return default
        return max(minimum, parsed)

    def _link_preview_kwargs(self) -> dict[str, Any]:
        if not getattr(self, "_disable_link_previews", False):
            return {}
        if LinkPreviewOptions is not None:
            return {"link_preview_options": LinkPreviewOptions(is_disabled=True)}
        return {"disable_web_page_preview": True}

    async def _handle_polling_network_error(self, error: Exception) -> None:
        """Reconnect polling after a transient network interruption.

        Triggered by NetworkError/TimedOut in the polling error callback, which
        happen when the host loses connectivity (Mac sleep, WiFi switch, VPN
        reconnect, etc.).  The gateway process stays alive but the long-poll
        connection silently dies; without this handler the bot never recovers.

        Strategy: exponential back-off (5s, 10s, 20s, 40s, 60s cap) up to
        MAX_NETWORK_RETRIES attempts, then mark the adapter retryable-fatal so
        the supervisor restarts the gateway process.
        """
        if self.has_fatal_error:
            return

        MAX_NETWORK_RETRIES = 10
        BASE_DELAY = 5
        MAX_DELAY = 60

        self._polling_network_error_count += 1
        attempt = self._polling_network_error_count

        if attempt > MAX_NETWORK_RETRIES:
            message = (
                "Telegram polling could not reconnect after %d network error retries. "
                "Restarting gateway." % MAX_NETWORK_RETRIES
            )
            logger.error("[%s] %s Last error: %s", self.name, message, error)
            self._set_fatal_error("telegram_network_error", message, retryable=True)
            await self._notify_fatal_error()
            return

        delay = min(BASE_DELAY * (2 ** (attempt - 1)), MAX_DELAY)
        logger.warning(
            "[%s] Telegram network error (attempt %d/%d), reconnecting in %ds. Error: %s",
            self.name,
            attempt,
            MAX_NETWORK_RETRIES,
            delay,
            error,
        )
        await asyncio.sleep(delay)

        try:
            if self._app and self._app.updater and self._app.updater.running:
                await self._app.updater.stop()
        except Exception:
            pass

        try:
            assert self._app is not None and self._app.updater is not None
            await self._app.updater.start_polling(
                allowed_updates=self._allowed_update_types(),
                drop_pending_updates=False,
                error_callback=self._polling_error_callback_ref,
            )
            logger.info(
                "[%s] Telegram polling resumed after network error (attempt %d)",
                self.name,
                attempt,
            )
            self._polling_network_error_count = 0
        except Exception as retry_err:
            logger.warning("[%s] Telegram polling reconnect failed: %s", self.name, retry_err)
            # start_polling failed — polling is dead and no further error
            # callbacks will fire, so schedule the next retry ourselves.
            if not self.has_fatal_error:
                task = asyncio.ensure_future(self._handle_polling_network_error(retry_err))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

    async def _handle_polling_conflict(self, error: Exception) -> None:
        if self.has_fatal_error and self.fatal_error_code == "telegram_polling_conflict":
            return
        # Track consecutive conflicts — transient 409s can occur when a
        # previous gateway instance hasn't fully released its long-poll
        # session on Telegram's server (e.g. during --replace handoffs or
        # systemd Restart=on-failure respawns).  Retry a few times before
        # giving up, so the old session has time to expire.
        self._polling_conflict_count += 1

        MAX_CONFLICT_RETRIES = 3
        RETRY_DELAY = 10  # seconds

        if self._polling_conflict_count <= MAX_CONFLICT_RETRIES:
            logger.warning(
                "[%s] Telegram polling conflict (%d/%d), will retry in %ds. Error: %s",
                self.name,
                self._polling_conflict_count,
                MAX_CONFLICT_RETRIES,
                RETRY_DELAY,
                error,
            )
            try:
                if self._app and self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
            except Exception:
                pass
            await asyncio.sleep(RETRY_DELAY)
            try:
                assert self._app is not None and self._app.updater is not None
                await self._app.updater.start_polling(
                    allowed_updates=self._allowed_update_types(),
                    drop_pending_updates=False,
                    error_callback=self._polling_error_callback_ref,
                )
                logger.info(
                    "[%s] Telegram polling resumed after conflict retry %d",
                    self.name,
                    self._polling_conflict_count,
                )
                self._polling_conflict_count = 0  # reset on success
                return
            except Exception as retry_err:
                logger.warning("[%s] Telegram polling retry failed: %s", self.name, retry_err)
                # Don't fall through to fatal yet — wait for the next conflict
                # to trigger another retry attempt (up to MAX_CONFLICT_RETRIES).
                return

        # Exhausted retries — fatal
        message = (
            "Another process is already polling this Telegram bot token "
            "(possibly OpenClaw or another Olano instance). "
            "Olano stopped Telegram polling after %d retries. "
            "Only one poller can run per token — stop the other process "
            "and restart with 'olano start'." % MAX_CONFLICT_RETRIES
        )
        logger.error("[%s] %s Original error: %s", self.name, message, error)
        self._set_fatal_error("telegram_polling_conflict", message, retryable=False)
        try:
            if self._app and self._app.updater:
                await self._app.updater.stop()
        except Exception as stop_error:
            logger.warning(
                "[%s] Failed stopping Telegram polling after conflict: %s",
                self.name,
                stop_error,
                exc_info=True,
            )
        await self._notify_fatal_error()

    async def _create_dm_topic(
        self,
        chat_id: int,
        name: str,
        icon_color: Optional[int] = None,
        icon_custom_emoji_id: Optional[str] = None,
    ) -> Optional[int]:
        """Create a forum topic in a private (DM) chat.

        Uses Bot API 9.4's createForumTopic which now works for 1-on-1 chats.
        Returns the message_thread_id on success, None on failure.
        """
        if not self._bot:
            return None
        try:
            kwargs: dict[str, Any] = {"chat_id": chat_id, "name": name}
            if icon_color is not None:
                kwargs["icon_color"] = icon_color
            if icon_custom_emoji_id:
                kwargs["icon_custom_emoji_id"] = icon_custom_emoji_id

            topic = await self._bot.create_forum_topic(**kwargs)
            thread_id = topic.message_thread_id
            logger.info(
                "[%s] Created DM topic '%s' in chat %s -> thread_id=%s",
                self.name,
                name,
                chat_id,
                thread_id,
            )
            return thread_id
        except Exception as e:
            error_text = str(e).lower()
            # If topic already exists, try to find it via getForumTopicIconStickers
            # or we just log and skip — Telegram doesn't provide a "list topics" API
            if "topic_name_duplicate" in error_text or "already" in error_text:
                logger.info(
                    "[%s] DM topic '%s' already exists in chat %s (will be mapped from incoming messages)",
                    self.name,
                    name,
                    chat_id,
                )
            else:
                logger.warning(
                    "[%s] Failed to create DM topic '%s' in chat %s: %s",
                    self.name,
                    name,
                    chat_id,
                    e,
                )
            return None

    def _persist_dm_topic_thread_id(self, chat_id: int, topic_name: str, thread_id: int) -> None:
        """Save a newly created thread_id back into config.yaml so it persists across restarts."""
        try:
            from olano.config.home import get_olano_home

            config_path = get_olano_home() / "config.yaml"
            if not config_path.exists():
                logger.warning(
                    "[%s] Config file not found at %s, cannot persist thread_id",
                    self.name,
                    config_path,
                )
                return

            import yaml as _yaml

            with open(config_path) as f:
                config = _yaml.safe_load(f) or {}

            # Navigate to platforms.telegram.extra.dm_topics
            dm_topics = (
                config.get("platforms", {})
                .get("telegram", {})
                .get("extra", {})
                .get("dm_topics", [])
            )
            if not dm_topics:
                return

            changed = False
            for chat_entry in dm_topics:
                if int(chat_entry.get("chat_id", 0)) != int(chat_id):
                    continue
                for t in chat_entry.get("topics", []):
                    if t.get("name") == topic_name and not t.get("thread_id"):
                        t["thread_id"] = thread_id
                        changed = True
                        break

            if changed:
                with open(config_path, "w") as f:
                    _yaml.dump(config, f, default_flow_style=False, sort_keys=False)
                logger.info(
                    "[%s] Persisted thread_id=%s for topic '%s' in config.yaml",
                    self.name,
                    thread_id,
                    topic_name,
                )
        except Exception as e:
            logger.warning(
                "[%s] Failed to persist thread_id to config: %s", self.name, e, exc_info=True
            )

    async def _setup_dm_topics(self) -> None:
        """Load or create configured DM topics for specified chats.

        Reads config.extra['dm_topics'] — a list of dicts:
        [
            {
                "chat_id": 123456789,
                "topics": [
                    {"name": "General", "icon_color": 7322096, "thread_id": 100},
                    {"name": "Accessibility Auditor", "icon_color": 9367192, "skill": "accessibility-auditor"}
                ]
            }
        ]

        If a topic already has a thread_id in the config (persisted from a previous
        creation), it is loaded into the cache without calling createForumTopic.
        Only topics without a thread_id are created via the API, and their thread_id
        is then saved back to config.yaml for future restarts.
        """
        if not self._dm_topics_config:
            return

        for chat_entry in self._dm_topics_config:
            chat_id = chat_entry.get("chat_id")
            topics = chat_entry.get("topics", [])
            if not chat_id or not topics:
                continue

            logger.info(
                "[%s] Setting up %d DM topic(s) for chat %s",
                self.name,
                len(topics),
                chat_id,
            )

            for topic_conf in topics:
                topic_name = topic_conf.get("name")
                if not topic_name:
                    continue

                cache_key = f"{chat_id}:{topic_name}"

                # If thread_id is already persisted in config, just load into cache
                existing_thread_id = topic_conf.get("thread_id")
                if existing_thread_id:
                    self._dm_topics[cache_key] = int(existing_thread_id)
                    logger.info(
                        "[%s] DM topic loaded from config: %s -> thread_id=%s",
                        self.name,
                        cache_key,
                        existing_thread_id,
                    )
                    continue

                # No persisted thread_id — create the topic via API
                icon_color = topic_conf.get("icon_color")
                icon_emoji = topic_conf.get("icon_custom_emoji_id")

                thread_id = await self._create_dm_topic(
                    chat_id=int(chat_id),
                    name=topic_name,
                    icon_color=icon_color,
                    icon_custom_emoji_id=icon_emoji,
                )

                if thread_id:
                    self._dm_topics[cache_key] = thread_id
                    logger.info(
                        "[%s] DM topic cached: %s -> thread_id=%s",
                        self.name,
                        cache_key,
                        thread_id,
                    )
                    # Persist thread_id to config so we don't recreate on next restart
                    self._persist_dm_topic_thread_id(int(chat_id), topic_name, thread_id)

    async def connect(self) -> bool:
        """Connect to Telegram via polling or webhook.

        By default, uses long polling (outbound connection to Telegram).
        If ``TELEGRAM_WEBHOOK_URL`` is set, starts an HTTP webhook server
        instead.  Webhook mode is useful for cloud deployments (Fly.io,
        Railway) where inbound HTTP can wake a suspended machine.

        Env vars for webhook mode::

            TELEGRAM_WEBHOOK_URL    Public HTTPS URL (e.g. https://app.fly.dev/telegram)
            TELEGRAM_WEBHOOK_PORT   Local listen port (default 8443)
            TELEGRAM_WEBHOOK_SECRET Secret token for update verification
        """
        if not TELEGRAM_AVAILABLE:
            logger.error(
                "[%s] python-telegram-bot not installed. Run: pip install python-telegram-bot",
                self.name,
            )
            return False

        if not self.config.token:
            logger.error("[%s] No bot token configured", self.name)
            return False

        try:
            if not self._acquire_platform_lock(
                "telegram-bot-token", self.config.token, "Telegram bot token"
            ):
                return False

            # Build the application
            builder = Application.builder().token(self.config.token)
            custom_base_url = self.config.extra.get("base_url")
            if custom_base_url:
                builder = builder.base_url(custom_base_url)
                builder = builder.base_file_url(
                    self.config.extra.get("base_file_url", custom_base_url)
                )
                logger.info(
                    "[%s] Using custom Telegram base_url: %s",
                    self.name,
                    custom_base_url,
                )

            # PTB defaults (pool_timeout=1s) are too aggressive on flaky networks and
            # can trigger "Pool timeout: All connections in the connection pool are occupied"
            # during reconnect/bootstrap. Use safer defaults and allow env overrides.
            def _env_int(name: str, default: int) -> int:
                try:
                    return int(os.getenv(name, str(default)))
                except (TypeError, ValueError):
                    return default

            def _env_float(name: str, default: float) -> float:
                try:
                    return float(os.getenv(name, str(default)))
                except (TypeError, ValueError):
                    return default

            request_kwargs: dict[str, Any] = {
                "connection_pool_size": _env_int("LIONCLAW_TELEGRAM_HTTP_POOL_SIZE", 512),
                "pool_timeout": _env_float("LIONCLAW_TELEGRAM_HTTP_POOL_TIMEOUT", 8.0),
                "connect_timeout": _env_float("LIONCLAW_TELEGRAM_HTTP_CONNECT_TIMEOUT", 10.0),
                "read_timeout": _env_float("LIONCLAW_TELEGRAM_HTTP_READ_TIMEOUT", 20.0),
                "write_timeout": _env_float("LIONCLAW_TELEGRAM_HTTP_WRITE_TIMEOUT", 20.0),
            }

            proxy_url = resolve_proxy_url("TELEGRAM_PROXY")
            disable_fallback = os.getenv(
                "LIONCLAW_TELEGRAM_DISABLE_FALLBACK_IPS", ""
            ).strip().lower() in ("1", "true", "yes", "on")
            fallback_ips = self._fallback_ips()
            if not fallback_ips:
                fallback_ips = await discover_fallback_ips()
                logger.info(
                    "[%s] Auto-discovered Telegram fallback IPs: %s",
                    self.name,
                    ", ".join(fallback_ips),
                )

            if fallback_ips and not proxy_url and not disable_fallback:
                logger.info(
                    "[%s] Telegram fallback IPs active: %s",
                    self.name,
                    ", ".join(fallback_ips),
                )
                # Keep request/update pools separate to reduce contention during
                # polling reconnect + bot API bootstrap/delete_webhook calls.
                request = HTTPXRequest(
                    **request_kwargs,
                    httpx_kwargs={"transport": TelegramFallbackTransport(fallback_ips)},
                )
                get_updates_request = HTTPXRequest(
                    **request_kwargs,
                    httpx_kwargs={"transport": TelegramFallbackTransport(fallback_ips)},
                )
            elif proxy_url:
                logger.info(
                    "[%s] Proxy detected; passing explicitly to HTTPXRequest: %s",
                    self.name,
                    proxy_url,
                )
                request = HTTPXRequest(**request_kwargs, proxy=proxy_url)
                get_updates_request = HTTPXRequest(**request_kwargs, proxy=proxy_url)
            else:
                if disable_fallback:
                    logger.info("[%s] Telegram fallback-IP transport disabled via env", self.name)
                request = HTTPXRequest(**request_kwargs)
                get_updates_request = HTTPXRequest(**request_kwargs)

            builder = builder.request(request).get_updates_request(get_updates_request)
            self._app = builder.build()
            self._bot = self._app.bot

            # Register handlers
            self._app.add_handler(
                TelegramMessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_text_message)
            )
            self._app.add_handler(TelegramMessageHandler(filters.COMMAND, self._handle_command))
            self._app.add_handler(
                TelegramMessageHandler(
                    filters.LOCATION | getattr(filters, "VENUE", filters.LOCATION),
                    self._handle_location_message,
                )
            )
            self._app.add_handler(
                TelegramMessageHandler(
                    filters.PHOTO
                    | filters.VIDEO
                    | filters.AUDIO
                    | filters.VOICE
                    | filters.Document.ALL
                    | filters.Sticker.ALL,
                    self._handle_media_message,
                )
            )
            # Track this bot's own membership (added to / removed from a
            # group or channel) so the chat directory learns the chat id the
            # moment the bot joins — a Telegram bot cannot enumerate its
            # chats, so this event is the only way to know a silent group.
            self._app.add_handler(
                ChatMemberHandler(self._handle_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER)
            )
            # Group → supergroup upgrades change the chat id; record the
            # mapping so stored ids (schedules, memory) keep resolving.
            self._app.add_handler(
                TelegramMessageHandler(filters.StatusUpdate.MIGRATE, self._handle_chat_migration)
            )
            # Handle inline keyboard button callbacks (update prompts)
            self._app.add_handler(CallbackQueryHandler(self._handle_callback_query))
            # Telegram Stars payments (creator monetization)
            self._app.add_handler(PreCheckoutQueryHandler(self._handle_pre_checkout))
            self._app.add_handler(
                TelegramMessageHandler(filters.SUCCESSFUL_PAYMENT, self._handle_successful_payment)
            )
            # Telegram Business ("secretary mode") — only active when the
            # update types are subscribed (extra.business_mode).
            if self._business_mode_enabled():
                self._app.add_handler(BusinessConnectionHandler(self._handle_business_connection))
                self._app.add_handler(
                    BusinessMessagesDeletedHandler(self._handle_deleted_business_messages)
                )

            # Start polling — retry initialize() for transient TLS resets
            try:
                from telegram.error import NetworkError, TimedOut
            except ImportError:
                NetworkError = TimedOut = OSError  # type: ignore[misc,assignment]
            _max_connect = 8
            for _attempt in range(_max_connect):
                try:
                    await self._app.initialize()
                    break
                except (NetworkError, TimedOut, OSError) as init_err:
                    if _attempt < _max_connect - 1:
                        wait = min(2**_attempt, 15)
                        logger.warning(
                            "[%s] Connect attempt %d/%d failed: %s — retrying in %ds",
                            self.name,
                            _attempt + 1,
                            _max_connect,
                            init_err,
                            wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise
            await self._app.start()

            # Decide between webhook and polling mode
            webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()

            if webhook_url:
                # ── Webhook mode ─────────────────────────────────────
                # Telegram pushes updates to our HTTP endpoint.  This
                # enables cloud platforms (Fly.io, Railway) to auto-wake
                # suspended machines on inbound HTTP traffic.
                from olano.platforms.helpers import per_agent_port

                # Per-agent default so two agents in webhook mode don't
                # fight over the same TCP port. ``TELEGRAM_WEBHOOK_PORT``
                # env var pins a fixed port if set.
                cfg_port = (self.config.extra or {}).get("webhook_port")
                webhook_port = int(
                    cfg_port
                    if cfg_port is not None
                    else os.getenv(
                        "TELEGRAM_WEBHOOK_PORT",
                        str(per_agent_port(8443, self.agent_name)),
                    )
                )
                webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip() or None
                from urllib.parse import urlparse

                webhook_path = urlparse(webhook_url).path or "/telegram"

                assert self._app is not None and self._app.updater is not None
                await self._app.updater.start_webhook(
                    listen="0.0.0.0",
                    port=webhook_port,
                    url_path=webhook_path,
                    webhook_url=webhook_url,
                    secret_token=webhook_secret,
                    allowed_updates=self._allowed_update_types(),
                    drop_pending_updates=True,
                )
                self._webhook_mode = True
                logger.info(
                    "[%s] Webhook server listening on 0.0.0.0:%d%s",
                    self.name,
                    webhook_port,
                    webhook_path,
                )
            else:
                # ── Polling mode (default) ───────────────────────────
                # Clear any stale webhook first so polling doesn't inherit a
                # previous webhook registration and silently stop receiving updates.
                delete_webhook = getattr(self._bot, "delete_webhook", None)
                if callable(delete_webhook):
                    await delete_webhook(drop_pending_updates=False)

                loop = asyncio.get_running_loop()

                def _polling_error_callback(error: Exception) -> None:
                    if self._polling_error_task and not self._polling_error_task.done():
                        return
                    if self._looks_like_polling_conflict(error):
                        self._polling_error_task = loop.create_task(
                            self._handle_polling_conflict(error)
                        )
                    elif self._looks_like_network_error(error):
                        logger.warning(
                            "[%s] Telegram network error, scheduling reconnect: %s",
                            self.name,
                            error,
                        )
                        self._polling_error_task = loop.create_task(
                            self._handle_polling_network_error(error)
                        )
                    else:
                        logger.error(
                            "[%s] Telegram polling error: %s", self.name, error, exc_info=True
                        )

                # Store reference for retry use in _handle_polling_conflict
                self._polling_error_callback_ref = _polling_error_callback

                assert self._app is not None and self._app.updater is not None
                await self._app.updater.start_polling(
                    allowed_updates=self._allowed_update_types(),
                    drop_pending_updates=True,
                    error_callback=_polling_error_callback,
                )

            # Register bot commands so Telegram shows a hint menu when users
            # type "/". The list is per-agent: exactly what this agent exposes.
            await self.publish_command_menu()

            self._mark_connected()
            mode = "webhook" if self._webhook_mode else "polling"
            logger.info("[%s] Connected to Telegram (%s mode)", self.name, mode)

            # Set up DM topics (Bot API 9.4 — Private Chat Topics)
            # Runs after connection is established so the bot can call createForumTopic.
            # Failures here are non-fatal — the bot works fine without topics.
            try:
                await self._setup_dm_topics()
            except Exception as topics_err:
                logger.warning(
                    "[%s] DM topics setup failed (non-fatal): %s",
                    self.name,
                    topics_err,
                    exc_info=True,
                )

            return True

        except Exception as e:
            self._release_platform_lock()
            message = f"Telegram startup failed: {e}"
            self._set_fatal_error("telegram_connect_error", message, retryable=True)
            logger.error("[%s] Failed to connect to Telegram: %s", self.name, e, exc_info=True)
            return False

    async def publish_command_menu(self, *, force: bool = False) -> bool:
        """Publish this agent's ``/``-autocomplete menu via ``setMyCommands``.

        The menu is per-agent: the built-ins this agent's ``commands`` config
        exposes (so the *sensitive* ones stay hidden unless the owner enabled
        them) plus its custom commands. Advertising more than that offered
        commands the inbound gate then refused with
        "🔒 … is disabled for this agent".

        Skips the call when the menu is unchanged since the last publish —
        connect() and every Commands-tab save both land here. Never raises: a
        bot works fine without an autocomplete hint.
        """
        if self._bot is None:
            return False
        try:
            from telegram import BotCommand

            from olano.cli.commands import telegram_menu_commands, telegram_unlistable_custom

            commands = None
            if self.agent_name:
                from olano.config.loader import load_agent_config

                commands = getattr(load_agent_config(self.agent_name), "commands", None)

            # Telegram allows up to 100 commands but has an undocumented
            # payload size limit.  Descriptions are truncated to 40 chars in
            # telegram_menu_commands() to fit 100 commands safely.
            menu, hidden_count = telegram_menu_commands(max_commands=100, commands=commands)
            if not force and menu == self._published_menu:
                return False

            await self._bot.set_my_commands([BotCommand(name, desc) for name, desc in menu])
            self._published_menu = menu
            logger.info(
                "[%s] Telegram menu: %d commands registered%s",
                self.name,
                len(menu),
                f", {hidden_count} hidden (over the 100 limit)" if hidden_count else "",
            )
            # A custom command may legally be named with a hyphen; Telegram's
            # menu cannot carry one. Say so, or the owner just sees it missing.
            unlistable = telegram_unlistable_custom(commands)
            if unlistable:
                logger.info(
                    "[%s] Custom commands not shown in Telegram's menu (its names allow only "
                    "a-z, 0-9 and _): %s. They still work when typed.",
                    self.name,
                    ", ".join(f"/{n}" for n in unlistable),
                )
            return True
        except Exception as e:
            logger.warning(
                "[%s] Could not register Telegram command menu: %s",
                self.name,
                e,
                exc_info=True,
            )
            return False

    async def disconnect(self) -> None:
        """Stop polling/webhook, cancel pending album flushes, and disconnect."""
        pending_media_group_tasks = list(self._media_group_tasks.values())
        for task in pending_media_group_tasks:
            task.cancel()
        if pending_media_group_tasks:
            await asyncio.gather(*pending_media_group_tasks, return_exceptions=True)
        self._media_group_tasks.clear()
        self._media_group_events.clear()

        if self._app:
            try:
                # Only stop the updater if it's running
                if self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
                if self._app.running:
                    await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.warning(
                    "[%s] Error during Telegram disconnect: %s", self.name, e, exc_info=True
                )
        self._release_platform_lock()

        for task in self._pending_photo_batch_tasks.values():
            if task and not task.done():
                task.cancel()
        self._pending_photo_batch_tasks.clear()
        self._pending_photo_batches.clear()
        self._thinking_placeholders.clear()

        self._mark_disconnected()
        self._app = None
        self._bot = None
        logger.info("[%s] Disconnected from Telegram", self.name)

    def _should_thread_reply(self, reply_to: Optional[str], chunk_index: int) -> bool:
        """Determine if this message chunk should thread to the original message.

        Args:
            reply_to: The original message ID to reply to
            chunk_index: Index of this chunk (0 = first chunk)

        Returns:
            True if this chunk should be threaded to the original message
        """
        if not reply_to:
            return False
        mode = self._reply_to_mode
        if mode == "off":
            return False
        elif mode == "all":
            return True
        else:  # "first" (default)
            return chunk_index == 0

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message to a Telegram chat."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        # Skip whitespace-only text to prevent Telegram 400 empty-text errors.
        if not content or not content.strip():
            return SendResult(success=True, message_id=None)

        try:
            # Format and split message if needed
            formatted = self.format_message(content)
            chunks = self.truncate_message(
                formatted,
                self.MAX_MESSAGE_LENGTH,
                len_fn=utf16_len,
            )
            if len(chunks) > 1:
                # truncate_message appends a raw " (1/2)" suffix. Escape the
                # MarkdownV2-special parentheses so Telegram doesn't reject the
                # chunk and fall back to plain text.
                chunks = [re.sub(r" \((\d+)/(\d+)\)$", r" \\(\1/\2\\)", chunk) for chunk in chunks]

            message_ids = []
            thread_id = self._metadata_thread_id(metadata)
            # Secretary-mode chats carry the business_connection_id in the
            # chat ref; sends with it go out as the owner, in the owner's
            # business chat. (chat_id itself keeps the full ref — it is the
            # thinking-placeholder key.)
            plain_chat_id, biz_conn_id = self._parse_chat_ref(chat_id)
            if not self._PLAIN_CHAT_ID_RE.fullmatch(plain_chat_id):
                # Name the unusable value (see _PLAIN_CHAT_ID_RE) and mark it
                # non-retryable: re-sending a name cannot make it numeric, and
                # the default `retryable=True` below would spend three
                # attempts proving that.
                return SendResult(
                    success=False,
                    error=(
                        f"{plain_chat_id!r} is not a Telegram chat id — Telegram addresses "
                        f"chats by numeric id. Look one up with chats_find/chats_list, or "
                        f"ask the person to message this bot first."
                    ),
                    retryable=False,
                )

            try:
                from telegram.error import NetworkError as _NetErr
            except ImportError:
                _NetErr = OSError  # type: ignore[misc,assignment]

            try:
                from telegram.error import BadRequest as _BadReq
            except ImportError:
                _BadReq = None  # type: ignore[assignment,misc]

            try:
                from telegram.error import TimedOut as _TimedOut
            except (ImportError, AttributeError):
                _TimedOut = None  # type: ignore[assignment,misc]

            # If this send is the reply a "Thinking…" placeholder was staged
            # for, edit the placeholder in place with the first chunk so it
            # morphs into the final message instead of lingering as a stray
            # bubble.  On edit failure (e.g. the user deleted it) fall back
            # to deleting the placeholder and sending normally.
            start_index = 0
            placeholder_id = self._claim_thinking_placeholder(chat_id, reply_to)
            if placeholder_id is not None:
                if await self._edit_thinking_into_reply(chat_id, placeholder_id, chunks[0]):
                    message_ids.append(str(placeholder_id))
                    start_index = 1
                else:
                    await self._delete_thinking_message(chat_id, placeholder_id)

            for i, chunk in enumerate(chunks):
                if i < start_index:
                    continue
                should_thread = self._should_thread_reply(reply_to, i)
                reply_to_id = int(reply_to) if should_thread and reply_to else None
                effective_thread_id = self._message_thread_id_for_send(thread_id)

                msg = None
                for _send_attempt in range(3):
                    try:
                        # Try Markdown first, fall back to plain text if it fails
                        try:
                            msg = await self._bot.send_message(
                                chat_id=int(plain_chat_id),
                                text=chunk,
                                parse_mode=ParseMode.MARKDOWN_V2,
                                reply_to_message_id=reply_to_id,
                                message_thread_id=effective_thread_id,
                                business_connection_id=biz_conn_id,
                                **self._link_preview_kwargs(),
                            )
                        except Exception as md_error:
                            # Markdown parsing failed, try plain text
                            if (
                                "parse" in str(md_error).lower()
                                or "markdown" in str(md_error).lower()
                            ):
                                logger.warning(
                                    "[%s] MarkdownV2 parse failed, falling back to plain text: %s",
                                    self.name,
                                    md_error,
                                )
                                plain_chunk = _strip_mdv2(chunk)
                                msg = await self._bot.send_message(
                                    chat_id=int(plain_chat_id),
                                    text=plain_chunk,
                                    parse_mode=None,
                                    reply_to_message_id=reply_to_id,
                                    message_thread_id=effective_thread_id,
                                    business_connection_id=biz_conn_id,
                                    **self._link_preview_kwargs(),
                                )
                            else:
                                raise
                        break  # success
                    except _NetErr as send_err:
                        # BadRequest is a subclass of NetworkError in
                        # python-telegram-bot but represents permanent errors
                        # (not transient network issues). Detect and handle
                        # specific cases instead of blindly retrying.
                        if _BadReq is not None and isinstance(send_err, _BadReq):
                            if (
                                self._is_thread_not_found_error(send_err)
                                and effective_thread_id is not None
                            ):
                                # Thread doesn't exist — retry without
                                # message_thread_id so the message still
                                # reaches the chat.
                                logger.warning(
                                    "[%s] Thread %s not found, retrying without message_thread_id",
                                    self.name,
                                    effective_thread_id,
                                )
                                effective_thread_id = None
                                continue
                            err_lower = str(send_err).lower()
                            if (
                                "message to be replied not found" in err_lower
                                and reply_to_id is not None
                            ):
                                # Original message was deleted before we
                                # could reply — clear reply target and retry
                                # so the response is still delivered.
                                logger.warning(
                                    "[%s] Reply target deleted, retrying without reply_to: %s",
                                    self.name,
                                    send_err,
                                )
                                reply_to_id = None
                                continue
                            # Other BadRequest errors are permanent — don't retry
                            raise
                        # TimedOut is also a subclass of NetworkError but
                        # indicates the request may have reached the server —
                        # retrying risks duplicate message delivery.
                        if _TimedOut is not None and isinstance(send_err, _TimedOut):
                            raise
                        if _send_attempt < 2:
                            wait = 2**_send_attempt
                            logger.warning(
                                "[%s] Network error on send (attempt %d/3), retrying in %ds: %s",
                                self.name,
                                _send_attempt + 1,
                                wait,
                                send_err,
                            )
                            await asyncio.sleep(wait)
                        else:
                            raise
                    except Exception as send_err:
                        retry_after = getattr(send_err, "retry_after", None)
                        if retry_after is not None or "retry after" in str(send_err).lower():
                            if _send_attempt < 2:
                                wait = float(retry_after) if retry_after is not None else 1.0
                                logger.warning(
                                    "[%s] Telegram flood control on send (attempt %d/3), retrying in %.1fs: %s",
                                    self.name,
                                    _send_attempt + 1,
                                    wait,
                                    send_err,
                                )
                                await asyncio.sleep(wait)
                                continue
                        raise
                assert msg is not None
                message_ids.append(str(msg.message_id))

            return SendResult(
                success=True,
                message_id=message_ids[0] if message_ids else None,
                raw_response={"message_ids": message_ids},
            )

        except Exception as e:
            logger.error("[%s] Failed to send Telegram message: %s", self.name, e, exc_info=True)
            # TimedOut means the request may have reached Telegram —
            # mark as non-retryable so _send_with_retry() doesn't re-send.
            _to = locals().get("_TimedOut")
            err_str = str(e).lower()
            is_timeout = (_to and isinstance(e, _to)) or "timed out" in err_str
            return SendResult(success=False, error=str(e), retryable=not is_timeout)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Edit a previously sent Telegram message."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")
        chat_id, biz_conn_id = self._parse_chat_ref(chat_id)
        try:
            formatted = self.format_message(content)
            try:
                await self._bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=int(message_id),
                    text=formatted,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    business_connection_id=biz_conn_id,
                )
            except Exception as fmt_err:
                # "Message is not modified" is a no-op, not an error
                if "not modified" in str(fmt_err).lower():
                    return SendResult(success=True, message_id=message_id)
                # Fallback: retry without markdown formatting
                await self._bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=int(message_id),
                    text=content,
                    business_connection_id=biz_conn_id,
                )
            return SendResult(success=True, message_id=message_id)
        except Exception as e:
            err_str = str(e).lower()
            # "Message is not modified" — content identical, treat as success
            if "not modified" in err_str:
                return SendResult(success=True, message_id=message_id)
            # Message too long — content exceeded 4096 chars (e.g. during
            # streaming).  Truncate and succeed so the stream consumer can
            # split the overflow into a new message instead of dying.
            if "message_too_long" in err_str or "too long" in err_str:
                truncated = _prefix_within_utf16_limit(content, self.MAX_MESSAGE_LENGTH - 20) + "…"
                try:
                    await self._bot.edit_message_text(
                        chat_id=int(chat_id),
                        message_id=int(message_id),
                        text=truncated,
                        business_connection_id=biz_conn_id,
                    )
                except Exception:
                    pass  # best-effort truncation
                return SendResult(success=True, message_id=message_id)
            # Flood control / RetryAfter — short waits are retried inline,
            # long waits return a failure immediately so streaming can fall back
            # to a normal final send instead of leaving a truncated partial.
            retry_after = getattr(e, "retry_after", None)
            if retry_after is not None or "retry after" in err_str:
                wait = retry_after if retry_after else 1.0
                logger.warning(
                    "[%s] Telegram flood control, waiting %.1fs",
                    self.name,
                    wait,
                )
                if wait > 5.0:
                    return SendResult(success=False, error=f"flood_control:{wait}")
                await asyncio.sleep(wait)
                try:
                    await self._bot.edit_message_text(
                        chat_id=int(chat_id),
                        message_id=int(message_id),
                        text=content,
                        business_connection_id=biz_conn_id,
                    )
                    return SendResult(success=True, message_id=message_id)
                except Exception as retry_err:
                    logger.error(
                        "[%s] Edit retry failed after flood wait: %s",
                        self.name,
                        retry_err,
                    )
                    return SendResult(success=False, error=str(retry_err))
            logger.error(
                "[%s] Failed to edit Telegram message %s: %s",
                self.name,
                message_id,
                e,
                exc_info=True,
            )
            return SendResult(success=False, error=str(e))

    async def send_update_prompt(
        self,
        chat_id: str,
        prompt: str,
        default: str = "",
        session_key: str = "",
    ) -> SendResult:
        """Send an inline-keyboard update prompt (Yes / No buttons).

        Used by the gateway ``/update`` watcher when ``olano update --gateway``
        needs user input (stash restore, config migration).
        """
        if not self._bot:
            return SendResult(success=False, error="Not connected")
        try:
            default_hint = f" (default: {default})" if default else ""
            text = f"⚕ *Update needs your input:*\n\n{prompt}{default_hint}"
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✓ Yes", callback_data="update_prompt:y"),
                        InlineKeyboardButton("✗ No", callback_data="update_prompt:n"),
                    ]
                ]
            )
            msg = await self._bot.send_message(
                chat_id=int(chat_id),
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
                **self._link_preview_kwargs(),
            )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.warning("[%s] send_update_prompt failed: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send an inline-keyboard approval prompt with interactive buttons.

        The buttons call ``resolve_gateway_approval()`` to unblock the waiting
        agent thread — same mechanism as the text ``/approve`` flow.
        """
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            cmd_preview = command[:3800] + "..." if len(command) > 3800 else command
            text = (
                f"⚠️ <b>Command Approval Required</b>\n\n"
                f"<pre>{_html.escape(cmd_preview)}</pre>\n\n"
                f"Reason: {_html.escape(description)}"
            )

            # Resolve thread context for thread replies
            thread_id = self._metadata_thread_id(metadata)

            # We'll use the message_id as part of callback_data to look up session_key
            # Send a placeholder first, then update — or use a counter.
            # Simpler: use a monotonic counter to generate short IDs.
            import itertools

            if not hasattr(self, "_approval_counter"):
                self._approval_counter = itertools.count(1)
            approval_id = next(self._approval_counter)

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "✅ Allow Once", callback_data=f"ea:once:{approval_id}"
                        ),
                        InlineKeyboardButton(
                            "✅ Session", callback_data=f"ea:session:{approval_id}"
                        ),
                    ],
                    [
                        InlineKeyboardButton("✅ Always", callback_data=f"ea:always:{approval_id}"),
                        InlineKeyboardButton("❌ Deny", callback_data=f"ea:deny:{approval_id}"),
                    ],
                ]
            )

            kwargs: dict[str, Any] = {
                "chat_id": int(chat_id),
                "text": text,
                "parse_mode": ParseMode.HTML,
                "reply_markup": keyboard,
                **self._link_preview_kwargs(),
            }
            message_thread_id = self._message_thread_id_for_send(thread_id)
            if message_thread_id is not None:
                kwargs["message_thread_id"] = message_thread_id

            msg = await self._bot.send_message(**kwargs)

            # Store session_key keyed by approval_id for the callback handler
            self._approval_state[approval_id] = session_key

            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.warning("[%s] send_exec_approval failed: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    async def send_model_picker(
        self,
        chat_id: str,
        providers: list,
        current_model: str,
        current_provider: str,
        session_key: str,
        on_model_selected,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send an interactive inline-keyboard model picker.

        Two-step drill-down: provider selection → model selection.
        Edits the same message in-place as the user navigates.
        """
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        from olano.models import provider_label as get_label

        try:
            # Build provider buttons — 2 per row
            buttons: list = []
            for p in providers:
                count = p.get("total_models", len(p.get("models", [])))
                label = f"{p['name']} ({count})"
                if p.get("is_current"):
                    label = f"✓ {label}"
                # Compact callback data: mp:<slug>  (max 64 bytes)
                buttons.append(InlineKeyboardButton(label, callback_data=f"mp:{p['slug']}"))

            rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
            rows.append([InlineKeyboardButton("✗ Cancel", callback_data="mx")])
            keyboard = InlineKeyboardMarkup(rows)

            provider_label = get_label(current_provider)
            text = (
                f"⚙ *Model Configuration*\n\n"
                f"Current model: `{current_model or 'unknown'}`\n"
                f"Provider: {provider_label}\n\n"
                f"Select a provider:"
            )

            thread_id = metadata.get("thread_id") if metadata else None
            msg = await self._bot.send_message(
                chat_id=int(chat_id),
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
                message_thread_id=int(thread_id) if thread_id else None,
                **self._link_preview_kwargs(),
            )

            # Store picker state keyed by chat_id
            self._model_picker_state[str(chat_id)] = {
                "msg_id": msg.message_id,
                "providers": providers,
                "session_key": session_key,
                "on_model_selected": on_model_selected,
                "current_model": current_model,
                "current_provider": current_provider,
            }

            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.warning("[%s] send_model_picker failed: %s", self.name, e)
            return SendResult(success=False, error=str(e))

    _MODEL_PAGE_SIZE = 8

    def _build_model_keyboard(self, models: list, page: int) -> tuple:
        """Build paginated model buttons. Returns (keyboard, page_info_text)."""
        page_size = self._MODEL_PAGE_SIZE
        total = len(models)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))

        start = page * page_size
        end = min(start + page_size, total)
        page_models = models[start:end]

        buttons: list = []
        for i, model_id in enumerate(page_models):
            abs_idx = start + i
            short = model_id.split("/")[-1] if "/" in model_id else model_id
            if len(short) > 38:
                short = short[:35] + "..."
            buttons.append(InlineKeyboardButton(short, callback_data=f"mm:{abs_idx}"))

        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]

        # Pagination row (if needed)
        if total_pages > 1:
            nav: list = []
            if page > 0:
                nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"mg:{page - 1}"))
            nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="mx:noop"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton("Next ▶", callback_data=f"mg:{page + 1}"))
            rows.append(nav)

        rows.append(
            [
                InlineKeyboardButton("◀ Back", callback_data="mb"),
                InlineKeyboardButton("✗ Cancel", callback_data="mx"),
            ]
        )

        page_info = f" ({start + 1}–{end} of {total})" if total_pages > 1 else ""
        return InlineKeyboardMarkup(rows), page_info

    async def _handle_model_picker_callback(self, query, data: str, chat_id: str) -> None:
        """Handle model picker inline keyboard callbacks (mp:/mm:/mb:/mx:/mg:)."""
        state = self._model_picker_state.get(chat_id)
        if not state:
            await query.answer(text="Picker expired — use /model again.")
            return

        from olano.models import provider_label as get_label

        if data.startswith("mp:"):
            # --- Provider selected: show model buttons (page 0) ---
            provider_slug = data[3:]
            provider = next(
                (p for p in state["providers"] if p["slug"] == provider_slug),
                None,
            )
            if not provider:
                await query.answer(text="Provider not found.")
                return

            models = provider.get("models", [])
            state["selected_provider"] = provider_slug
            state["selected_provider_name"] = provider.get("name", provider_slug)
            state["model_list"] = models
            state["model_page"] = 0

            keyboard, page_info = self._build_model_keyboard(models, 0)

            pname = provider.get("name", provider_slug)
            total = provider.get("total_models", len(models))
            shown = len(models)
            extra = (
                f"\n_{total - shown} more available — type `/model <name>` directly_"
                if total > shown
                else ""
            )

            await query.edit_message_text(
                text=(
                    f"⚙ *Model Configuration*\n\n"
                    f"Provider: *{pname}*{page_info}\n"
                    f"Select a model:{extra}"
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )
            await query.answer()

        elif data.startswith("mg:"):
            # --- Page navigation ---
            try:
                page = int(data[3:])
            except ValueError:
                await query.answer(text="Invalid page.")
                return

            models = state.get("model_list", [])
            state["model_page"] = page

            keyboard, page_info = self._build_model_keyboard(models, page)

            pname = state.get("selected_provider_name", "")
            provider_slug = state.get("selected_provider", "")
            provider = next(
                (p for p in state["providers"] if p["slug"] == provider_slug),
                None,
            )
            total = provider.get("total_models", len(models)) if provider else len(models)
            shown = len(models)
            extra = (
                f"\n_{total - shown} more available — type `/model <name>` directly_"
                if total > shown
                else ""
            )

            await query.edit_message_text(
                text=(
                    f"⚙ *Model Configuration*\n\n"
                    f"Provider: *{pname}*{page_info}\n"
                    f"Select a model:{extra}"
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )
            await query.answer()

        elif data.startswith("mm:"):
            # --- Model selected: perform the switch ---
            try:
                idx = int(data[3:])
            except ValueError:
                await query.answer(text="Invalid selection.")
                return

            model_list = state.get("model_list", [])
            if idx < 0 or idx >= len(model_list):
                await query.answer(text="Invalid model index.")
                return

            model_id = model_list[idx]
            provider_slug = state.get("selected_provider", "")
            callback = state.get("on_model_selected")

            if not callback:
                await query.answer(text="Picker expired.")
                return

            try:
                result_text = await callback(chat_id, model_id, provider_slug)
            except Exception as exc:
                logger.error("Model picker switch failed: %s", exc)
                result_text = f"Error switching model: {exc}"

            # Edit message to show confirmation, remove buttons
            try:
                await query.edit_message_text(
                    text=result_text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=None,
                )
            except Exception:
                # Markdown parse failure — retry as plain text
                try:
                    await query.edit_message_text(
                        text=result_text,
                        parse_mode=None,
                        reply_markup=None,
                    )
                except Exception:
                    pass
            await query.answer(text="Model switched!")

            # Clean up state
            self._model_picker_state.pop(chat_id, None)

        elif data == "mb":
            # --- Back to provider list ---
            buttons = []
            for p in state["providers"]:
                count = p.get("total_models", len(p.get("models", [])))
                label = f"{p['name']} ({count})"
                if p.get("is_current"):
                    label = f"✓ {label}"
                buttons.append(InlineKeyboardButton(label, callback_data=f"mp:{p['slug']}"))

            rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
            rows.append([InlineKeyboardButton("✗ Cancel", callback_data="mx")])
            keyboard = InlineKeyboardMarkup(rows)

            try:
                provider_label = get_label(state["current_provider"])
            except Exception:
                provider_label = state["current_provider"]

            await query.edit_message_text(
                text=(
                    f"⚙ *Model Configuration*\n\n"
                    f"Current model: `{state['current_model'] or 'unknown'}`\n"
                    f"Provider: {provider_label}\n\n"
                    f"Select a provider:"
                ),
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
            )
            await query.answer()

        elif data == "mx":
            # --- Cancel ---
            self._model_picker_state.pop(chat_id, None)
            await query.edit_message_text(
                text="Model selection cancelled.",
                reply_markup=None,
            )
            await query.answer()

        else:
            # Catch-all (e.g. page counter button "mx:noop")
            await query.answer()

    async def _handle_callback_query(
        self, update: "Update", context: "ContextTypes.DEFAULT_TYPE"
    ) -> None:
        """Handle inline keyboard button clicks."""
        query = update.callback_query
        if not query or not query.data:
            return
        data = query.data

        # --- Model picker callbacks ---
        if data.startswith(("mp:", "mm:", "mb", "mx", "mg:")):
            chat_id = (
                str(query.message.chat_id)  # type: ignore[attr-defined]  # ptb stub gap on MaybeInaccessibleMessage
                if query.message
                else None
            )
            if chat_id:
                await self._handle_model_picker_callback(query, data, chat_id)
            return

        # --- Exec approval callbacks (ea:choice:id) ---
        if data.startswith("ea:"):
            parts = data.split(":", 2)
            if len(parts) == 3:
                choice = parts[1]  # once, session, always, deny
                try:
                    approval_id = int(parts[2])
                except (ValueError, IndexError):
                    await query.answer(text="Invalid approval data.")
                    return

                # Only authorized users may click approval buttons.
                caller_id = str(getattr(query.from_user, "id", ""))
                if not self._is_callback_user_authorized(caller_id):
                    await query.answer(text="⛔ You are not authorized to approve commands.")
                    return

                session_key = self._approval_state.pop(approval_id, None)
                if not session_key:
                    await query.answer(text="This approval has already been resolved.")
                    return

                # Map choice to human-readable label
                label_map = {
                    "once": "✅ Approved once",
                    "session": "✅ Approved for session",
                    "always": "✅ Approved permanently",
                    "deny": "❌ Denied",
                }
                user_display = getattr(query.from_user, "first_name", "User")
                label = label_map.get(choice, "Resolved")

                await query.answer(text=label)

                # Edit message to show decision, remove buttons
                try:
                    await query.edit_message_text(
                        text=f"{label} by {user_display}",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=None,
                    )
                except Exception:
                    pass  # non-fatal if edit fails

                # Resolve the approval — unblocks the agent thread
                try:
                    from olano.platforms.approval import resolve_gateway_approval

                    count = resolve_gateway_approval(session_key, choice)
                    logger.info(
                        "Telegram button resolved %d approval(s) for session %s (choice=%s, user=%s)",
                        count,
                        session_key,
                        choice,
                        user_display,
                    )
                except Exception as exc:
                    logger.error("Failed to resolve gateway approval from Telegram button: %s", exc)
            return

        # --- Update prompt callbacks ---
        if not data.startswith("update_prompt:"):
            return
        answer = data.split(":", 1)[1]  # "y" or "n"
        caller_id = str(getattr(query.from_user, "id", ""))
        if not self._is_callback_user_authorized(caller_id):
            await query.answer(text="⛔ You are not authorized to answer update prompts.")
            return
        await query.answer(text=f"Sent '{answer}' to the update process.")
        # Edit the message to show the choice and remove buttons
        label = "Yes" if answer == "y" else "No"
        try:
            await query.edit_message_text(
                text=f"⚕ Update prompt answered: *{label}*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=None,
            )
        except Exception:
            pass  # non-fatal if edit fails
        # Write the response file
        try:
            from olano.config.home import get_olano_home

            home = get_olano_home()
            response_path = home / ".update_response"
            tmp = response_path.with_suffix(".tmp")
            tmp.write_text(answer)
            tmp.replace(response_path)
            logger.info(
                "Telegram update prompt answered '%s' by user %s",
                answer,
                getattr(query.from_user, "id", "unknown"),
            )
        except Exception as exc:
            logger.error("Failed to write update response from callback: %s", exc)

    # ── Telegram Stars payments (creator monetization) ────────────────────
    # Ported from the deprecated classic channel adapter so monetized agents
    # work on platform:telegram. See olano/monetization/ for the gate,
    # providers, and entitlement store these handlers feed.

    async def _handle_pre_checkout(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Approve a Stars checkout iff its payload is one of ours."""
        query = update.pre_checkout_query
        if query is None:
            return
        from olano.monetization.providers.stripe_provider import decode_reference

        ok = decode_reference(query.invoice_payload or "") is not None
        try:
            await query.answer(ok=ok, error_message=None if ok else "Invalid subscription.")
        except Exception as e:
            logger.warning("[%s] Telegram pre_checkout answer failed: %s", self.name, e)

    async def _handle_successful_payment(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Record a completed Stars payment as an active entitlement."""
        msg = update.message
        sp = getattr(msg, "successful_payment", None) if msg else None
        if sp is None:
            return
        from olano.monetization.providers.stars_provider import TelegramStarsProvider
        from olano.monetization.service import apply_entitlement_update

        payload = {
            "invoice_payload": getattr(sp, "invoice_payload", ""),
            "telegram_payment_charge_id": getattr(sp, "telegram_payment_charge_id", ""),
            "subscription_expiration_date": _dt_to_epoch(
                getattr(sp, "subscription_expiration_date", None)
            ),
        }
        provider = TelegramStarsProvider(agent=self.agent_name or "", plan=None)
        payment_update = provider.parse_event(payload)
        if payment_update is None:
            logger.warning("[%s] Telegram successful_payment with unrecognized payload", self.name)
            return
        try:
            apply_entitlement_update(payment_update)
        except Exception as e:
            logger.error("[%s] Failed to record Stars payment: %s", self.name, e, exc_info=True)
        try:
            if msg is not None:
                await self.send(
                    str(msg.chat_id),
                    "✅ Subscription active — thanks! You now have full access.",
                )
        except Exception as e:
            logger.debug("[%s] Could not confirm payment: %s", self.name, e)

    async def send_subscription_offer(self, recipient: str, offer: Any) -> bool:
        """Present a subscription offer in-chat; return True if anything sent.

        Renders whatever rails the offer carries: a Stripe Checkout URL becomes
        an inline "Subscribe" button; a Telegram Stars invoice is sent natively
        via ``send_invoice`` (recurring, ``currency="XTR"``). Both can appear.
        """
        if self._bot is None:
            return False
        sent = False
        text = getattr(offer, "text", "") or "Subscribe to continue."
        stripe_url = getattr(offer, "stripe_url", "") or ""
        stars_invoice = getattr(offer, "stars_invoice", None)

        if stripe_url:
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("💳 Subscribe", url=stripe_url)]]
            )
            try:
                await self._bot.send_message(
                    chat_id=int(recipient), text=text, reply_markup=keyboard
                )
                sent = True
            except Exception as e:
                logger.warning("[%s] Could not send Stripe offer: %s", self.name, e)

        if stars_invoice:
            from telegram import LabeledPrice

            try:
                prices = [
                    LabeledPrice(str(p["label"]), int(p["amount"]))
                    for p in stars_invoice.get("prices", [])
                ]
                title = stars_invoice.get("title", "Subscription")
                description = stars_invoice.get("description", "") or "Subscribe for full access."
                payload = stars_invoice.get("payload", "")
                currency = stars_invoice.get("currency", "XTR")
                subscription_period = stars_invoice.get("subscription_period")
                if subscription_period:
                    # Recurring Stars subscriptions only exist on
                    # createInvoiceLink — sendInvoice has no
                    # subscription_period parameter (the legacy channel
                    # passed it anyway and always crashed into this except).
                    link = await self._bot.create_invoice_link(
                        title=title,
                        description=description,
                        payload=payload,
                        provider_token="",  # empty for Telegram Stars (XTR)
                        currency=currency,
                        prices=prices,
                        subscription_period=int(subscription_period),
                    )
                    await self._bot.send_message(
                        chat_id=int(recipient),
                        text=f"⭐ {title} — {description}",
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("⭐ Subscribe with Stars", url=link)]]
                        ),
                    )
                else:
                    await self._bot.send_invoice(
                        chat_id=int(recipient),
                        title=title,
                        description=description,
                        payload=payload,
                        provider_token="",  # empty for Telegram Stars (XTR)
                        currency=currency,
                        prices=prices,
                    )
                sent = True
            except Exception as e:
                logger.warning("[%s] Could not send Stars invoice: %s", self.name, e)

        return sent

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        as_voice: bool = True,
        **kwargs,
    ) -> SendResult:
        """Send audio as a native Telegram voice message or audio file.

        Telegram only renders the round playable bubble for Ogg/Opus, so the
        container decides: ``sendVoice`` for ``.ogg``/``.opus``, ``sendAudio``
        (a file card in the chat) for everything else. ``as_voice=False`` —
        an attached audio file with no ``[[audio_as_voice]]`` directive —
        forces the file card even for Ogg.
        """
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            import os

            from olano.speech import VOICE_NOTE_EXTENSIONS

            if not os.path.exists(audio_path):
                return SendResult(success=False, error=f"Audio file not found: {audio_path}")

            _plain_chat, _biz_conn = self._parse_chat_ref(chat_id)
            _is_voice_note = as_voice and _Path(audio_path).suffix.lower() in VOICE_NOTE_EXTENSIONS
            with open(audio_path, "rb") as audio_file:
                # .ogg files -> send as voice (round playable bubble)
                if _is_voice_note:
                    _voice_thread = self._metadata_thread_id(metadata)
                    msg = await self._bot.send_voice(
                        chat_id=int(_plain_chat),
                        voice=audio_file,
                        caption=caption[:1024] if caption else None,
                        reply_to_message_id=int(reply_to) if reply_to else None,
                        message_thread_id=self._message_thread_id_for_send(_voice_thread),
                        business_connection_id=_biz_conn,
                    )
                else:
                    # .mp3 and others -> send as audio file
                    _audio_thread = self._metadata_thread_id(metadata)
                    msg = await self._bot.send_audio(
                        chat_id=int(_plain_chat),
                        audio=audio_file,
                        caption=caption[:1024] if caption else None,
                        reply_to_message_id=int(reply_to) if reply_to else None,
                        message_thread_id=self._message_thread_id_for_send(_audio_thread),
                        business_connection_id=_biz_conn,
                    )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.error(
                "[%s] Failed to send Telegram voice/audio, falling back to base adapter: %s",
                self.name,
                e,
                exc_info=True,
            )
            return await super().send_voice(
                chat_id, audio_path, caption, reply_to, as_voice=as_voice
            )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file natively as a Telegram photo."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            import os

            if not os.path.exists(image_path):
                return SendResult(success=False, error=f"Image file not found: {image_path}")

            _thread = self._metadata_thread_id(metadata)
            _plain_chat, _biz_conn = self._parse_chat_ref(chat_id)
            with open(image_path, "rb") as image_file:
                msg = await self._bot.send_photo(
                    chat_id=int(_plain_chat),
                    photo=image_file,
                    caption=caption[:1024] if caption else None,
                    reply_to_message_id=int(reply_to) if reply_to else None,
                    message_thread_id=self._message_thread_id_for_send(_thread),
                    business_connection_id=_biz_conn,
                )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.error(
                "[%s] Failed to send Telegram local image, falling back to base adapter: %s",
                self.name,
                e,
                exc_info=True,
            )
            return await super().send_image_file(chat_id, image_path, caption, reply_to)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a document/file natively as a Telegram file attachment."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            if not os.path.exists(file_path):
                return SendResult(success=False, error=f"File not found: {file_path}")

            display_name = file_name or os.path.basename(file_path)
            _thread = self._metadata_thread_id(metadata)
            _plain_chat, _biz_conn = self._parse_chat_ref(chat_id)

            with open(file_path, "rb") as f:
                msg = await self._bot.send_document(
                    chat_id=int(_plain_chat),
                    document=f,
                    filename=display_name,
                    caption=caption[:1024] if caption else None,
                    reply_to_message_id=int(reply_to) if reply_to else None,
                    message_thread_id=self._message_thread_id_for_send(_thread),
                    business_connection_id=_biz_conn,
                )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            print(f"[{self.name}] Failed to send document: {e}")
            return await super().send_document(chat_id, file_path, caption, file_name, reply_to)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video natively as a Telegram video message."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            if not os.path.exists(video_path):
                return SendResult(success=False, error=f"Video file not found: {video_path}")

            _thread = self._metadata_thread_id(metadata)
            _plain_chat, _biz_conn = self._parse_chat_ref(chat_id)
            with open(video_path, "rb") as f:
                msg = await self._bot.send_video(
                    chat_id=int(_plain_chat),
                    video=f,
                    caption=caption[:1024] if caption else None,
                    reply_to_message_id=int(reply_to) if reply_to else None,
                    message_thread_id=self._message_thread_id_for_send(_thread),
                    business_connection_id=_biz_conn,
                )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            print(f"[{self.name}] Failed to send video: {e}")
            return await super().send_video(chat_id, video_path, caption, reply_to)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image natively as a Telegram photo.

        Tries URL-based send first (fast, works for <5MB images).
        Falls back to downloading and uploading as file (supports up to 10MB).
        """
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        from olano.platforms.url_safety import is_safe_url

        if not is_safe_url(image_url):
            logger.warning("[%s] Blocked unsafe image URL (SSRF protection)", self.name)
            return await super().send_image(
                chat_id, image_url, caption, reply_to, metadata=metadata
            )

        _plain_chat, _biz_conn = self._parse_chat_ref(chat_id)
        try:
            # Telegram can send photos directly from URLs (up to ~5MB)
            _photo_thread = self._metadata_thread_id(metadata)
            msg = await self._bot.send_photo(
                chat_id=int(_plain_chat),
                photo=image_url,
                caption=caption[:1024] if caption else None,  # Telegram caption limit
                reply_to_message_id=int(reply_to) if reply_to else None,
                message_thread_id=self._message_thread_id_for_send(_photo_thread),
                business_connection_id=_biz_conn,
            )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.warning(
                "[%s] URL-based send_photo failed, trying file upload: %s",
                self.name,
                e,
                exc_info=True,
            )
            # Fallback: download and upload as file (supports up to 10MB)
            try:
                import httpx

                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.get(image_url)
                    resp.raise_for_status()
                    image_data = resp.content

                msg = await self._bot.send_photo(
                    chat_id=int(_plain_chat),
                    photo=image_data,
                    caption=caption[:1024] if caption else None,
                    reply_to_message_id=int(reply_to) if reply_to else None,
                    message_thread_id=self._message_thread_id_for_send(_photo_thread),
                    business_connection_id=_biz_conn,
                )
                return SendResult(success=True, message_id=str(msg.message_id))
            except Exception as e2:
                logger.error(
                    "[%s] File upload send_photo also failed: %s",
                    self.name,
                    e2,
                    exc_info=True,
                )
                # Final fallback: send URL as text
                return await super().send_image(chat_id, image_url, caption, reply_to)

    async def send_animation(
        self,
        chat_id: str,
        animation_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        """Send an animated GIF natively as a Telegram animation (auto-plays inline)."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            _anim_thread = self._metadata_thread_id(metadata)
            _plain_chat, _biz_conn = self._parse_chat_ref(chat_id)
            msg = await self._bot.send_animation(
                chat_id=int(_plain_chat),
                animation=animation_url,
                caption=caption[:1024] if caption else None,
                reply_to_message_id=int(reply_to) if reply_to else None,
                message_thread_id=self._message_thread_id_for_send(_anim_thread),
                business_connection_id=_biz_conn,
            )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.error(
                "[%s] Failed to send Telegram animation, falling back to photo: %s",
                self.name,
                e,
                exc_info=True,
            )
            # Fallback: try as a regular photo
            return await self.send_image(chat_id, animation_url, caption, reply_to)

    async def send_typing(self, chat_id: str, metadata: Optional[dict[str, Any]] = None) -> None:
        """Send typing indicator."""
        if self._bot:
            try:
                _typing_thread = self._metadata_thread_id(metadata)
                message_thread_id = self._message_thread_id_for_typing(_typing_thread)
                _plain_chat, _biz_conn = self._parse_chat_ref(chat_id)
                try:
                    await self._bot.send_chat_action(
                        chat_id=int(_plain_chat),
                        action="typing",
                        message_thread_id=message_thread_id,
                        business_connection_id=_biz_conn,
                    )
                except Exception as e:
                    if message_thread_id is not None and self._is_thread_not_found_error(e):
                        await self._bot.send_chat_action(
                            chat_id=int(_plain_chat),
                            action="typing",
                            message_thread_id=None,
                            business_connection_id=_biz_conn,
                        )
                    else:
                        raise
            except Exception as e:
                # Typing failures are non-fatal; log at debug level only.
                logger.debug(
                    "[%s] Failed to send Telegram typing indicator: %s",
                    self.name,
                    e,
                    exc_info=True,
                )

    async def _handle_my_chat_member(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Record this bot being added to / removed from a chat.

        ``my_chat_member`` fires the moment someone adds the bot to a group
        or channel — before any message is sent there — which is the only
        way a Telegram bot can learn a silent chat's id (the Bot API has no
        chat enumeration).
        """
        try:
            cm = update.my_chat_member
            if cm is None or cm.new_chat_member is None:
                return
            status = str(getattr(cm.new_chat_member, "status", "") or "")
            joined = status in {"member", "administrator", "creator", "restricted"}
            left = status in {"left", "kicked"}
            if not joined and not left:
                return
            chat = cm.chat
            chat_type = "group"
            if ChatType is not None:
                if chat.type == ChatType.CHANNEL:
                    chat_type = "channel"
                elif chat.type == ChatType.PRIVATE:
                    chat_type = "dm"
            from olano.platforms.chat_directory import record_membership_event

            record_membership_event(
                self,
                str(chat.id),
                chat_name=str(chat.title or getattr(chat, "full_name", "") or ""),
                chat_type=chat_type,
                joined=joined,
            )
            logger.info(
                "[%s] Bot %s %s '%s' (id=%s)",
                self.name,
                "joined" if joined else "left",
                chat_type,
                chat.title or chat.id,
                chat.id,
            )
        except Exception as e:
            logger.debug("[%s] my_chat_member handling failed: %s", self.name, e)

    async def _handle_chat_migration(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Record group → supergroup chat-id migrations in the chat directory."""
        try:
            msg = update.effective_message
            if msg is None:
                return
            from olano.platforms.chat_directory import record_migration_event

            if getattr(msg, "migrate_to_chat_id", None):
                record_migration_event(self, str(msg.chat.id), str(msg.migrate_to_chat_id))
            elif getattr(msg, "migrate_from_chat_id", None):
                record_migration_event(self, str(msg.migrate_from_chat_id), str(msg.chat.id))
        except Exception as e:
            logger.debug("[%s] chat migration handling failed: %s", self.name, e)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """Get information about a Telegram chat (or a ``chat:thread`` topic ref)."""
        if not self._bot:
            return {"name": "Unknown", "type": "dm"}

        # A forum-topic ref describes its parent chat; the Bot API has no
        # topic-info endpoint, so annotate the thread id on the parent's info.
        topic_ref, topic_thread = self._split_topic_ref(chat_id)
        if topic_thread is not None:
            info = await self.get_chat_info(topic_ref)
            info["topic_thread_id"] = topic_thread
            if not info.get("error"):
                info["type"] = "topic"
            return info

        try:
            _plain_chat, _ = self._parse_chat_ref(chat_id)
            chat = await self._bot.get_chat(int(_plain_chat))

            chat_type = "dm"
            if chat.type == ChatType.GROUP:
                chat_type = "group"
            elif chat.type == ChatType.SUPERGROUP:
                chat_type = "group"
                if chat.is_forum:
                    chat_type = "forum"
            elif chat.type == ChatType.CHANNEL:
                chat_type = "channel"

            return {
                "name": chat.title or chat.full_name or str(chat_id),
                "type": chat_type,
                "username": chat.username,
                "is_forum": getattr(chat, "is_forum", False),
            }
        except Exception as e:
            logger.error(
                "[%s] Failed to get Telegram chat info for %s: %s",
                self.name,
                chat_id,
                e,
                exc_info=True,
            )
            return {"name": str(chat_id), "type": "dm", "error": str(e)}

    def format_message(self, content: str) -> str:
        """
        Convert standard markdown to Telegram MarkdownV2 format.

        Protected regions (code blocks, inline code) are extracted first so
        their contents are never modified.  Standard markdown constructs
        (headers, bold, italic, links) are translated to MarkdownV2 syntax,
        and all remaining special characters are escaped.
        """
        if not content:
            return content

        placeholders: dict = {}
        counter = [0]

        def _ph(value: str) -> str:
            """Stash *value* behind a placeholder token that survives escaping."""
            key = f"\x00PH{counter[0]}\x00"
            counter[0] += 1
            placeholders[key] = value
            return key

        text = content

        # 0) Pre-wrap GFM-style pipe tables in ``` fences.  Telegram can't
        #    render tables natively, but fenced code blocks render as
        #    monospace preformatted text with columns intact.  The wrapped
        #    tables then flow through step (1) below as protected regions.
        text = _wrap_markdown_tables(text)

        # 1) Protect fenced code blocks (``` ... ```)
        #    Per MarkdownV2 spec, \ and ` inside pre/code must be escaped.
        def _protect_fenced(m):
            raw = m.group(0)
            # Split off opening ``` (with optional language) and closing ```
            open_end = raw.index("\n") + 1 if "\n" in raw[3:] else 3
            opening = raw[:open_end]
            body_and_close = raw[open_end:]
            body = body_and_close[:-3]
            body = body.replace("\\", "\\\\").replace("`", "\\`")
            return _ph(opening + body + "```")

        text = re.sub(
            r"(```(?:[^\n]*\n)?[\s\S]*?```)",
            _protect_fenced,
            text,
        )

        # 2) Protect inline code (`...`)
        #    Escape \ inside inline code per MarkdownV2 spec.
        text = re.sub(
            r"(`[^`]+`)",
            lambda m: _ph(m.group(0).replace("\\", "\\\\")),
            text,
        )

        # 3) Convert markdown links – escape the display text; inside the URL
        #    only ')' and '\' need escaping per the MarkdownV2 spec.
        def _convert_link(m):
            display = _escape_mdv2(m.group(1))
            url = m.group(2).replace("\\", "\\\\").replace(")", "\\)")
            return _ph(f"[{display}]({url})")

        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _convert_link, text)

        # 4) Convert markdown headers (## Title) → bold *Title*
        def _convert_header(m):
            inner = m.group(1).strip()
            # Strip redundant bold markers that may appear inside a header
            inner = re.sub(r"\*\*(.+?)\*\*", r"\1", inner)
            return _ph(f"*{_escape_mdv2(inner)}*")

        text = re.sub(r"^#{1,6}\s+(.+)$", _convert_header, text, flags=re.MULTILINE)

        # 5) Convert bold: **text** → *text* (MarkdownV2 bold)
        text = re.sub(
            r"\*\*(.+?)\*\*",
            lambda m: _ph(f"*{_escape_mdv2(m.group(1))}*"),
            text,
        )

        # 6) Convert italic: *text* (single asterisk) → _text_ (MarkdownV2 italic)
        #    [^*\n]+ prevents matching across newlines (which would corrupt
        #    bullet lists using * markers and multi-line content).
        text = re.sub(
            r"\*([^*\n]+)\*",
            lambda m: _ph(f"_{_escape_mdv2(m.group(1))}_"),
            text,
        )

        # 7) Convert strikethrough: ~~text~~ → ~text~ (MarkdownV2)
        text = re.sub(
            r"~~(.+?)~~",
            lambda m: _ph(f"~{_escape_mdv2(m.group(1))}~"),
            text,
        )

        # 8) Convert spoiler: ||text|| → ||text|| (protect from | escaping)
        text = re.sub(
            r"\|\|(.+?)\|\|",
            lambda m: _ph(f"||{_escape_mdv2(m.group(1))}||"),
            text,
        )

        # 9) Convert blockquotes: > at line start → protect > from escaping
        #    Handle both regular blockquotes (> text) and expandable blockquotes
        #    (Telegram MarkdownV2: **> for expandable start, || to end the quote)
        def _convert_blockquote(m):
            prefix = m.group(1)  # >, >>, >>>, **>, or **>> etc.
            content = m.group(2)
            # Check if content ends with || (expandable blockquote end marker)
            # In this case, preserve the trailing || unescaped for Telegram
            if prefix.startswith("**") and content.endswith("||"):
                return _ph(f"{prefix} {_escape_mdv2(content[:-2])}||")
            return _ph(f"{prefix} {_escape_mdv2(content)}")

        text = re.sub(
            r"^((?:\*\*)?>{1,3}) (.+)$",
            _convert_blockquote,
            text,
            flags=re.MULTILINE,
        )

        # 10) Escape remaining special characters in plain text
        text = _escape_mdv2(text)

        # 11) Restore placeholders in reverse insertion order so that
        #    nested references (a placeholder inside another) resolve correctly.
        for key in reversed(list(placeholders.keys())):
            text = text.replace(key, placeholders[key])

        # 12) Safety net: escape unescaped ( ) { } that slipped through
        #     placeholder processing.  Split the text into code/non-code
        #     segments so we never touch content inside ``` or ` spans.
        _code_split = re.split(r"(```[\s\S]*?```|`[^`]+`)", text)
        _safe_parts = []
        for _idx, _seg in enumerate(_code_split):
            if _idx % 2 == 1:
                # Inside code span/block — leave untouched
                _safe_parts.append(_seg)
            else:
                # Outside code — escape bare ( ) { }
                def _esc_bare(m, _seg=_seg):
                    s = m.start()
                    ch = m.group(0)
                    # Already escaped
                    if s > 0 and _seg[s - 1] == "\\":
                        return ch
                    # ( that opens a MarkdownV2 link [text](url)
                    if ch == "(" and s > 0 and _seg[s - 1] == "]":
                        return ch
                    # ) that closes a link URL
                    if ch == ")":
                        before = _seg[:s]
                        if "](http" in before or "](" in before:
                            # Check depth
                            depth = 0
                            for j in range(s - 1, max(s - 2000, -1), -1):
                                if _seg[j] == "(":
                                    depth -= 1
                                    if depth < 0:
                                        if j > 0 and _seg[j - 1] == "]":
                                            return ch
                                        break
                                elif _seg[j] == ")":
                                    depth += 1
                    return "\\" + ch

                _safe_parts.append(re.sub(r"[(){}]", _esc_bare, _seg))
        text = "".join(_safe_parts)

        return text

    # ── Group mention gating ──────────────────────────────────────────────

    def _telegram_require_mention(self) -> bool:
        """Return whether group chats should require an explicit bot trigger."""
        configured = self.config.extra.get("require_mention")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in ("true", "1", "yes", "on")
            return bool(configured)
        return os.getenv("TELEGRAM_REQUIRE_MENTION", "false").lower() in ("true", "1", "yes", "on")

    def _telegram_group_respond(self) -> str:
        """Effective ``group_respond`` mode ("all" | "mentions" | "none").

        ``group_respond`` wins when set; otherwise the legacy
        ``require_mention`` resolution decides (Telegram default: "all").
        """
        from olano.platforms.group_modes import resolve_group_respond

        return resolve_group_respond(
            self.config.extra, legacy_require_mention=self._telegram_require_mention()
        )

    def _telegram_group_listen(self) -> str:
        """Effective ``group_listen`` mode ("off" | "mentions" | "all").

        ``group_listen`` wins when set; the legacy ``passive_listen`` toggle
        maps to "all".
        """
        from olano.platforms.group_modes import resolve_group_listen

        return resolve_group_listen(
            self.config.extra, legacy_passive_listen=self._telegram_passive_listen()
        )

    def _telegram_group_policy(self) -> str:
        """Who in a group may reach the agent at all.

        - ``allowlist`` (default): current behaviour — with ``allowed_users``
          configured, only those senders get through anywhere.
        - ``open``: group members bypass the sender allowlist (DMs stay
          gated). They are still non-owners: the runtime envelopes their text
          as untrusted and the security pipeline denies mutating tools in
          groups — this only opens conversation, not control. The setting that
          makes "everyone may talk in the group, only the owner commands"
          expressible, mirroring WeCom's ``group_policy``.
        - ``disabled``: drop every group message.
        """
        raw = self.config.extra.get("group_policy")
        if raw is None:
            raw = os.getenv("TELEGRAM_GROUP_POLICY", "")
        value = str(raw or "").strip().lower()
        return value if value in ("open", "allowlist", "disabled") else "allowlist"

    def _telegram_passive_listen(self) -> bool:
        """Whether unaddressed group messages are buffered as observed context
        (``extra.passive_listen``) instead of dropped without trace."""
        configured = self.config.extra.get("passive_listen")
        if configured is not None:
            if isinstance(configured, str):
                return configured.lower() in ("true", "1", "yes", "on")
            return bool(configured)
        return os.getenv("TELEGRAM_PASSIVE_LISTEN", "false").lower() in ("true", "1", "yes", "on")

    def _observed_buffer(self) -> Any:
        """Lazy per-adapter ObservedConversationBuffer (None when off).

        The in-RAM buffer feeds observed context into the agent's next
        addressed turn; it runs whenever the effective ``group_listen`` mode
        is "all" (which legacy ``passive_listen`` maps to). The persistent
        store in ``_maybe_record_observed`` is independent of this buffer.
        """
        if self._telegram_group_listen() != "all":
            return None
        buf = getattr(self, "_observed_conversations", None)
        if buf is None:
            from olano.platforms.group_listen import ObservedConversationBuffer

            try:
                max_messages = int(self.config.extra.get("passive_listen_max_messages") or 30)
            except (TypeError, ValueError):
                max_messages = 30
            buf = ObservedConversationBuffer(max_messages=max_messages)
            self._observed_conversations = buf
        return buf

    def _observed_chat_key(self, message: Message) -> str:
        chat_id = str(getattr(getattr(message, "chat", None), "id", "") or "")
        thread_id = getattr(message, "message_thread_id", None)
        return f"{chat_id}:{thread_id}" if thread_id is not None else chat_id

    def _maybe_record_observed(self, message: Message) -> None:
        """Record a group message the gate declined to answer.

        Two sinks, per the effective ``group_listen`` mode:
        - the persistent observed-messages store (``chat_directory``) —
          queryable later via ``chat_messages_recent`` / ``chat_messages_search``
          ("mentions" records only messages that addressed the bot; "all"
          records everything the bot could see);
        - the in-RAM buffer that surfaces as context on the next addressed
          turn (mode "all" only).
        """
        if not self._is_group_chat(message):
            return
        listen = self._telegram_group_listen()
        if listen == "off":
            return
        text = str(getattr(message, "text", "") or getattr(message, "caption", "") or "")
        if not text:
            return
        if not self._bot_sender_check(message):
            return
        if listen == "mentions":
            mentioned = (
                self._is_reply_to_bot(message)
                or self._message_mentions_bot(message)
                or self._message_matches_mention_patterns(message)
            )
            if not mentioned:
                return
        try:
            from olano.platforms.chat_directory import record_observed_event

            chat = message.chat
            user = getattr(message, "from_user", None)
            thread_raw = getattr(message, "message_thread_id", None)
            thread_id = (
                str(thread_raw)
                if thread_raw is not None
                else (self._GENERAL_TOPIC_THREAD_ID if getattr(chat, "is_forum", False) else None)
            )
            source = self.build_source(
                chat_id=str(chat.id),
                chat_name=getattr(chat, "title", None),
                chat_type="group",
                user_id=str(user.id) if user else None,
                user_name=getattr(user, "full_name", None) if user else None,
                username=getattr(user, "username", None) if user else None,
                thread_id=thread_id,
                is_bot=bool(getattr(user, "is_bot", False)) if user else False,
            )
            record_observed_event(self, source, text)
        except Exception as e:
            logger.debug("[%s] observed-store capture failed: %s", self.name, e)
        buf = self._observed_buffer()
        if buf is None:
            return
        user = getattr(message, "from_user", None)
        handle = str(getattr(user, "username", "") or "").strip()
        name = str(getattr(user, "full_name", "") or "").strip()
        label = f"@{handle}" if handle else (name or "someone")
        buf.record(self._observed_chat_key(message), label, text)

    def _attach_observed_context(self, message: Message, text: str) -> str:
        """Prepend (and clear) any observed-context block for this chat."""
        buf = self._observed_buffer()
        if buf is None or not self._is_group_chat(message):
            return text
        block = buf.drain(self._observed_chat_key(message))
        return f"{block}\n\n{text}" if block else text

    def _telegram_free_response_chats(self) -> set[str]:
        raw = self.config.extra.get("free_response_chats")
        if raw is None:
            raw = os.getenv("TELEGRAM_FREE_RESPONSE_CHATS", "")
        if isinstance(raw, list):
            return {str(part).strip() for part in raw if str(part).strip()}
        return {part.strip() for part in str(raw).split(",") if part.strip()}

    def _telegram_ignored_threads(self) -> set[int]:
        raw = self.config.extra.get("ignored_threads")
        if raw is None:
            raw = os.getenv("TELEGRAM_IGNORED_THREADS", "")

        values = raw if isinstance(raw, list) else str(raw).split(",")

        ignored: set[int] = set()
        for value in values:
            text = str(value).strip()
            if not text:
                continue
            try:
                ignored.add(int(text))
            except (TypeError, ValueError):
                logger.warning("[%s] Ignoring invalid Telegram thread id: %r", self.name, value)
        return ignored

    def _compile_mention_patterns(self) -> list[re.Pattern]:
        """Compile optional regex wake-word patterns for group triggers."""
        patterns = self.config.extra.get("mention_patterns")
        if patterns is None:
            raw = os.getenv("TELEGRAM_MENTION_PATTERNS", "").strip()
            if raw:
                try:
                    loaded = json.loads(raw)
                except Exception:
                    loaded = [part.strip() for part in raw.splitlines() if part.strip()]
                    if not loaded:
                        loaded = [part.strip() for part in raw.split(",") if part.strip()]
                patterns = loaded

        if patterns is None:
            return []
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list):
            logger.warning(
                "[%s] telegram mention_patterns must be a list or string; got %s",
                self.name,
                type(patterns).__name__,
            )
            return []

        compiled: list[re.Pattern] = []
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning(
                    "[%s] Invalid Telegram mention pattern %r: %s", self.name, pattern, exc
                )
        if compiled:
            logger.info("[%s] Loaded %d Telegram mention pattern(s)", self.name, len(compiled))
        return compiled

    def _is_group_chat(self, message: Message) -> bool:
        chat = getattr(message, "chat", None)
        if not chat:
            return False
        chat_type = str(getattr(chat, "type", "")).split(".")[-1].lower()
        return chat_type in ("group", "supergroup")

    def _is_reply_to_bot(self, message: Message) -> bool:
        if not self._bot or not getattr(message, "reply_to_message", None):
            return False
        reply_user = getattr(message.reply_to_message, "from_user", None)
        return bool(
            reply_user and getattr(reply_user, "id", None) == getattr(self._bot, "id", None)
        )

    def _message_mentions_bot(self, message: Message) -> bool:
        if not self._bot:
            return False

        bot_username = (getattr(self._bot, "username", None) or "").lstrip("@").lower()
        bot_id = getattr(self._bot, "id", None)

        def _iter_sources():
            yield getattr(message, "text", None) or "", getattr(message, "entities", None) or []
            yield (
                getattr(message, "caption", None) or "",
                getattr(message, "caption_entities", None) or [],
            )

        for source_text, entities in _iter_sources():
            if bot_username and f"@{bot_username}" in source_text.lower():
                return True
            for entity in entities:
                entity_type = str(getattr(entity, "type", "")).split(".")[-1].lower()
                if entity_type == "mention" and bot_username:
                    offset = int(getattr(entity, "offset", -1))
                    length = int(getattr(entity, "length", 0))
                    if offset < 0 or length <= 0:
                        continue
                    if source_text[offset : offset + length].strip().lower() == f"@{bot_username}":
                        return True
                elif entity_type == "text_mention":
                    user = getattr(entity, "user", None)
                    if user and getattr(user, "id", None) == bot_id:
                        return True
        return False

    def _message_matches_mention_patterns(self, message: Message) -> bool:
        if not self._mention_patterns:
            return False
        for candidate in (getattr(message, "text", None), getattr(message, "caption", None)):
            if not candidate:
                continue
            for pattern in self._mention_patterns:
                if pattern.search(candidate):
                    return True
        return False

    def _clean_bot_trigger_text(self, text: Optional[str]) -> Optional[str]:
        if not text or not self._bot or not getattr(self._bot, "username", None):
            return text
        username = re.escape(self._bot.username)
        cleaned = re.sub(rf"(?i)@{username}\b[,:\-]*\s*", "", text).strip()
        return cleaned or text

    def _bot_sender_check(self, message: Message) -> bool:
        """Gate messages authored by other bots (agent-to-agent messaging).

        Returns True to continue processing. Human messages always pass and
        reset the chat's consecutive bot-turn counter. Bot-authored messages
        pass only when ``extra.allow_bot_messages`` is on, the sender clears
        the optional ``allowed_bot_usernames`` allowlist, and the loop guards
        (consecutive-turn cap + per-chat rate limit) have headroom.
        """
        user = getattr(message, "from_user", None)
        chat_id = str(getattr(getattr(message, "chat", None), "id", "") or "")
        if user is None or not getattr(user, "is_bot", False):
            # Human activity ends any bot-to-bot exchange window.
            if chat_id:
                self._bot_turn_counts.pop(chat_id, None)
            return True
        # Own outbound messages must never loop back in as input.
        own_id = getattr(self._bot, "id", None) if self._bot is not None else None
        if own_id is not None and getattr(user, "id", None) == own_id:
            return False
        if not self._allow_bot_messages:
            logger.debug(
                "[%s] Dropping bot-authored message from %s (allow_bot_messages off)",
                self.name,
                getattr(user, "username", None) or getattr(user, "id", "?"),
            )
            return False
        uname = (getattr(user, "username", "") or "").lstrip("@").lower()
        if self._allowed_bot_usernames and uname not in self._allowed_bot_usernames:
            logger.info(
                "[%s] Dropping bot message: @%s not in allowed_bot_usernames",
                self.name,
                uname or getattr(user, "id", "?"),
            )
            return False
        count = self._bot_turn_counts.get(chat_id, 0) + 1
        if count > self._bot_loop_max_turns:
            logger.warning(
                "[%s] Bot-to-bot loop guard: %d consecutive bot turns in chat %s "
                "without human input — dropping until a human posts",
                self.name,
                count - 1,
                chat_id,
            )
            return False
        now = time.monotonic()
        times = self._bot_msg_times.setdefault(chat_id, deque())
        while times and now - times[0] > 60.0:
            times.popleft()
        if len(times) >= self._bot_rate_limit_per_minute:
            logger.warning(
                "[%s] Bot-to-bot rate limit: >%d bot messages/min in chat %s — dropping",
                self.name,
                self._bot_rate_limit_per_minute,
                chat_id,
            )
            return False
        times.append(now)
        self._bot_turn_counts[chat_id] = count
        return True

    def _should_process_message(self, message: Message, *, is_command: bool = False) -> bool:
        """Apply Telegram group trigger rules.

        DMs remain unrestricted. Group/supergroup messages are accepted when:
        - the chat is explicitly allowlisted in ``free_response_chats``
        - ``require_mention`` is disabled
        - the message is a command
        - the message replies to the bot
        - the bot is @mentioned
        - the text/caption matches a configured regex wake-word pattern
        """
        if not self._bot_sender_check(message):
            return False
        is_group = self._is_group_chat(message)
        group_policy = self._telegram_group_policy() if is_group else ""
        if group_policy == "disabled":
            return False
        # group_policy "open" lets non-allowlisted group members converse —
        # they stay non-owners (untrusted envelope + group tool denial apply);
        # DMs and "allowlist" groups keep the hard sender gate.
        if group_policy != "open" and not self._sender_is_allowed(message):
            user = getattr(message, "from_user", None)
            logger.info(
                "[%s] Dropping inbound message: sender id=%s username=%s not in allowed_users",
                self.name,
                getattr(user, "id", "?"),
                getattr(user, "username", "?"),
            )
            return False
        if not is_group:
            return True
        thread_id = getattr(message, "message_thread_id", None)
        if thread_id is not None:
            try:
                if int(thread_id) in self._telegram_ignored_threads():
                    return False
            except (TypeError, ValueError):
                logger.warning(
                    "[%s] Ignoring non-numeric Telegram message_thread_id: %r", self.name, thread_id
                )
        if (
            str(getattr(getattr(message, "chat", None), "id", ""))
            in self._telegram_free_response_chats()
        ):
            # A per-chat exemption is more specific than the global mode, so
            # it wins even over group_respond="none".
            return True
        if is_command:
            # Slash commands from allowed senders always work — the owner's
            # escape hatch even in "never reply" mode.
            return True
        respond = self._telegram_group_respond()
        if respond == "none":
            return False
        if respond == "all":
            return True
        if self._is_reply_to_bot(message):
            return True
        if self._message_mentions_bot(message):
            return True
        return self._message_matches_mention_patterns(message)

    async def _handle_text_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle incoming text messages.

        Telegram clients split long messages into multiple updates.  Buffer
        rapid successive text messages from the same user/chat and aggregate
        them into a single MessageEvent before dispatching.
        """
        # Business (secretary-mode) messages match the same TEXT filter via
        # Update.effective_message but carry no update.message — route them
        # through the dedicated business path (or drop when disabled).
        if update.business_message is not None or update.edited_business_message is not None:
            await self._handle_business_message(update)
            return
        if not update.message or not update.message.text:
            return
        if not self._should_process_message(update.message):
            # Passive listening: an unaddressed group message becomes observed
            # context for the agent's next addressed turn instead of vanishing.
            self._maybe_record_observed(update.message)
            return

        event = self._build_message_event(
            update.message, MessageType.TEXT, update_id=update.update_id
        )
        event.text = cast(str, self._clean_bot_trigger_text(event.text))
        event.text = self._attach_observed_context(update.message, event.text)
        # "what does this say?" under a photo is a text message whose whole
        # subject is the quoted attachment.
        await self._attach_quoted_media(event, update.message)
        self._enqueue_text_event(event)

    async def _handle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming command messages."""
        if not update.message or not update.message.text:
            return
        if not self._should_process_message(update.message, is_command=True):
            return

        event = self._build_message_event(
            update.message, MessageType.COMMAND, update_id=update.update_id
        )
        await self.handle_message(event)

    async def _handle_location_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle incoming location/venue pin messages."""
        if not update.message:
            return
        if not self._should_process_message(update.message):
            return

        msg = update.message
        venue = getattr(msg, "venue", None)
        location = getattr(venue, "location", None) if venue else getattr(msg, "location", None)

        if not location:
            return

        lat = getattr(location, "latitude", None)
        lon = getattr(location, "longitude", None)
        if lat is None or lon is None:
            return

        # Build a text message with coordinates and context
        parts = ["[The user shared a location pin.]"]
        if venue:
            title = getattr(venue, "title", None)
            address = getattr(venue, "address", None)
            if title:
                parts.append(f"Venue: {title}")
            if address:
                parts.append(f"Address: {address}")
        parts.append(f"latitude: {lat}")
        parts.append(f"longitude: {lon}")
        parts.append(f"Map: https://www.google.com/maps/search/?api=1&query={lat},{lon}")
        parts.append(
            "Ask what they'd like to find nearby (restaurants, cafes, etc.) and any preferences."
        )

        event = self._build_message_event(msg, MessageType.LOCATION, update_id=update.update_id)
        event.text = "\n".join(parts)
        await self.handle_message(event)

    # ------------------------------------------------------------------
    # Text message aggregation (handles Telegram client-side splits)
    # ------------------------------------------------------------------

    def _text_batch_key(self, event: MessageEvent) -> str:
        """Session-scoped key for text message batching."""
        from olano.platforms.session import build_session_key

        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )

    def _enqueue_text_event(self, event: MessageEvent) -> None:
        """Buffer a text event and reset the flush timer.

        When Telegram splits a long user message into multiple updates,
        they arrive within a few hundred milliseconds.  This method
        concatenates them and waits for a short quiet period before
        dispatching the combined message.
        """
        key = self._text_batch_key(event)
        existing = self._pending_text_batches.get(key)
        chunk_len = len(event.text or "")
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_text_batches[key] = event
        else:
            # Append text from the follow-up chunk
            if event.text:
                existing.text = f"{existing.text}\n{event.text}" if existing.text else event.text
            existing._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            # Merge any media that might be attached
            if event.media_urls:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)

        # Cancel any pending flush and restart the timer
        prior_task = self._pending_text_batch_tasks.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        self._pending_text_batch_tasks[key] = asyncio.create_task(self._flush_text_batch(key))

    async def _flush_text_batch(self, key: str) -> None:
        """Wait for the quiet period then dispatch the aggregated text.

        Uses a longer delay when the latest chunk is near Telegram's 4096-char
        split point, since a continuation chunk is almost certain.
        """
        current_task = asyncio.current_task()
        try:
            # Adaptive delay: if the latest chunk is near Telegram's 4096-char
            # split point, a continuation is almost certain — wait longer.
            pending = self._pending_text_batches.get(key)
            last_len = getattr(pending, "_last_chunk_len", 0) if pending else 0
            if last_len >= self._SPLIT_THRESHOLD:
                delay = self._text_batch_split_delay_seconds
            else:
                delay = self._text_batch_delay_seconds
            await asyncio.sleep(delay)
            event = self._pending_text_batches.pop(key, None)
            if not event:
                return
            logger.info(
                "[Telegram] Flushing text batch %s (%d chars)",
                key,
                len(event.text or ""),
            )
            await self.handle_message(event)
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)

    # ------------------------------------------------------------------
    # Photo batching
    # ------------------------------------------------------------------

    def _photo_batch_key(self, event: MessageEvent, msg: Message) -> str:
        """Return a batching key for Telegram photos/albums."""
        from olano.platforms.session import build_session_key

        session_key = build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )
        media_group_id = getattr(msg, "media_group_id", None)
        if media_group_id:
            return f"{session_key}:album:{media_group_id}"
        return f"{session_key}:photo-burst"

    async def _flush_photo_batch(self, batch_key: str) -> None:
        """Send a buffered photo burst/album as a single MessageEvent."""
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(self._media_batch_delay_seconds)
            event = self._pending_photo_batches.pop(batch_key, None)
            if not event:
                return
            logger.info(
                "[Telegram] Flushing photo batch %s with %d image(s)",
                batch_key,
                len(event.media_urls),
            )
            await self.handle_message(event)
        finally:
            if self._pending_photo_batch_tasks.get(batch_key) is current_task:
                self._pending_photo_batch_tasks.pop(batch_key, None)

    def _enqueue_photo_event(self, batch_key: str, event: MessageEvent) -> None:
        """Merge photo events into a pending batch and schedule flush."""
        existing = self._pending_photo_batches.get(batch_key)
        if existing is None:
            self._pending_photo_batches[batch_key] = event
        else:
            existing.media_urls.extend(event.media_urls)
            existing.media_types.extend(event.media_types)
            if event.text:
                existing.text = self._merge_caption(existing.text, event.text)

        prior_task = self._pending_photo_batch_tasks.get(batch_key)
        if prior_task and not prior_task.done():
            prior_task.cancel()

        self._pending_photo_batch_tasks[batch_key] = asyncio.create_task(
            self._flush_photo_batch(batch_key)
        )

    async def _handle_media_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle incoming media messages, downloading images to local cache."""
        if not update.message:
            return
        if not self._should_process_message(update.message):
            return

        msg = update.message

        # Determine media type
        if msg.sticker:
            msg_type = MessageType.STICKER
        elif msg.photo:
            msg_type = MessageType.PHOTO
        elif msg.video:
            msg_type = MessageType.VIDEO
        elif msg.audio:
            msg_type = MessageType.AUDIO
        elif msg.voice:
            msg_type = MessageType.VOICE
        elif msg.document:
            msg_type = MessageType.DOCUMENT
        else:
            msg_type = MessageType.DOCUMENT

        event = self._build_message_event(msg, msg_type, update_id=update.update_id)

        # Add caption as text
        if msg.caption:
            event.text = cast(str, self._clean_bot_trigger_text(msg.caption))

        # Media answering media ("here's mine" under someone's photo) still
        # needs the quoted one to be comparable.
        await self._attach_quoted_media(event, msg)

        # Handle stickers: describe via vision tool with caching
        if msg.sticker:
            await self._handle_sticker(msg, event)
            await self.handle_message(event)
            return

        # Download photo to local image cache so the vision tool can access it
        # even after Telegram's ephemeral file URLs expire (~1 hour).
        if msg.photo:
            try:
                # msg.photo is a list of PhotoSize sorted by size; take the largest
                photo = msg.photo[-1]
                file_obj = await photo.get_file()
                # Download the image bytes directly into memory
                image_bytes = await file_obj.download_as_bytearray()
                # Determine extension from the file path if available
                ext = ".jpg"
                if file_obj.file_path:
                    for candidate in [".png", ".webp", ".gif", ".jpeg", ".jpg"]:
                        if file_obj.file_path.lower().endswith(candidate):
                            ext = candidate
                            break
                # Save to local cache (for vision tool access)
                cached_path = cache_image_from_bytes(bytes(image_bytes), ext=ext)
                event.media_urls = [cached_path]
                event.media_types = [f"image/{ext.lstrip('.')}"]
                logger.info("[Telegram] Cached user photo at %s", cached_path)
                media_group_id = getattr(msg, "media_group_id", None)
                if media_group_id:
                    await self._queue_media_group_event(str(media_group_id), event)
                else:
                    batch_key = self._photo_batch_key(event, msg)
                    self._enqueue_photo_event(batch_key, event)
                return

            except Exception as e:
                logger.warning("[Telegram] Failed to cache photo: %s", e, exc_info=True)

        # Download voice/audio messages to cache for STT transcription
        if msg.voice:
            try:
                file_obj = await msg.voice.get_file()
                audio_bytes = await file_obj.download_as_bytearray()
                cached_path = cache_audio_from_bytes(bytes(audio_bytes), ext=".ogg")
                event.media_urls = [cached_path]
                event.media_types = ["audio/ogg"]
                logger.info("[Telegram] Cached user voice at %s", cached_path)
            except Exception as e:
                logger.warning("[Telegram] Failed to cache voice: %s", e, exc_info=True)
        elif msg.audio:
            try:
                file_obj = await msg.audio.get_file()
                audio_bytes = await file_obj.download_as_bytearray()
                cached_path = cache_audio_from_bytes(bytes(audio_bytes), ext=".mp3")
                event.media_urls = [cached_path]
                event.media_types = ["audio/mp3"]
                logger.info("[Telegram] Cached user audio at %s", cached_path)
            except Exception as e:
                logger.warning("[Telegram] Failed to cache audio: %s", e, exc_info=True)

        # Download document files to cache for agent processing
        elif msg.document:
            doc = msg.document
            try:
                # Determine file extension
                ext = ""
                original_filename = doc.file_name or ""
                if original_filename:
                    _, ext = os.path.splitext(original_filename)
                    ext = ext.lower()

                # If no extension from filename, reverse-lookup from MIME type
                if not ext and doc.mime_type:
                    mime_to_ext = {v: k for k, v in SUPPORTED_DOCUMENT_TYPES.items()}
                    ext = mime_to_ext.get(doc.mime_type, "")

                # Check if supported
                if ext not in SUPPORTED_DOCUMENT_TYPES:
                    supported_list = ", ".join(sorted(SUPPORTED_DOCUMENT_TYPES.keys()))
                    event.text = (
                        f"Unsupported document type '{ext or 'unknown'}'. "
                        f"Supported types: {supported_list}"
                    )
                    logger.info("[Telegram] Unsupported document type: %s", ext or "unknown")
                    await self.handle_message(event)
                    return

                # Check file size (Telegram Bot API limit: 20 MB)
                MAX_DOC_BYTES = 20 * 1024 * 1024
                if not doc.file_size or doc.file_size > MAX_DOC_BYTES:
                    event.text = (
                        "The document is too large or its size could not be verified. "
                        "Maximum: 20 MB."
                    )
                    logger.info("[Telegram] Document too large: %s bytes", doc.file_size)
                    await self.handle_message(event)
                    return

                # Download and cache
                file_obj = await doc.get_file()
                doc_bytes = await file_obj.download_as_bytearray()
                raw_bytes = bytes(doc_bytes)
                cached_path = cache_document_from_bytes(
                    raw_bytes, original_filename or f"document{ext}"
                )
                mime_type = SUPPORTED_DOCUMENT_TYPES[ext]
                event.media_urls = [cached_path]
                event.media_types = [mime_type]
                logger.info("[Telegram] Cached user document at %s", cached_path)

                # For text files, inject content into event.text (capped at 100 KB)
                MAX_TEXT_INJECT_BYTES = 100 * 1024
                if ext in (".md", ".txt") and len(raw_bytes) <= MAX_TEXT_INJECT_BYTES:
                    try:
                        text_content = raw_bytes.decode("utf-8")
                        display_name = original_filename or f"document{ext}"
                        display_name = re.sub(r"[^\w.\- ]", "_", display_name)
                        injection = f"[Content of {display_name}]:\n{text_content}"
                        if event.text:
                            event.text = f"{injection}\n\n{event.text}"
                        else:
                            event.text = injection
                    except UnicodeDecodeError:
                        logger.warning(
                            "[Telegram] Could not decode text file as UTF-8, skipping content injection",
                            exc_info=True,
                        )

            except Exception as e:
                logger.warning("[Telegram] Failed to cache document: %s", e, exc_info=True)

        media_group_id = getattr(msg, "media_group_id", None)
        if media_group_id:
            await self._queue_media_group_event(str(media_group_id), event)
            return

        await self.handle_message(event)

    async def _queue_media_group_event(self, media_group_id: str, event: MessageEvent) -> None:
        """Buffer Telegram media-group items so albums arrive as one logical event.

        Telegram delivers albums as multiple updates with a shared media_group_id.
        If we forward each item immediately, the gateway thinks the second image is a
        new user message and interrupts the first. We debounce briefly and merge the
        attachments into a single MessageEvent.
        """
        existing = self._media_group_events.get(media_group_id)
        if existing is None:
            self._media_group_events[media_group_id] = event
        else:
            existing.media_urls.extend(event.media_urls)
            existing.media_types.extend(event.media_types)
            if event.text:
                existing.text = self._merge_caption(existing.text, event.text)

        prior_task = self._media_group_tasks.get(media_group_id)
        if prior_task:
            prior_task.cancel()

        self._media_group_tasks[media_group_id] = asyncio.create_task(
            self._flush_media_group_event(media_group_id)
        )

    async def _flush_media_group_event(self, media_group_id: str) -> None:
        try:
            await asyncio.sleep(self.MEDIA_GROUP_WAIT_SECONDS)
            event = self._media_group_events.pop(media_group_id, None)
            if event is not None:
                await self.handle_message(event)
        except asyncio.CancelledError:
            return
        finally:
            self._media_group_tasks.pop(media_group_id, None)

    async def _handle_sticker(self, msg: Message, event: "MessageEvent") -> None:
        """
        Describe a Telegram sticker via vision analysis, with caching.

        For static stickers (WEBP), we download, analyze with vision, and cache
        the description by file_unique_id. For animated/video stickers, we inject
        a placeholder noting the emoji.
        """
        from gateway.sticker_cache import (
            STICKER_VISION_PROMPT,
            build_animated_sticker_injection,
            build_sticker_injection,
            cache_sticker_description,
            get_cached_description,
        )

        sticker = msg.sticker
        assert sticker is not None
        emoji = sticker.emoji or ""
        set_name = sticker.set_name or ""

        # Animated and video stickers can't be analyzed as static images
        if sticker.is_animated or sticker.is_video:
            event.text = build_animated_sticker_injection(emoji)
            return

        # Check the cache first
        cached = get_cached_description(sticker.file_unique_id)
        if cached:
            event.text = build_sticker_injection(
                cached["description"], cached.get("emoji", emoji), cached.get("set_name", set_name)
            )
            logger.info("[Telegram] Sticker cache hit: %s", sticker.file_unique_id)
            return

        # Cache miss -- download and analyze
        try:
            file_obj = await sticker.get_file()
            image_bytes = await file_obj.download_as_bytearray()
            cached_path = cache_image_from_bytes(bytes(image_bytes), ext=".webp")
            logger.info("[Telegram] Analyzing sticker at %s", cached_path)

            import json as _json

            from tools.vision_tools import vision_analyze_tool

            result_json = await vision_analyze_tool(
                image_url=cached_path,
                user_prompt=STICKER_VISION_PROMPT,
            )
            result = _json.loads(result_json)

            if result.get("success"):
                description = result.get("analysis", "a sticker")
                cache_sticker_description(sticker.file_unique_id, description, emoji, set_name)
                event.text = build_sticker_injection(description, emoji, set_name)
            else:
                # Vision failed -- use emoji as fallback
                event.text = build_sticker_injection(
                    f"a sticker with emoji {emoji}" if emoji else "a sticker",
                    emoji,
                    set_name,
                )
        except Exception as e:
            logger.warning("[Telegram] Sticker analysis error: %s", e, exc_info=True)
            event.text = build_sticker_injection(
                f"a sticker with emoji {emoji}" if emoji else "a sticker",
                emoji,
                set_name,
            )

    def _reload_dm_topics_from_config(self) -> None:
        """Re-read dm_topics from config.yaml and load any new thread_ids into cache.

        This allows topics created externally (e.g. by the agent via API) to be
        recognized without a gateway restart.
        """
        try:
            from olano.config.home import get_olano_home

            config_path = get_olano_home() / "config.yaml"
            if not config_path.exists():
                return

            import yaml as _yaml

            with open(config_path) as f:
                config = _yaml.safe_load(f) or {}

            dm_topics = (
                config.get("platforms", {})
                .get("telegram", {})
                .get("extra", {})
                .get("dm_topics", [])
            )
            if not dm_topics:
                return

            # Update in-memory config and cache any new thread_ids
            self._dm_topics_config = dm_topics
            for chat_entry in dm_topics:
                cid = chat_entry.get("chat_id")
                if not cid:
                    continue
                for t in chat_entry.get("topics", []):
                    tid = t.get("thread_id")
                    name = t.get("name")
                    if tid and name:
                        cache_key = f"{cid}:{name}"
                        if cache_key not in self._dm_topics:
                            self._dm_topics[cache_key] = int(tid)
                            logger.info(
                                "[%s] Hot-loaded DM topic from config: %s -> thread_id=%s",
                                self.name,
                                cache_key,
                                tid,
                            )
        except Exception as e:
            logger.debug("[%s] Failed to reload dm_topics from config: %s", self.name, e)

    def _get_dm_topic_info(
        self, chat_id: str, thread_id: Optional[str]
    ) -> Optional[dict[str, Any]]:
        """Look up DM topic config by chat_id and thread_id.

        Returns the topic config dict (name, skill, etc.) if this thread_id
        matches a known DM topic, or None.
        """
        if not thread_id:
            return None

        thread_id_int = int(thread_id)

        # Check cached topics first (created by us or loaded at startup)
        for key, cached_tid in self._dm_topics.items():
            if cached_tid == thread_id_int and key.startswith(f"{chat_id}:"):
                topic_name = key.split(":", 1)[1]
                # Find the full config for this topic
                for chat_entry in self._dm_topics_config:
                    if str(chat_entry.get("chat_id")) == chat_id:
                        for t in chat_entry.get("topics", []):
                            if t.get("name") == topic_name:
                                return cast("dict[str, Any]", t)
                return {"name": topic_name}

        # Not in cache — hot-reload config in case topics were added externally
        self._reload_dm_topics_from_config()

        # Check cache again after reload
        for key, cached_tid in self._dm_topics.items():
            if cached_tid == thread_id_int and key.startswith(f"{chat_id}:"):
                topic_name = key.split(":", 1)[1]
                for chat_entry in self._dm_topics_config:
                    if str(chat_entry.get("chat_id")) == chat_id:
                        for t in chat_entry.get("topics", []):
                            if t.get("name") == topic_name:
                                return cast("dict[str, Any]", t)
                return {"name": topic_name}

        return None

    def _cache_dm_topic_from_message(self, chat_id: str, thread_id: str, topic_name: str) -> None:
        """Cache a thread_id -> topic_name mapping discovered from an incoming message."""
        cache_key = f"{chat_id}:{topic_name}"
        if cache_key not in self._dm_topics:
            self._dm_topics[cache_key] = int(thread_id)
            logger.info(
                "[%s] Cached DM topic from message: %s -> thread_id=%s",
                self.name,
                cache_key,
                thread_id,
            )

    # Telegram's getFile caps at 20 MB, and a quoted attachment is a side-quest
    # to the message the user actually sent — never worth stalling a turn for.
    QUOTED_MEDIA_MAX_BYTES = 20 * 1024 * 1024

    async def _download_quoted_attachment(self, quoted: Message) -> tuple[Optional[str], str]:
        """Cache the one attachment on a quoted message; ``(None, "")`` if none.

        Covers the kinds the adapter downloads for a message's own media —
        photo, voice, audio, document — so a quote can never surface a file
        type the ordinary path would have dropped.
        """
        if quoted.photo:
            file_obj = await quoted.photo[-1].get_file()
            if (file_obj.file_size or 0) > self.QUOTED_MEDIA_MAX_BYTES:
                return None, ""
            raw = bytes(await file_obj.download_as_bytearray())
            ext = ".jpg"
            for candidate in (".png", ".webp", ".gif", ".jpeg", ".jpg"):
                if (file_obj.file_path or "").lower().endswith(candidate):
                    ext = candidate
                    break
            return cache_image_from_bytes(raw, ext=ext), f"image/{ext.lstrip('.')}"

        for attr, ext, mime in (("voice", ".ogg", "audio/ogg"), ("audio", ".mp3", "audio/mp3")):
            node = getattr(quoted, attr, None)
            if node is None:
                continue
            file_obj = await node.get_file()
            if (file_obj.file_size or 0) > self.QUOTED_MEDIA_MAX_BYTES:
                return None, ""
            raw = bytes(await file_obj.download_as_bytearray())
            return cache_audio_from_bytes(raw, ext=ext), mime

        doc = getattr(quoted, "document", None)
        if doc is not None:
            name = doc.file_name or ""
            ext = os.path.splitext(name)[1].lower()
            if not ext and doc.mime_type:
                ext = {v: k for k, v in SUPPORTED_DOCUMENT_TYPES.items()}.get(doc.mime_type, "")
            if ext not in SUPPORTED_DOCUMENT_TYPES:
                return None, ""
            if not doc.file_size or doc.file_size > self.QUOTED_MEDIA_MAX_BYTES:
                return None, ""
            file_obj = await doc.get_file()
            raw = bytes(await file_obj.download_as_bytearray())
            path = cache_document_from_bytes(raw, name or f"document{ext}")
            return path, SUPPORTED_DOCUMENT_TYPES[ext]

        return None, ""

    async def _attach_quoted_media(self, event: MessageEvent, message: Message) -> None:
        """Put the attachment on ``message.reply_to_message`` onto *event*.

        Replying to a photo is how people point at one ("what does this say?"),
        and a photo with no caption leaves ``reply_to_text`` empty — so without
        this the agent is told a reply happened and never sees the thing it is
        about. Best effort by design: a failed or oversized quoted download
        degrades to the reply-context note, never to a lost message.
        """
        quoted = getattr(message, "reply_to_message", None)
        if quoted is None:
            return
        try:
            path, mime = await self._download_quoted_attachment(quoted)
        except Exception as e:  # network, expired file_id, Bot API refusal
            logger.info("[Telegram] quoted attachment download failed: %s", e)
            return
        if path:
            event.reply_to_media_urls = [path]
            event.reply_to_media_types = [mime]
            logger.info("[Telegram] Cached quoted attachment at %s", path)

    def _build_message_event(
        self,
        message: Message,
        msg_type: MessageType,
        update_id: Optional[int] = None,
        business_connection_id: Optional[str] = None,
    ) -> MessageEvent:
        """Build a MessageEvent from a Telegram message.

        ``update_id`` is the ``Update.update_id`` from PTB; passing it through
        lets ``/restart`` record the triggering offset so the new gateway
        process can advance past it (prevents ``/restart`` being re-delivered
        when PTB's graceful-shutdown ACK fails).

        ``business_connection_id`` marks a secretary-mode message: the chat is
        namespaced as ``biz:<connection_id>:<chat_id>`` so its sessions stay
        separate from a direct DM and outbound paths reply via the connection.
        """
        chat = message.chat
        user = message.from_user

        # Determine chat type
        chat_type = "dm"
        if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            chat_type = "group"
        elif chat.type == ChatType.CHANNEL:
            chat_type = "channel"

        # Resolve DM topic name and skill binding
        thread_id_raw = message.message_thread_id
        thread_id_str = str(thread_id_raw) if thread_id_raw is not None else None
        if chat_type == "group" and thread_id_str is None and getattr(chat, "is_forum", False):
            thread_id_str = self._GENERAL_TOPIC_THREAD_ID
        chat_topic = None
        topic_skill = None

        if chat_type == "dm" and thread_id_str:
            topic_info = self._get_dm_topic_info(str(chat.id), thread_id_str)
            if topic_info:
                chat_topic = topic_info.get("name")
                topic_skill = topic_info.get("skill")

            # Also check forum_topic_created service message for topic discovery
            if hasattr(message, "forum_topic_created") and message.forum_topic_created:
                created_name = message.forum_topic_created.name
                if created_name:
                    self._cache_dm_topic_from_message(str(chat.id), thread_id_str, created_name)
                    if not chat_topic:
                        chat_topic = created_name

        elif chat_type == "group" and thread_id_str:
            # Group/supergroup forum topic skill binding via config.extra['group_topics']
            group_topics_config: list = self.config.extra.get("group_topics", [])
            for chat_entry in group_topics_config:
                if str(chat_entry.get("chat_id", "")) == str(chat.id):
                    for topic in chat_entry.get("topics", []):
                        tid = topic.get("thread_id")
                        if tid is not None and str(tid) == thread_id_str:
                            chat_topic = topic.get("name")
                            topic_skill = topic.get("skill")
                            break
                    break
            # Auto-learn the topic's name without config: a top-level topic
            # message arrives as a "reply" to the topic's creation service
            # message, which carries the name (same trick the DM-topics code
            # uses). Best-effort — a reply to a real message has no name and
            # a later message fills it in.
            if not chat_topic:
                _reply = getattr(message, "reply_to_message", None)
                _created = getattr(_reply, "forum_topic_created", None) if _reply else None
                if _created is None:
                    _created = getattr(message, "forum_topic_created", None)
                created_topic_name = str(getattr(_created, "name", "") or "")
                if created_topic_name:
                    chat_topic = created_topic_name
            if not chat_topic and thread_id_str == self._GENERAL_TOPIC_THREAD_ID:
                # Telegram's implicit first topic — it has no creation event.
                chat_topic = "General"

        # Build source. Business (secretary-mode) chats are namespaced so
        # their sessions and outbound routing stay separate from a direct DM
        # with the same person (see _parse_chat_ref).
        chat_ref = (
            f"{self._BIZ_CHAT_PREFIX}{business_connection_id}:{chat.id}"
            if business_connection_id
            else str(chat.id)
        )
        source = self.build_source(
            chat_id=chat_ref,
            chat_name=chat.title or (chat.full_name if hasattr(chat, "full_name") else None),
            chat_type=chat_type,
            user_id=str(user.id) if user else None,
            user_name=user.full_name if user else None,
            username=getattr(user, "username", None) if user else None,
            thread_id=thread_id_str,
            chat_topic=chat_topic,
            is_bot=bool(getattr(user, "is_bot", False)) if user else False,
        )

        # Extract reply context if this message is a reply
        reply_to_id = None
        reply_to_text = None
        if message.reply_to_message:
            reply_to_id = str(message.reply_to_message.message_id)
            reply_to_text = (
                message.reply_to_message.text or message.reply_to_message.caption or None
            )

        # Per-channel/topic ephemeral prompt
        from olano.platforms.base import resolve_channel_prompt

        _chat_id_str = str(chat.id)
        _channel_prompt = resolve_channel_prompt(
            self.config.extra,
            thread_id_str or _chat_id_str,
            _chat_id_str if thread_id_str else None,
        )

        return MessageEvent(
            text=message.text or "",
            message_type=msg_type,
            source=source,
            raw_message=message,
            message_id=str(message.message_id),
            platform_update_id=update_id,
            reply_to_message_id=reply_to_id,
            reply_to_text=reply_to_text,
            auto_skill=topic_skill,
            channel_prompt=_channel_prompt,
            timestamp=message.date,
        )

    # ── "Thinking…" placeholder (processing lifecycle) ────────────────────
    # While the agent works on a reply the chat shows a visible
    # "💭 Thinking…" bubble (in addition to the ephemeral typing status).
    # When the reply is ready, send() edits the placeholder in place so it
    # becomes the final message; if no reply ever claims it (empty
    # response, error, interrupt) on_processing_complete deletes it.

    @staticmethod
    def _thinking_key(chat_id: str, message_id: Optional[str]) -> str:
        return f"{chat_id}:{message_id or ''}"

    async def _send_thinking_placeholder(self, event: MessageEvent) -> None:
        """Post the placeholder bubble for *event* and remember its id."""
        if not self._bot:
            return
        chat_id = str(event.source.chat_id)
        # Secretary mode replies go out as the account owner, to their own
        # customers — a "💭 Thinking…" bubble immediately reads as a bot and
        # costs the realism the whole feature depends on. Business chats get
        # their own switch, off by default, rather than inheriting the normal
        # chat setting. (Telegram's ephemeral "typing…" status still shows,
        # which is what a human replying looks like anyway.)
        _plain_chat, _biz_conn = self._parse_chat_ref(chat_id)
        enabled = self._business_thinking_message if _biz_conn else self._thinking_message_enabled
        if not enabled:
            return
        key = self._thinking_key(chat_id, event.message_id)

        # A stale placeholder here means the same message is being
        # reprocessed — remove the old bubble so the chat shows one.
        stale = self._thinking_placeholders.pop(key, None)
        if stale is not None:
            await self._delete_thinking_message(chat_id, stale)

        _plain_chat, _biz_conn = self._parse_chat_ref(chat_id)
        kwargs: dict[str, Any] = {
            "chat_id": int(_plain_chat),
            "text": f"💭 <i>{_html.escape(self._thinking_message_text)}</i>",
            "parse_mode": ParseMode.HTML,
            **self._link_preview_kwargs(),
        }
        if _biz_conn:
            kwargs["business_connection_id"] = _biz_conn
        if event.message_id and self._should_thread_reply(event.message_id, 0):
            kwargs["reply_to_message_id"] = int(event.message_id)
        message_thread_id = self._message_thread_id_for_send(event.source.thread_id)
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id

        try:
            try:
                msg = await self._bot.send_message(**kwargs)
            except Exception as first_err:
                err_lower = str(first_err).lower()
                # Same recoverable cases as send(): a vanished forum topic or
                # a deleted reply target shouldn't cost us the placeholder.
                retry = False
                if self._is_thread_not_found_error(first_err) and "message_thread_id" in kwargs:
                    kwargs.pop("message_thread_id", None)
                    retry = True
                if (
                    "message to be replied not found" in err_lower
                    and "reply_to_message_id" in kwargs
                ):
                    kwargs.pop("reply_to_message_id", None)
                    retry = True
                if not retry:
                    raise
                msg = await self._bot.send_message(**kwargs)
        except Exception as e:
            logger.debug("[%s] Could not send thinking placeholder: %s", self.name, e)
            return

        self._thinking_placeholders[key] = msg.message_id

    def _claim_thinking_placeholder(self, chat_id: str, reply_to: Optional[str]) -> Optional[int]:
        """Pop the placeholder id when *reply_to* is the message it was staged for.

        Matching on the triggering message id keeps unrelated sends to the
        same chat (agent-initiated notifications, error notices) from
        consuming the bubble.
        """
        if not reply_to:
            return None
        placeholder_id = self._thinking_placeholders.pop(
            self._thinking_key(str(chat_id), reply_to), None
        )
        return int(placeholder_id) if placeholder_id is not None else None

    async def _edit_thinking_into_reply(
        self, chat_id: str, message_id: int, formatted_chunk: str
    ) -> bool:
        """Edit the placeholder with an already-formatted MarkdownV2 chunk."""
        if not self._bot:
            return False
        chat_id, _biz_conn = self._parse_chat_ref(chat_id)
        try:
            try:
                await self._bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=message_id,
                    text=formatted_chunk,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    business_connection_id=_biz_conn,
                    **self._link_preview_kwargs(),
                )
            except Exception as md_error:
                err_lower = str(md_error).lower()
                if "not modified" in err_lower:
                    return True
                if "parse" not in err_lower and "markdown" not in err_lower:
                    raise
                await self._bot.edit_message_text(
                    chat_id=int(chat_id),
                    message_id=message_id,
                    text=_strip_mdv2(formatted_chunk),
                    parse_mode=None,
                    business_connection_id=_biz_conn,
                    **self._link_preview_kwargs(),
                )
            return True
        except Exception as e:
            logger.debug(
                "[%s] Could not edit thinking placeholder %s: %s", self.name, message_id, e
            )
            return False

    # ── Live progress ─────────────────────────────────────────────────────
    # The "💭 Thinking…" bubble and the progress view want the same slot in
    # the chat, so progress takes the placeholder over rather than posting a
    # second bubble. Ownership transfers with the id: once adopted, the
    # placeholder bookkeeping must not touch that message again, or send()
    # would edit a half-finished tool log into the final reply and
    # on_processing_complete would delete a message progress still owns.

    def _adopt_progress_message(self, event: MessageEvent) -> Optional[str]:
        chat_id = getattr(getattr(event, "source", None), "chat_id", None)
        if not chat_id:
            return None
        key = self._thinking_key(str(chat_id), getattr(event, "message_id", None))
        placeholder_id = self._thinking_placeholders.pop(key, None)
        return str(placeholder_id) if placeholder_id is not None else None

    async def _delete_progress_message(self, chat_id: str, message_id: str) -> bool:
        if not self._bot:
            return False
        plain_chat, biz_conn = self._parse_chat_ref(chat_id)
        if biz_conn:
            # deleteMessage takes no business_connection_id — an owner-sent
            # business message cannot be deleted this way. Report failure so
            # the caller collapses the bubble instead.
            return False
        try:
            await self._bot.delete_message(chat_id=int(plain_chat), message_id=int(message_id))
            return True
        except Exception as exc:
            logger.debug("[%s] Could not delete progress message: %s", self.name, exc)
            return False

    async def _delete_thinking_message(self, chat_id: str, message_id: Any) -> None:
        if not self._bot:
            return
        try:
            # deleteMessage has no business_connection_id parameter; deleting
            # an owner-sent business message uses a different API. Best-effort
            # only — in business chats the placeholder is edited, not deleted.
            _plain_chat, _ = self._parse_chat_ref(chat_id)
            await self._bot.delete_message(chat_id=int(_plain_chat), message_id=int(message_id))
        except Exception as e:
            logger.debug(
                "[%s] Could not delete thinking placeholder %s: %s", self.name, message_id, e
            )

    # ── Message reactions (processing lifecycle) ──────────────────────────

    def _reactions_enabled(self) -> bool:
        """Check if message reactions are enabled via config/env."""
        return os.getenv("TELEGRAM_REACTIONS", "false").lower() not in ("false", "0", "no")

    async def _set_reaction(self, chat_id: str, message_id: str, emoji: str) -> bool:
        """Set a single emoji reaction on a Telegram message."""
        if not self._bot:
            return False
        _plain_chat, _biz_conn = self._parse_chat_ref(chat_id)
        if _biz_conn:
            # setMessageReaction has no business_connection_id parameter —
            # reacting inside the owner's business chat is not supported.
            return False
        try:
            await self._bot.set_message_reaction(
                chat_id=int(_plain_chat),
                message_id=int(message_id),
                reaction=emoji,
            )
            return True
        except Exception as e:
            logger.debug("[%s] set_message_reaction failed (%s): %s", self.name, emoji, e)
            return False

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Post the "Thinking…" placeholder and add an in-progress reaction."""
        await self._send_thinking_placeholder(event)
        if not self._reactions_enabled():
            return
        chat_id = getattr(event.source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if chat_id and message_id:
            await self._set_reaction(chat_id, message_id, "\U0001f440")

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Clean up the "Thinking…" placeholder and swap the reaction.

        The placeholder is normally consumed by send() editing it into the
        final reply; if it's still tracked here the turn produced no reply
        (empty response, failure, interrupt), so delete the bubble.

        Unlike Discord (additive reactions), Telegram's set_message_reaction
        replaces all existing reactions in one call — no remove step needed.
        """
        chat_id = getattr(event.source, "chat_id", None)
        if chat_id:
            key = self._thinking_key(str(chat_id), event.message_id)
            placeholder_id = self._thinking_placeholders.pop(key, None)
            if placeholder_id is not None:
                await self._delete_thinking_message(str(chat_id), placeholder_id)
        if not self._reactions_enabled():
            return
        message_id = getattr(event, "message_id", None)
        if chat_id and message_id and outcome != ProcessingOutcome.CANCELLED:
            await self._set_reaction(
                chat_id,
                message_id,
                "\U0001f44d" if outcome == ProcessingOutcome.SUCCESS else "\U0001f44e",
            )
