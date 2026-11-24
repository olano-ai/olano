"""Base channel adapter interface."""

from __future__ import annotations

import abc
import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ChannelMessage:
    """Normalized inbound message from any channel."""

    text: str
    user_id: str
    channel: str
    thread_id: str  # used as LangGraph thread_id for memory continuity
    raw: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SendResult:
    """
    Outcome of a send operation.

    `retryable` distinguishes transient connection failures (safe to retry)
    from failures where the request may have reached the server (unsafe to
    retry; retrying risks duplicate delivery). Adapters set this explicitly
    when they know; otherwise callers infer it from the error string.
    """

    success: bool
    message_id: str | None = None
    error: str | None = None
    retryable: bool = False


MessageHandler = Callable[[ChannelMessage], Awaitable[str]]


_RETRYABLE_ERROR_PATTERNS = (
    "connecterror",
    "connectionerror",
    "connectionreset",
    "connectionrefused",
    "connecttimeout",
    "nameresolutionerror",
    "dns",
    "network",
    "broken pipe",
    "remotedisconnected",
    "eoferror",
)

_TIMEOUT_ERROR_PATTERNS = (
    "timed out",
    "readtimeout",
    "writetimeout",
)


def is_retryable_error(error: str | None) -> bool:
    """Return True if *error* looks like a transient connection failure.

    Read/write timeouts are NOT retryable — the request may have been
    delivered and retrying risks duplicate messages. Connect-time
    timeouts ARE retryable because the connection was never established.
    """
    if not error:
        return False
    lowered = error.lower()
    if any(pat in lowered for pat in _TIMEOUT_ERROR_PATTERNS):
        return False
    return any(pat in lowered for pat in _RETRYABLE_ERROR_PATTERNS)


def is_timeout_error(error: str | None) -> bool:
    """Return True if *error* is a read/write timeout (do not retry)."""
    if not error:
        return False
    lowered = error.lower()
    return any(pat in lowered for pat in _TIMEOUT_ERROR_PATTERNS)


def utf16_len(s: str) -> int:
    """Count UTF-16 code units — Telegram's 4096 limit is in UTF-16 units.

    Characters outside the BMP (emoji, CJK Extension B, etc.) encode as
    surrogate pairs and count as 2, even though ``len()`` returns 1.
    """
    return len(s.encode("utf-16-le")) // 2


def _custom_unit_to_cp(s: str, budget: int, len_fn: Callable[[str], int]) -> int:
    """Largest codepoint offset *n* such that ``len_fn(s[:n]) <= budget``."""
    if len_fn(s) <= budget:
        return len(s)
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len_fn(s[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


def truncate_message(
    content: str,
    max_length: int = 4096,
    len_fn: Callable[[str], int] | None = None,
) -> list[str]:
    """Split *content* into chunks ≤ *max_length*, preserving code fences.

    When a split falls inside a triple-backtick block, the fence is closed
    at the end of the current chunk and reopened (with the original language
    tag) at the start of the next. Multi-chunk output gets `(1/N)` indicators.

    Pass ``len_fn=utf16_len`` for Telegram so UTF-16 code units are measured.
    """
    _len = len_fn or len
    if _len(content) <= max_length:
        return [content]

    INDICATOR_RESERVE = 10  # " (XX/XX)"
    FENCE_CLOSE = "\n```"

    chunks: list[str] = []
    remaining = content
    carry_lang: str | None = None

    while remaining:
        prefix = f"```{carry_lang}\n" if carry_lang is not None else ""
        headroom = max_length - INDICATOR_RESERVE - _len(prefix) - _len(FENCE_CLOSE)
        if headroom < 1:
            headroom = max_length // 2

        if _len(prefix) + _len(remaining) <= max_length - INDICATOR_RESERVE:
            chunks.append(prefix + remaining)
            break

        cp_limit = _custom_unit_to_cp(remaining, headroom, _len) if _len is not len else headroom

        region = remaining[:cp_limit]
        split_at = region.rfind("\n")
        if split_at < cp_limit // 2:
            split_at = region.rfind(" ")
        if split_at < 1:
            split_at = cp_limit

        chunk_body = remaining[:split_at]
        remaining = remaining[split_at:].lstrip()

        in_code = carry_lang is not None
        lang = carry_lang or ""
        for line in chunk_body.split("\n"):
            stripped = line.strip()
            if stripped.startswith("```"):
                if in_code:
                    in_code = False
                    lang = ""
                else:
                    in_code = True
                    tag = stripped[3:].strip()
                    lang = tag.split()[0] if tag else ""

        full_chunk = prefix + chunk_body
        if in_code:
            full_chunk += FENCE_CLOSE
            carry_lang = lang
        else:
            carry_lang = None

        chunks.append(full_chunk)

    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"{chunk} ({i + 1}/{total})" for i, chunk in enumerate(chunks)]

    return chunks


class BaseChannel(abc.ABC):
    """
    Abstract base for all channel adapters.

    Each adapter:
    1. Listens for inbound messages on a platform (Telegram, Discord, etc.)
    2. Normalizes them to ChannelMessage
    3. Calls the registered handler (which runs the agent)
    4. Sends the response back to the platform

    Subclasses implement `start`, `stop`, and `send`. For richer control,
    override `send_message` to return a `SendResult` — the default wraps
    `send()` in a try/except and classifies failures.
    """

    name: str = "base"
    max_message_length: int = 4096
    use_utf16_length: bool = False

    # Whether the runtime may append live progress updates (the agent's plan
    # and tool calls) to this channel while a turn runs. Classic channels
    # only send — there is no edit to update in place — so every update is a
    # new message and a notification. Opt in per channel, and only where a
    # play-by-play is welcome: never on email or SMS-like surfaces.
    SUPPORTS_PROGRESS: bool = False

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        # Identifier of the owning agent, set by the runtime right after
        # construction. Used by per-agent default resolution and by the
        # multi-tenant ChannelRegistry routing.
        self.agent_name: str = ""
        self._handler: MessageHandler | None = None
        # Queue for messages that arrive while a session is actively processing.
        # Key: session_key (usually thread_id); value: ChannelMessage.
        self._pending_messages: dict[str, ChannelMessage] = {}
        # Set of active session keys currently being processed.
        self._active_sessions: set[str] = set()
        # Background tasks spawned by the adapter (cancel on shutdown).
        self._background_tasks: set[asyncio.Task] = set()
        # Chats where typing indicator is paused (e.g., during approval waits).
        self._typing_paused: set[str] = set()

    def set_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    async def handle(self, msg: ChannelMessage) -> str:
        """Call the registered handler and return the response."""
        if self._handler is None:
            raise RuntimeError("No message handler registered")
        return await self._handler(msg)

    @abc.abstractmethod
    async def start(self) -> None:
        """Start listening for messages."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop the channel adapter. Base teardown cancels tracked tasks."""

    @abc.abstractmethod
    async def send(self, recipient: str, text: str) -> None:
        """Send a message back to the user.

        Raise on failure. For richer retry/classification, override
        `send_message` to return a `SendResult` directly.
        """

    async def make_approval_handler(
        self, recipient: str, agent_name: str
    ) -> Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]] | None:
        """Return a coroutine that prompts *recipient* for HITL approval.

        The returned callable receives a list of pending action dicts and
        must return a list of decision dicts (one per action), each shaped
        ``{"type": "approve" | "reject", "message": Optional[str]}``.

        Default implementation returns ``None`` — the runner will then
        auto-reject pending approvals. Channels that can render interactive
        approval UIs (inline buttons, slash commands) should override this.
        """
        return None

    async def send_message(self, recipient: str, text: str) -> SendResult:
        """Send *text* to *recipient* and return a structured SendResult.

        Default implementation wraps `send()` in a try/except. Adapters that
        can surface a platform message_id or distinguish error types should
        override this method directly.
        """
        try:
            await self.send(recipient, text)
            return SendResult(success=True)
        except Exception as e:
            err = str(e)
            return SendResult(
                success=False,
                error=err,
                retryable=is_retryable_error(err),
            )

    async def send_with_retry(
        self,
        recipient: str,
        text: str,
        *,
        max_retries: int = 2,
        base_delay: float = 2.0,
    ) -> SendResult:
        """Send with exponential backoff on transient failures.

        Splits long messages into chunks via `truncate_message` respecting
        the adapter's `max_message_length` and `use_utf16_length`. On
        permanent failure, falls back once to a plain-text version.
        """
        if not text or not text.strip():
            logger.debug("[%s] Skipping empty send to %s", self.name, recipient)
            return SendResult(success=True)

        len_fn = utf16_len if self.use_utf16_length else len
        chunks = truncate_message(text, self.max_message_length, len_fn=len_fn)

        last_result: SendResult = SendResult(success=True)
        for chunk in chunks:
            result = await self._send_chunk_with_retry(
                recipient, chunk, max_retries=max_retries, base_delay=base_delay
            )
            last_result = result
            if not result.success:
                return result
        return last_result

    async def _send_chunk_with_retry(
        self,
        recipient: str,
        chunk: str,
        *,
        max_retries: int,
        base_delay: float,
    ) -> SendResult:
        result = await self.send_message(recipient, chunk)
        if result.success:
            return result

        if is_timeout_error(result.error):
            # Request may have been delivered — do not retry.
            return result

        if result.retryable or is_retryable_error(result.error):
            for attempt in range(1, max_retries + 1):
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
                logger.warning(
                    "[%s] Send failed (attempt %d/%d, retrying in %.1fs): %s",
                    self.name,
                    attempt,
                    max_retries,
                    delay,
                    result.error,
                )
                await asyncio.sleep(delay)
                result = await self.send_message(recipient, chunk)
                if result.success:
                    logger.info("[%s] Send succeeded on retry %d", self.name, attempt)
                    return result
                if not (result.retryable or is_retryable_error(result.error)):
                    break
            else:
                logger.error(
                    "[%s] Delivery failed after %d retries: %s",
                    self.name,
                    max_retries,
                    result.error,
                )
                return result

        # Non-network failure (formatting, permissions): try plain text once.
        logger.warning(
            "[%s] Send failed (%s) — trying plain-text fallback",
            self.name,
            result.error,
        )
        fallback_text = f"(Response formatting failed, plain text:)\n\n{chunk[:3500]}"
        fallback = await self.send_message(recipient, fallback_text)
        return fallback if fallback.success else result

    # ── Pending message queue ──────────────────────────────────────────────

    def queue_pending(self, session_key: str, msg: ChannelMessage) -> None:
        """Queue a message to process after the current session finishes."""
        self._pending_messages[session_key] = msg

    def pop_pending(self, session_key: str) -> ChannelMessage | None:
        return self._pending_messages.pop(session_key, None)

    def has_pending(self, session_key: str) -> bool:
        return session_key in self._pending_messages

    # ── Typing indicator pause ─────────────────────────────────────────────

    def pause_typing(self, chat_id: str) -> None:
        """Pause typing indicator for a chat (e.g., during approval waits)."""
        self._typing_paused.add(chat_id)

    def resume_typing(self, chat_id: str) -> None:
        self._typing_paused.discard(chat_id)

    def is_typing_paused(self, chat_id: str) -> bool:
        return chat_id in self._typing_paused

    # ── Background task tracking ───────────────────────────────────────────

    def track_task(self, task: asyncio.Task) -> None:
        """Register a background task so `cancel_background_tasks` can reap it."""
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def cancel_background_tasks(self) -> None:
        """Cancel in-flight background tasks during adapter shutdown."""
        tasks = [t for t in self._background_tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._background_tasks.clear()
