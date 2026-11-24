"""Outbound message delivery layer — envelopes, adapters, registry.

This is the unified v2 outbound API originally in ``olano.channels``.
Moved here so ``olano.platforms`` owns both inbound (``base.py``) and
outbound delivery. Legacy ``olano.channels.adapter`` and
``olano.channels.registry`` re-export from this module during the
migration; new code should import from :mod:`olano.platforms`.

Tier 1 (core) — every adapter implements:
    send_text, send_media

Tier 2 (extended) — rich adapters:
    edit_message, delete_message, react_message

Tier 3 (specialized) — advanced adapters:
    stream_response (edit-in-place streaming)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from olano.platforms.targets import resolve_target

logger = logging.getLogger(__name__)


@dataclass
class MessageEnvelope:
    """Normalized outbound message."""

    text: str
    target: str  # chat_id, channel, DM target, etc.
    channel_type: str  # "telegram" | "discord" | "slack" | ...
    agent_name: str = ""
    reply_to: str | None = None
    media_url: str | None = None
    media_type: str | None = None  # "image" | "video" | "audio" | "document"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedInbound:
    """Normalized inbound message from any channel (legacy v2 shape).

    Retained for the outbound-delivery path; new platform adapters
    produce richer :class:`~olano.platforms.base.MessageEvent`
    instances instead.
    """

    text: str
    user_id: str
    channel_type: str
    chat_id: str
    thread_id: str
    message_id: str = ""
    username: str = ""
    is_group: bool = False
    media_urls: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    raw: Any = None


@runtime_checkable
class ChannelAdapter(Protocol):
    """Structural protocol for outbound-capable adapters."""

    channel_type: str

    async def send_text(self, text: str, target: str, **kwargs: Any) -> str: ...

    async def send_media(
        self,
        media_url: str,
        media_type: str,
        target: str,
        caption: str = "",
        **kwargs: Any,
    ) -> str: ...

    async def edit_message(self, message_id: str, new_text: str, target: str) -> bool: ...

    async def delete_message(self, message_id: str, target: str) -> bool: ...

    async def react_message(self, message_id: str, emoji: str, target: str) -> bool: ...

    async def stream_response(
        self,
        message_id: str,
        text_chunks: AsyncIterator[str],
        target: str,
    ) -> None: ...


class BaseChannelAdapter:
    """Base class with default no-op Tier 2/3 implementations."""

    channel_type: str = "base"

    async def send_text(self, text: str, target: str, **kwargs: Any) -> str:
        raise NotImplementedError

    async def send_media(
        self,
        media_url: str,
        media_type: str,
        target: str,
        caption: str = "",
        **kwargs: Any,
    ) -> str:
        logger.warning("%s: send_media not implemented", self.channel_type)
        return await self.send_text(f"[media: {media_url}]", target)

    async def edit_message(self, message_id: str, new_text: str, target: str) -> bool:
        logger.debug("%s: edit_message not supported", self.channel_type)
        return False

    async def delete_message(self, message_id: str, target: str) -> bool:
        logger.debug("%s: delete_message not supported", self.channel_type)
        return False

    async def react_message(self, message_id: str, emoji: str, target: str) -> bool:
        logger.debug("%s: react_message not supported", self.channel_type)
        return False

    async def stream_response(
        self,
        message_id: str,
        text_chunks: AsyncIterator[str],
        target: str,
    ) -> None:
        parts: list[str] = []
        async for chunk in text_chunks:
            parts.append(chunk)
        if parts:
            await self.send_text("".join(parts), target)

    async def start(self) -> None:
        """Start listening for inbound messages."""

    async def stop(self) -> None:
        """Stop the adapter cleanly."""

    def normalize_routing_key(self, chat_type: str, chat_id: str, agent: str) -> str:
        return f"{agent}:{self.channel_type}:{chat_type}:{chat_id}"


class ChannelRegistry:
    """Central registry for outbound message delivery.

    Adapters are keyed by ``(agent_name, channel_type)`` so multiple agents
    can each own their own e.g. Telegram bot, WhatsApp number, or Slack
    workspace without colliding. Callers route to a specific instance by
    setting ``MessageEnvelope.agent_name`` — if left empty the registry
    falls back to the (single, ambiguous) adapter registered for that
    channel type, logging a warning so the unrouted call is visible.
    """

    def __init__(self) -> None:
        self._adapters: dict[tuple[str, str], BaseChannelAdapter] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        """Event loop the adapters live on (captured at registration).

        Adapters hold loop-bound resources (python-telegram-bot applications,
        aiohttp/httpx sessions, …), so senders in other threads must schedule
        deliveries onto this loop (``asyncio.run_coroutine_threadsafe``) —
        running them on a private loop raises "attached to a different loop"
        RuntimeErrors. ``None`` when registration happened outside any loop.
        """
        loop = self._loop
        if loop is not None and loop.is_closed():
            return None
        return loop

    def adapters_for(self, agent_name: str) -> dict[str, BaseChannelAdapter]:
        """One agent's live adapters, keyed by channel type (read-only view)."""
        return {
            channel_type: adapter
            for (agent, channel_type), adapter in self._adapters.items()
            if agent == agent_name
        }

    # ── registration ──────────────────────────────────────────────────
    def register(
        self,
        agent_name: str,
        channel_type: str,
        adapter: BaseChannelAdapter,
    ) -> None:
        self._adapters[(agent_name, channel_type)] = adapter
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass  # registered from sync context (CLI, tests) — no loop to bind
        logger.info("ChannelRegistry: registered '%s' for agent '%s'", channel_type, agent_name)

    def unregister(self, agent_name: str, channel_type: str) -> None:
        self._adapters.pop((agent_name, channel_type), None)

    def unregister_agent(self, agent_name: str) -> None:
        for key in [k for k in self._adapters if k[0] == agent_name]:
            self._adapters.pop(key, None)

    # ── lookups ───────────────────────────────────────────────────────
    def get(self, agent_name: str, channel_type: str) -> BaseChannelAdapter | None:
        return self._adapters.get((agent_name, channel_type))

    def for_agent(self, agent_name: str) -> dict[str, BaseChannelAdapter]:
        """Return ``{channel_type: adapter}`` for one agent."""
        return {
            ctype: adapter
            for (owner, ctype), adapter in self._adapters.items()
            if owner == agent_name
        }

    def for_channel(self, channel_type: str) -> dict[str, BaseChannelAdapter]:
        """Return ``{agent_name: adapter}`` for one channel type."""
        return {
            owner: adapter
            for (owner, ctype), adapter in self._adapters.items()
            if ctype == channel_type
        }

    def _resolve(self, agent_name: str, channel_type: str) -> BaseChannelAdapter | None:
        """Look up by ``(agent, channel)``; if no agent given, fall back
        to any adapter of that channel type (and warn — the call is
        ambiguous when more than one agent owns this channel type)."""
        if agent_name:
            adapter = self._adapters.get((agent_name, channel_type))
            if adapter is not None:
                return adapter
            # Caller specified an agent but it isn't registered — refuse
            # to silently route to a different agent's adapter.
            logger.warning(
                "ChannelRegistry: agent '%s' has no '%s' adapter registered",
                agent_name,
                channel_type,
            )
            return None
        peers = self.for_channel(channel_type)
        if not peers:
            return None
        if len(peers) == 1:
            return next(iter(peers.values()))
        logger.warning(
            "ChannelRegistry: ambiguous routing — %d agents own '%s' (%s). "
            "Set MessageEnvelope.agent_name to disambiguate.",
            len(peers),
            channel_type,
            ", ".join(sorted(peers)),
        )
        return next(iter(peers.values()))

    # ── delivery ──────────────────────────────────────────────────────
    async def send(self, envelope: MessageEnvelope, *, raise_on_error: bool = False) -> str | None:
        """Deliver one envelope; returns the platform message id.

        Returns ``None`` on failure for fire-and-forget callers (PULSE digests,
        the MCP OAuth notice) that have nobody to report to. Callers that *do*
        have somebody — the agent-facing notification tools — pass
        ``raise_on_error=True`` so the underlying reason survives as text
        instead of collapsing into a ``None`` indistinguishable from "no
        adapter". A tool cannot read the runtime log; whatever it is handed is
        all the agent gets to recover from.
        """
        adapter = self._resolve(envelope.agent_name, envelope.channel_type)
        if not adapter:
            logger.warning(
                "ChannelRegistry: no adapter for channel type '%s' (agent='%s')",
                envelope.channel_type,
                envelope.agent_name or "<unspecified>",
            )
            if raise_on_error:
                raise LookupError(
                    f"agent '{envelope.agent_name or '<unspecified>'}' has no "
                    f"'{envelope.channel_type}' channel registered"
                )
            return None
        try:
            # Every outbound tool defaults ``target`` to the literal "owner".
            # Resolve that sentinel here — the one choke point every envelope
            # passes through — so it can never reach an adapter, where it used
            # to blow up as ``int('owner')``. Real addresses pass through.
            target = resolve_target(envelope.agent_name, envelope.channel_type, envelope.target)
            # Forward the envelope's metadata (thread_id for forum topics /
            # platform threads, …) — dropping it here silently strips topic
            # routing from every proactive send. All adapter shims accept it
            # via **kwargs.
            metadata = dict(envelope.metadata) if envelope.metadata else None
            if envelope.media_url:
                result = await adapter.send_media(
                    media_url=envelope.media_url,
                    media_type=envelope.media_type or "document",
                    target=target,
                    caption=envelope.text,
                    metadata=metadata,
                )
            else:
                result = await adapter.send_text(envelope.text, target, metadata=metadata)
        except Exception as exc:
            logger.error(
                "ChannelRegistry: delivery failed via %s (agent=%s): %s",
                envelope.channel_type,
                envelope.agent_name or "<unspecified>",
                exc,
            )
            if raise_on_error:
                raise
            return None
        # Fold the delivered content into the target conversation's history so
        # a user replying to it there doesn't face an agent that has never
        # seen it. Best-effort: never fails a delivery that succeeded. A None
        # result is a failed send (callers treat it as such) — nothing landed,
        # so nothing is recorded.
        if result is not None:
            try:
                from olano.platforms.context_log import record_outbound_delivery

                record_outbound_delivery(envelope, adapter, target)
            except Exception:
                logger.debug("outbound context recording failed", exc_info=True)
        return result

    async def broadcast(
        self,
        text: str,
        targets: dict[str, str],
        *,
        agent_name: str = "",
    ) -> dict[str, str | None]:
        """Fan-out one text to many channel types for a single agent.

        ``targets`` maps ``channel_type → target``. To broadcast across
        all agents at once, call this once per agent.
        """
        results: dict[str, str | None] = {}
        for channel_type, target in targets.items():
            envelope = MessageEnvelope(
                text=text,
                target=target,
                channel_type=channel_type,
                agent_name=agent_name,
            )
            results[channel_type] = await self.send(envelope)
        return results

    # ── introspection ─────────────────────────────────────────────────
    def list_channels(self) -> list[tuple[str, str]]:
        """Returns ``[(agent_name, channel_type), ...]`` for every registered adapter."""
        return list(self._adapters)

    def has_channel(self, channel_type: str, agent_name: str = "") -> bool:
        if agent_name:
            return (agent_name, channel_type) in self._adapters
        return any(ctype == channel_type for _, ctype in self._adapters)


_default_registry: ChannelRegistry | None = None


def get_channel_registry() -> ChannelRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = ChannelRegistry()
    return _default_registry
