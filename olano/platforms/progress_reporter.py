"""Delivery of live progress updates to a chat while the agent works.

:mod:`olano.platforms.progress` decides *what* a turn looks like; this
module decides *when and how* it reaches the user.  Three concerns live here:

**Decoupling.**  Pushes run on a background pump task, never inline in the
agent's event stream.  A Telegram flood-wait or a slow Slack API call must
cost the user a stale progress line, never a stalled agent — so
:meth:`ProgressReporter.feed` only folds the event into state and sets a
dirty flag, and the pump coalesces whatever accumulated into one push.

**Throttling.**  Every platform rate-limits edits, and a 40-tool turn would
otherwise produce 40 API calls.  The pump pushes at most once per interval
(and skips entirely when the rendered text is unchanged), with a longer
interval for append-mode platforms, where each update costs a new message and
a notification ping.

**Hand-off.**  When the answer is ready the progress message **vanishes** —
deleted just before the final answer is sent as its own new message, so the
chat is left with the question and the answer, and the reply triggers a
notification rather than silently mutating a bubble the user already read.
The work was visible while it mattered and leaves no residue.  Chats that
want the record instead set ``extra.progress_keep_log`` on the platform
config (or ``LIONCLAW_PROGRESS_KEEP_LOG=1``): the view is then re-rendered
one last time in its settled form and left in place.

Sinks abstract the two surfaces that stream turns: platform adapters
(:mod:`olano.platforms.base`), which may support editing, and classic
channels (:mod:`olano.channels.base`), which only ever send.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, Protocol

from olano.platforms.progress import (
    ProgressState,
    feed_event,
    render_progress,
)
from olano.platforms.verbosity import DEFAULT_VERBOSITY, MarkupDialect, normalize_verbosity

logger = logging.getLogger(__name__)

# Seconds between pushes. Editing is cheap and invisible, so it can be
# frequent; appending costs a new message (and a notification on most
# platforms), so it is deliberately slow.
EDIT_INTERVAL = 2.0
APPEND_INTERVAL = 8.0
# Give up after this many consecutive delivery failures — a platform that
# rejects progress updates (permissions, deleted chat, unsupported edit)
# should not be retried once per interval for the rest of a long turn.
MAX_CONSECUTIVE_FAILURES = 3
# Hard ceiling on append-mode messages per turn, so a pathological 200-tool
# run cannot bury the chat.
MAX_APPEND_MESSAGES = 15


class ProgressSink(Protocol):
    """Where a rendered progress view goes."""

    #: ``True`` when the sink updates one message in place (rather than
    #: posting a new one per update), which drives interval and hand-off.
    edits: bool

    async def push(self, text: str) -> bool:
        """Deliver *text*; return ``False`` if it could not be delivered."""
        ...

    async def clear(self) -> None:
        """Remove the progress view (no-op for append-only sinks)."""
        ...

    async def release(self) -> None:
        """Keep the progress view, but stop owning it."""
        ...


class AdapterProgressSink:
    """Sink backed by a :class:`BasePlatformAdapter`.

    The adapter owns the message bookkeeping (see
    ``BasePlatformAdapter.progress_update`` / ``progress_clear``) because some
    platforms have a message to reuse — Telegram adopts its existing
    "💭 Thinking…" bubble rather than posting a second one.
    """

    def __init__(self, adapter: Any, event: Any, *, edits: bool) -> None:
        self._adapter = adapter
        self._event = event
        self.edits = edits

    async def push(self, text: str) -> bool:
        return bool(await self._adapter.progress_update(self._event, text))

    async def clear(self) -> None:
        await self._adapter.progress_clear(self._event)

    async def release(self) -> None:
        self._adapter.progress_release(self._event)


class ChannelProgressSink:
    """Sink backed by a classic :class:`BaseChannel` (send-only)."""

    def __init__(self, channel: Any, recipient: str) -> None:
        self._channel = channel
        self._recipient = recipient
        self.edits = False

    async def push(self, text: str) -> bool:
        send = getattr(self._channel, "send", None)
        if not callable(send):
            return False
        await send(self._recipient, text)
        return True

    async def clear(self) -> None:
        """Nothing to clear — appended messages are already in the history."""

    async def release(self) -> None:
        """Nothing is owned in the first place."""


class ProgressReporter:
    """Streams a turn's progress to one chat.

    Lifecycle: :meth:`start`, then :meth:`feed` per streamed event, then
    :meth:`finish` exactly once.  All three are safe to call on a disabled
    reporter, so callers need no conditionals.
    """

    def __init__(
        self,
        sink: ProgressSink | None,
        *,
        verbosity: Any = DEFAULT_VERBOSITY,
        dialect: MarkupDialect | str | None = None,
        interval: float | None = None,
        keep_log: bool = False,
    ) -> None:
        self._sink = sink
        self._verbosity = normalize_verbosity(verbosity) or DEFAULT_VERBOSITY
        self._dialect = dialect
        self._keep_log = keep_log
        self._state = ProgressState()
        self._dirty = asyncio.Event()
        self._pump: asyncio.Task | None = None
        self._last_text = ""
        self._failures = 0
        self._sent = 0
        self._delivered = False
        if interval is not None:
            self._interval = interval
        else:
            self._interval = EDIT_INTERVAL if (sink and sink.edits) else APPEND_INTERVAL
        # ``quiet`` opts out of the play-by-play entirely.
        self._enabled = sink is not None and self._verbosity != "quiet"

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def delivered(self) -> bool:
        """Whether any progress actually reached the chat this turn.

        The final render consults this: when progress was streamed, a
        ``verbose`` answer must not repeat the whole tool log inline.
        """
        return self._delivered

    def start(self) -> None:
        """Spawn the pump. Idempotent; a no-op when disabled."""
        if not self._enabled or self._pump is not None:
            return
        self._pump = asyncio.create_task(self._run())

    def feed(self, kind: str, payload: Any) -> None:
        """Fold one ``astream_events`` tuple in. Never blocks, never raises."""
        if not self._enabled:
            return
        try:
            if feed_event(self._state, kind, payload):
                self._dirty.set()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("progress feed failed: %s", exc)

    async def finish(self) -> None:
        """Stop the pump and settle the progress view.

        Default hand-off: the view **vanishes** — the caller sends the final
        answer as a new message right after, so the chat keeps only question
        and answer. ``keep_log`` instead re-renders the settled log one last
        time and leaves it in place.
        """
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump
            self._pump = None
        if not self._enabled or not self._state.has_content:
            return
        try:
            if self._keep_log:
                final = render_progress(
                    self._state,
                    self._verbosity,
                    self._dialect,
                    settled=True,
                )
                # An append-mode sink already posted these lines as it went, so
                # only post the settled log if nothing ever got through.
                postable = self._sink is not None and (self._sink.edits or not self._delivered)
                if final and final != self._last_text and postable:
                    await self._sink.push(final)  # type: ignore[union-attr]
                # The log stays in the chat, so hand the message back — the
                # adapter's end-of-turn orphan sweep must not delete it.
                if self._sink is not None:
                    await self._sink.release()
                return
            # Not gated on _delivered: cancelling the pump mid-push can leave
            # a message posted but not yet recorded, and clear() is a no-op
            # when the adapter tracks nothing.
            if self._sink is not None:
                await self._sink.clear()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("progress finish failed: %s", exc)

    async def _run(self) -> None:
        """Coalescing pump: at most one push per interval, only when changed."""
        assert self._sink is not None
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            try:
                text = render_progress(self._state, self._verbosity, self._dialect)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("progress render failed: %s", exc)
                text = ""
            if text and text != self._last_text:
                if not self._sink.edits and self._sent >= MAX_APPEND_MESSAGES:
                    return
                try:
                    ok = await self._sink.push(text)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("progress push failed: %s", exc)
                    ok = False
                if ok:
                    self._last_text = text
                    self._sent += 1
                    self._delivered = True
                    self._failures = 0
                else:
                    self._failures += 1
                    if self._failures >= MAX_CONSECUTIVE_FAILURES:
                        logger.debug("progress disabled after %d failures", self._failures)
                        return
                    # Re-arm so a transient failure retries next interval even
                    # if no further events arrive — otherwise the last state
                    # would silently never reach the chat.
                    self._dirty.set()
            await asyncio.sleep(self._interval)


_OFF_VALUES = {"off", "false", "0", "no", "none", "disabled"}
_ON_VALUES = {"on", "true", "1", "yes", "enabled", "auto"}


def resolve_progress_mode(adapter: Any) -> str:
    """Effective delivery mode for *adapter*: ``edit``, ``append`` or ``off``.

    Precedence: ``LIONCLAW_PROGRESS_UPDATES`` env → the platform's
    ``extra.progress_updates`` config → the adapter's ``PROGRESS_MODE``
    default. ``on`` means "the adapter's own default", so enabling globally
    never forces progress onto a surface that opted out (email, SMS).
    """
    default = str(getattr(adapter, "PROGRESS_MODE", "off") or "off").lower()

    override: Any = os.getenv("LIONCLAW_PROGRESS_UPDATES")
    if override is None:
        extra = getattr(getattr(adapter, "config", None), "extra", None)
        if isinstance(extra, dict):
            override = extra.get("progress_updates")

    if override is None:
        return default if default in ("edit", "append") else "off"
    if isinstance(override, bool):
        return default if override else "off"
    value = str(override).strip().lower()
    if value in _OFF_VALUES:
        return "off"
    if value in _ON_VALUES:
        # "on" means "the adapter's default", never "edit anyway" — a surface
        # that opted out (email, SMS) stays out. Forcing it takes an explicit
        # "edit"/"append".
        return default if default in ("edit", "append") else "off"
    if value in ("edit", "append"):
        return value
    return default if default in ("edit", "append") else "off"


def _resolve_keep_log(adapter: Any) -> bool:
    """Whether the settled log should stay in the chat instead of vanishing.

    ``LIONCLAW_PROGRESS_KEEP_LOG`` env → ``extra.progress_keep_log`` config →
    default False (the view vanishes when the answer arrives).
    """
    override: Any = os.getenv("LIONCLAW_PROGRESS_KEEP_LOG")
    if override is None:
        extra = getattr(getattr(adapter, "config", None), "extra", None)
        if isinstance(extra, dict):
            override = extra.get("progress_keep_log")
    if override is None:
        return False
    if isinstance(override, bool):
        return override
    return str(override).strip().lower() in _ON_VALUES


def make_platform_reporter(
    adapter: Any,
    event: Any,
    *,
    verbosity: Any,
    dialect: MarkupDialect | str | None,
) -> ProgressReporter:
    """Reporter for a platform adapter, honouring its resolved progress mode."""
    mode = resolve_progress_mode(adapter)
    if mode not in ("edit", "append"):
        return ProgressReporter(None)
    sink = AdapterProgressSink(adapter, event, edits=mode == "edit")
    return ProgressReporter(
        sink, verbosity=verbosity, dialect=dialect, keep_log=_resolve_keep_log(adapter)
    )


def make_channel_reporter(
    channel: Any,
    recipient: str,
    *,
    verbosity: Any,
    dialect: MarkupDialect | str | None,
) -> ProgressReporter:
    """Reporter for a classic channel (append-only, or disabled)."""
    if not getattr(channel, "SUPPORTS_PROGRESS", False) or not recipient:
        return ProgressReporter(None)
    sink = ChannelProgressSink(channel, recipient)
    return ProgressReporter(sink, verbosity=verbosity, dialect=dialect)
