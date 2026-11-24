"""Notification tools — send messages via ChannelRegistry.

These tools allow the agent to proactively push messages to any configured
channel (Telegram, Discord, Slack, Email, etc.) using the unified ChannelRegistry.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Annotated, Any, cast

from langchain_core.tools import tool

if TYPE_CHECKING:
    from olano.platforms import MessageEnvelope

logger = logging.getLogger(__name__)

_DELIVERY_TIMEOUT = 30.0


def _deliver_sync(coro_factory: Callable[[], Coroutine[Any, Any, Any]]) -> Any:
    """Run an async delivery coroutine from a sync tool context.

    Channel adapters are bound to the runtime loop they were registered on
    (python-telegram-bot applications, aiohttp/httpx sessions, …), so the
    delivery must be *scheduled onto that loop*. Sync tools execute in worker
    threads, where ``run_coroutine_threadsafe`` can safely block for the
    result. Running the coroutine on a private loop instead raises
    "attached to a different loop" / "no current event loop" RuntimeErrors.
    """
    from olano.platforms import get_channel_registry

    registry = get_channel_registry()
    loop = registry.loop
    try:
        running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if loop is not None and loop.is_running():
        if running is loop:
            # Called on the adapters' own loop — blocking here would freeze
            # the runtime, so hand the coroutine to the loop and return.
            loop.create_task(coro_factory())
            return "Delivery scheduled on the runtime loop."
        future = asyncio.run_coroutine_threadsafe(coro_factory(), loop)
        return future.result(timeout=_DELIVERY_TIMEOUT)
    if running is not None:
        raise RuntimeError("channel adapters' event loop is not available from this async context")
    # No runtime loop at all (CLI one-shot, tests): the adapters were created
    # in this same sync world, so a private loop is safe.
    return asyncio.run(coro_factory())


def _recovery_hint(envelope: MessageEnvelope, reason: str) -> str:
    """The next thing the agent should try, given why the send failed.

    These tools are called by a model mid-conversation, so a failure has to come
    back as something it can act on. "See runtime logs" is not that — the agent
    cannot read them, and an agent that cannot tell "the file path is wrong"
    from "this channel does not exist" concludes its channel is broken and stops
    trying. That is the exact loop this whole module was fixed to end.
    """
    lowered = reason.lower()
    if "no such file" in lowered:
        return (
            " The path did not resolve on the host. Confirm it with `ls`, then retry with the "
            "absolute path — or just mention the path in your reply and it is attached "
            "automatically."
        )
    if "cannot resolve target" in lowered:
        return " Pass the chat id explicitly as `target` instead of leaving it as 'owner'."
    if "is ambiguous on" in lowered:
        # The error already lists every candidate with its id; the only useful
        # next step is to pick one, and re-sending the same name cannot.
        return (
            " Do NOT retry the same name — pick one of the listed candidates and pass its id "
            "as `target`, or ask the user which they meant."
        )
    if "is not a valid" in lowered and "address" in lowered:
        # A name-shaped target on a channel that addresses by id. Retrying is
        # guaranteed to fail identically, and the generic "retry once" advice
        # below would send the agent round that loop — which is how a handle
        # read off a "[from @someone]" label turned into a repeated failure.
        return (
            " Do NOT retry the same target — it can never be a valid address on this channel. "
            "Look the id up with `chats_find` / `chats_list` and pass that as `target`; if the "
            "person has never messaged this agent it knows no id for them, so say so in your "
            "reply instead of retrying."
        )
    if "not a telegram chat id" in lowered:
        return (
            " Do NOT retry the same target — Telegram addresses chats by numeric id. Use "
            "`chats_find` / `chats_list` to look one up."
        )
    if "has no" in lowered and "channel" in lowered:
        return " Use one of this agent's configured channels, or reply in the conversation instead."
    if "too big" in lowered or "file is too large" in lowered or "413" in lowered:
        return " The file exceeds the platform's upload limit — send a smaller file or a link."
    return " Retry once; if it fails again, tell the user what you were trying to send."


def _send_sync(envelope: MessageEnvelope) -> str:
    """Deliver one envelope from a sync tool; returns a human-readable result.

    Never raises: every outcome — success, misconfiguration, a dead file path —
    comes back as a string the calling agent can read and act on.
    """
    from olano.platforms import get_channel_registry

    registry = get_channel_registry()
    # Distinguish "this agent owns no such channel" from "the channel exists but
    # the send failed". Reporting the former for a delivery error tells the agent
    # its channel isn't configured — which is how a working Telegram bot got
    # reported as "no Telegram delivery configured" when the real fault was an
    # unresolved target.
    if registry.get(envelope.agent_name, envelope.channel_type) is None:
        owned = sorted(registry.for_agent(envelope.agent_name))
        have = f"configured channels: {', '.join(owned)}" if owned else "no channels configured"
        reason = f"agent '{envelope.agent_name}' has no '{envelope.channel_type}' channel ({have})"
        return f"Notification failed: {reason}.{_recovery_hint(envelope, reason)}"
    try:
        # raise_on_error keeps the underlying reason attached to the failure;
        # without it the registry returns a bare None and the actionable text
        # ("no such file: …") is lost to the log the agent cannot read.
        result = _deliver_sync(lambda: registry.send(envelope, raise_on_error=True))
    except Exception as e:
        reason = str(e) or e.__class__.__name__
        return f"Notification failed: {reason}.{_recovery_hint(envelope, reason)}"
    if result is None:
        reason = (
            f"'{envelope.channel_type}' delivery to target '{envelope.target}' returned nothing"
        )
        return f"Notification failed: {reason}.{_recovery_hint(envelope, reason)}"
    # Report success in words. Returning the bare platform message id made a
    # model read "451" as an HTTP 451 and tell the user delivery was blocked —
    # while the message had in fact been delivered.
    what = "Message" if not envelope.media_url else f"Attachment ({envelope.media_type or 'file'})"
    detail = f" (message id {result})" if str(result).strip() else ""
    return f"{what} delivered on '{envelope.channel_type}'{detail}."


def _sending_agent() -> str:
    """The agent executing this turn — the deterministic notification sender.

    The runner binds ``current_agent`` to the running agent for every turn
    (``agent_runtime_context`` in ``core/runner``), and per-agent tools already
    read it for credential resolution. So a notification tool *always* knows who
    is sending; the sender is a runtime fact, never an LLM-supplied argument.

    If the binding is missing the tool ran outside an agent turn — a real wiring
    defect, not a missing default — so we fail loudly. Routing to a placeholder
    agent instead (the old ``agent_name="olano"`` default) silently targets a
    channel owner that owns no adapters, so every delivery is dropped with only a
    "has no adapter" warning that hides the true cause.
    """
    from olano.tools.talk_to import current_agent

    name = current_agent.get()
    if not name:
        logger.error(
            "notification tool invoked with no executing-agent context "
            "(current_agent unset); the runtime did not bind a sender identity "
            "for this turn."
        )
        raise RuntimeError("no executing-agent context for notification delivery")
    return name


@tool
def notification_send_text(
    text: Annotated[str, "Message text to send"],
    channel: Annotated[str, "Channel type: telegram, discord, slack, email"],
    target: Annotated[
        str,
        "Target chat ID, channel ID, chat name, or 'owner'. Telegram forum "
        "topic: append ':<thread id>' to the chat id (e.g. '-1004366376806:3' "
        "posts into that topic; the bare chat id posts to General).",
    ] = "owner",
) -> str:
    """Send a text notification to any configured channel.

    ``target`` accepts a known chat's NAME too — it resolves through the chat
    directory when unambiguous (use chats_find to preview the match).
    """
    try:
        from olano.platforms import MessageEnvelope

        envelope = MessageEnvelope(
            text=text,
            target=target,
            channel_type=channel,
            agent_name=_sending_agent(),
        )
        return _send_sync(envelope)
    except Exception as exc:
        return f"[tool-error] notification_send_text: {exc}"


@tool
def notification_broadcast(
    text: Annotated[str, "Message to broadcast to all channels"],
) -> str:
    """Broadcast a text message to ALL of this agent's configured channels."""
    try:
        from olano.platforms import get_channel_registry

        agent_name = _sending_agent()
        registry = get_channel_registry()

        async def _run() -> str:
            targets = {
                channel_type: "owner"
                for name, channel_type in registry.list_channels()
                if name == agent_name
            }
            results = await registry.broadcast(text, targets, agent_name=agent_name)
            return f"Broadcast sent to {len(results)} channels."

        return cast(str, _deliver_sync(_run))
    except Exception as e:
        return f"[tool-error] notification_broadcast: {e}"


@tool
def notification_send_photo(
    photo_url: Annotated[str, "Local image file path or public image URL"],
    caption: Annotated[str, "Optional caption"] = "",
    channel: Annotated[str, "Channel type: telegram, discord, slack"] = "telegram",
    target: Annotated[
        str,
        "Target chat ID, or 'owner' (default) for the current/owner chat. "
        "Telegram forum topic: '<chat id>:<thread id>' (e.g. '-100123:3').",
    ] = "owner",
) -> str:
    """Send a photo/image to a channel as a native attachment.

    Accepts a local file path (e.g. an image just generated into the workspace)
    or a public URL; a local path is uploaded as a real photo with the caption.
    Leave ``target`` as ``"owner"`` to send to the chat this turn came from.
    """
    try:
        from olano.platforms import MessageEnvelope

        # media_url/media_type route the envelope through the adapter's
        # send_media so the image arrives as a native attachment; the text
        # becomes its caption.
        envelope = MessageEnvelope(
            text=caption,
            target=target,
            channel_type=channel,
            agent_name=_sending_agent(),
            media_url=photo_url,
            media_type="image",
            metadata={"photo_url": photo_url, "caption": caption, "media_type": "photo"},
        )
        return _send_sync(envelope)
    except Exception as exc:
        return f"[tool-error] notification_send_photo: {exc}"


@tool
def notification_send_document(
    file_path: Annotated[str, "Local file path or URL to send"],
    caption: Annotated[str, "Optional caption"] = "",
    channel: Annotated[str, "Channel type"] = "telegram",
    target: Annotated[
        str,
        "Target chat ID, or 'owner' (default) for the current/owner chat. "
        "Telegram forum topic: '<chat id>:<thread id>' (e.g. '-100123:3').",
    ] = "owner",
) -> str:
    """Send a document/file to a channel as a native attachment.

    Leave ``target`` as ``"owner"`` to send to the chat this turn came from.
    """
    try:
        from olano.platforms import MessageEnvelope

        # media_url/media_type route the envelope through the adapter's
        # send_media so the file arrives as a native attachment (e.g. a real
        # Telegram document); the text becomes its caption.
        envelope = MessageEnvelope(
            text=caption,
            target=target,
            channel_type=channel,
            agent_name=_sending_agent(),
            media_url=file_path,
            media_type="document",
            metadata={"file_path": file_path, "media_type": "document"},
        )
        return _send_sync(envelope)
    except Exception as exc:
        return f"[tool-error] notification_send_document: {exc}"


@tool
def notification_send_poll(
    question: Annotated[str, "Poll question"],
    options: Annotated[list[str], "Poll options (2-10 items)"],
    channel: Annotated[str, "Channel type"] = "telegram",
    target: Annotated[str, "Target"] = "owner",
) -> str:
    """Send a poll to a channel (supported on Telegram, Discord)."""
    try:
        from olano.platforms import MessageEnvelope

        text = f"📊 **{question}**\n" + "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
        envelope = MessageEnvelope(
            text=text,
            target=target,
            channel_type=channel,
            agent_name=_sending_agent(),
            metadata={"question": question, "options": options, "media_type": "poll"},
        )
        return _send_sync(envelope)
    except Exception as exc:
        return f"[tool-error] notification_send_poll: {exc}"


@tool
def notification_send_to_subscribers(
    text: Annotated[str, "Message to send to every paying/trialing subscriber"],
    channel: Annotated[str, "Channel type: telegram, discord, slack"] = "telegram",
) -> str:
    """Send a message to this agent's entitled subscribers only.

    For monetized agents: fans the message out to every end-user whose
    subscription is active (or still in a valid trial), and NEVER to canceled or
    expired users. Use this for scheduled digests/alerts (e.g. the twice-daily
    tender push) so lapsed users stop receiving paid content. If the agent has
    no entitlement records yet, nothing is sent.
    """
    try:
        from olano.monetization.service import entitled_user_ids
        from olano.platforms import MessageEnvelope

        agent_name = _sending_agent()
        user_ids = entitled_user_ids(agent_name, channel)
        if not user_ids:
            return f"No entitled subscribers for agent '{agent_name}' on '{channel}'."
        sent = 0
        failures = 0
        for uid in user_ids:
            envelope = MessageEnvelope(
                text=text, target=str(uid), channel_type=channel, agent_name=agent_name
            )
            result = _send_sync(envelope)
            if isinstance(result, str) and result.startswith("Notification failed"):
                failures += 1
            else:
                sent += 1
        summary = f"Sent to {sent}/{len(user_ids)} subscribers on '{channel}'."
        return summary if not failures else f"{summary} ({failures} failed — see logs)."
    except Exception as exc:
        return f"[tool-error] notification_send_to_subscribers: {exc}"


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
