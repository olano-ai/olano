"""Regression tests for proactive notification delivery.

Channel adapters are bound to the runtime event loop they were registered
on. The notification tools run as sync LangChain tools in worker threads, so
_send_sync must schedule deliveries onto the registry's loop
(run_coroutine_threadsafe) — running them on a private asyncio.run loop
raised "attached to a different loop" / "no current event loop"
RuntimeErrors (the 2026-07-08 Telegram send failures).

Also covers the BasePlatformAdapter send_text/send_media shims that let
platform adapters (platform:telegram, …) register into the outbound
ChannelRegistry at all.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from olano.platforms import delivery
from olano.platforms.base import BasePlatformAdapter, SendResult
from olano.platforms.delivery import (
    BaseChannelAdapter,
    ChannelRegistry,
    MessageEnvelope,
)
from olano.tools.notification_tools import (
    _send_sync,
    _sending_agent,
    notification_send_text,
)
from olano.tools.talk_to import current_agent


class _RecordingAdapter(BaseChannelAdapter):
    channel_type = "fake"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.loop_seen: asyncio.AbstractEventLoop | None = None

    async def send_text(self, text: str, target: str, **kwargs: Any) -> str:
        self.loop_seen = asyncio.get_running_loop()
        self.sent.append((text, target))
        return "msg-42"


@pytest.fixture
def fresh_registry(monkeypatch) -> ChannelRegistry:
    registry = ChannelRegistry()
    monkeypatch.setattr(delivery, "_default_registry", registry)
    return registry


def _envelope(**overrides: Any) -> MessageEnvelope:
    fields: dict[str, Any] = {
        "text": "hi",
        "target": "123",
        "channel_type": "fake",
        "agent_name": "cindy",
    }
    fields.update(overrides)
    return MessageEnvelope(**fields)


async def test_register_captures_running_loop(fresh_registry):
    fresh_registry.register("cindy", "fake", _RecordingAdapter())
    assert fresh_registry.loop is asyncio.get_running_loop()


async def test_send_sync_delivers_on_registry_loop(fresh_registry):
    """The production shape: tool in a worker thread, adapters on this loop."""
    adapter = _RecordingAdapter()
    fresh_registry.register("cindy", "fake", adapter)

    result = await asyncio.to_thread(_send_sync, _envelope())

    assert result == "Message delivered on 'fake' (message id msg-42)."
    assert adapter.sent == [("hi", "123")]
    assert adapter.loop_seen is asyncio.get_running_loop()


def test_send_sync_without_runtime_loop(fresh_registry):
    """CLI/one-shot context: no loop anywhere — a private loop is fine."""
    adapter = _RecordingAdapter()
    fresh_registry.register("cindy", "fake", adapter)  # sync context: no loop bound
    assert fresh_registry.loop is None

    assert _send_sync(_envelope()) == "Message delivered on 'fake' (message id msg-42)."
    assert adapter.sent == [("hi", "123")]


async def test_send_sync_reports_missing_adapter(fresh_registry):
    fresh_registry.register("cindy", "fake", _RecordingAdapter())
    result = await asyncio.to_thread(_send_sync, _envelope(channel_type="telegram"))
    assert result.startswith("Notification failed")


class _DummyPlatform(BasePlatformAdapter):
    """Minimal concrete adapter for exercising the registry shims."""

    name = "dummy"

    def __init__(self) -> None:  # skip base wiring; shims only use send*/name
        self.agent_name = "cindy"
        self.sent: list[tuple[str, str]] = []
        self.documents: list[tuple[str, str, Optional[str]]] = []
        self.fail = False

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {}

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        if self.fail:
            return SendResult(success=False, error="boom")
        self.sent.append((chat_id, content))
        return SendResult(success=True, message_id="m-1")

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        **kwargs: Any,
    ) -> SendResult:
        self.documents.append((chat_id, file_path, caption))
        return SendResult(success=True, message_id="d-1")


async def test_platform_shim_send_text():
    adapter = _DummyPlatform()
    assert await adapter.send_text("hello", "123") == "m-1"
    assert adapter.sent == [("123", "hello")]


async def test_platform_shim_send_text_raises_on_failure():
    adapter = _DummyPlatform()
    adapter.fail = True
    with pytest.raises(RuntimeError, match="boom"):
        await adapter.send_text("hello", "123")


async def test_platform_shim_send_media_routes_local_document(tmp_path):
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-1.4")
    adapter = _DummyPlatform()

    msg_id = await adapter.send_media(str(report), "document", "123", caption="cap")

    assert msg_id == "d-1"
    assert adapter.documents == [("123", str(report), "cap")]


def test_sending_agent_resolves_from_runtime_context():
    """Identity is a runtime fact read from ``current_agent`` — not an LLM arg."""
    token = current_agent.set("marcus")
    try:
        assert _sending_agent() == "marcus"
    finally:
        current_agent.reset(token)


def test_sending_agent_fails_loud_when_unbound():
    """No bound agent → raise. No ``olano`` placeholder that owns no channels."""
    token = current_agent.set("")
    try:
        with pytest.raises(RuntimeError, match="no executing-agent context"):
            _sending_agent()
    finally:
        current_agent.reset(token)


async def test_notification_send_text_uses_bound_agent(fresh_registry):
    """The envelope's sender is the executing agent, resolved from context."""
    adapter = _RecordingAdapter()
    fresh_registry.register("marcus", "fake", adapter)

    token = current_agent.set("marcus")
    try:
        result = await asyncio.to_thread(
            notification_send_text.invoke,
            {"text": "hi", "channel": "fake", "target": "123"},
        )
    finally:
        current_agent.reset(token)

    assert result == "Message delivered on 'fake' (message id msg-42)."
    assert adapter.sent == [("hi", "123")]


async def test_notification_send_text_errors_without_agent_context(fresh_registry):
    """Unbound sender surfaces a tool error — never silently routes elsewhere."""
    fresh_registry.register("marcus", "fake", _RecordingAdapter())
    token = current_agent.set("")
    try:
        result = await asyncio.to_thread(
            notification_send_text.invoke,
            {"text": "hi", "channel": "fake", "target": "123"},
        )
    finally:
        current_agent.reset(token)

    assert result.startswith("[tool-error]")
    assert "no executing-agent context" in result


async def test_registry_send_media_envelope_uses_send_media(fresh_registry, tmp_path):
    """End-to-end: a media envelope reaches the platform adapter natively."""
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-1.4")
    adapter = _DummyPlatform()
    fresh_registry.register("cindy", "telegram", adapter)

    envelope = _envelope(
        channel_type="telegram",
        text="cap",
        media_url=str(report),
        media_type="document",
    )
    result = await asyncio.to_thread(_send_sync, envelope)

    assert result == "Attachment (document) delivered on 'telegram' (message id d-1)."
    assert adapter.documents == [("123", str(report), "cap")]


# ── target="owner" resolution ────────────────────────────────────────────────
# The notification toolkit defaults every ``target`` to the literal "owner".
# Nothing resolved it, so adapters received the sentinel itself — Telegram's
# ``int(chat_id)`` then raised ValueError, the registry swallowed it, and the
# agent was told it had "no telegram delivery", concluding its bot was
# unconfigured (2026-07-29: marcus could not send a generated image).


async def test_owner_target_resolves_to_current_chat(fresh_registry):
    """ "owner" means "reply here" when a turn is in flight on that channel."""
    from olano.security.request_context import push_request_context

    adapter = _RecordingAdapter()
    fresh_registry.register("marcus", "fake", adapter)

    with push_request_context(
        source="platform", channel="fake", is_group=False, metadata={"chat_id": "555"}
    ):
        result = await asyncio.to_thread(_send_sync, _envelope(agent_name="marcus", target="owner"))

    assert result == "Message delivered on 'fake' (message id msg-42)."
    assert adapter.sent == [("hi", "555")]  # never the literal "owner"


async def test_owner_target_falls_back_to_channel_config(fresh_registry, monkeypatch):
    """Outside a turn (scheduled/cron send), the configured chat is used."""
    from olano.config.agent_config import AgentConfig

    adapter = _RecordingAdapter()
    fresh_registry.register("marcus", "fake", adapter)
    config = AgentConfig(
        name="marcus",
        channels=[{"type": "fake", "allowed_users": ["5550001234", "8527871381"]}],
    )
    monkeypatch.setattr("olano.config.loader.load_agent_config", lambda name: config)

    result = await asyncio.to_thread(_send_sync, _envelope(agent_name="marcus", target="owner"))

    assert result == "Message delivered on 'fake' (message id msg-42)."
    assert adapter.sent == [("hi", "5550001234")]


async def test_unresolvable_owner_target_reports_what_to_configure(fresh_registry, monkeypatch):
    """No chat anywhere → name the config key, not a base-10 ValueError."""
    from olano.config.agent_config import AgentConfig

    fresh_registry.register("marcus", "fake", _RecordingAdapter())
    config = AgentConfig(name="marcus", channels=[])
    monkeypatch.setattr("olano.config.loader.load_agent_config", lambda name: config)

    result = await asyncio.to_thread(_send_sync, _envelope(agent_name="marcus", target="owner"))

    assert "cannot resolve target 'owner'" in result
    assert "default_chat_id" in result


async def test_explicit_target_is_never_rewritten(fresh_registry):
    """A real chat id passes through untouched, turn context notwithstanding."""
    from olano.security.request_context import push_request_context

    adapter = _RecordingAdapter()
    fresh_registry.register("marcus", "fake", adapter)

    with push_request_context(
        source="platform", channel="fake", is_group=False, metadata={"chat_id": "555"}
    ):
        result = await asyncio.to_thread(_send_sync, _envelope(agent_name="marcus", target="999"))

    assert result == "Message delivered on 'fake' (message id msg-42)."
    assert adapter.sent == [("hi", "999")]


async def test_bare_handle_target_fails_before_the_telegram_adapter(fresh_registry):
    """A username reaching Telegram's ``int(chat_id)`` is a dead end.

    The agent reads its own thread labels ("[from @jrivera (Jamie, id=…)]")
    and addresses the person the way it saw them written. With no "@" the
    target was neither address-shaped nor caught by the unresolved-handle
    guard, so it fell through delivery into the adapter and surfaced as
    ``ValueError: invalid literal for int() with base 10: 'jrivera'`` — a
    traceback in the runtime log and, for the tool, a reason naming nothing
    the agent could fix.
    """
    adapter = _DummyPlatform()
    fresh_registry.register("aurelius", "telegram", adapter)

    result = await asyncio.to_thread(
        _send_sync,
        _envelope(agent_name="aurelius", channel_type="telegram", target="jrivera"),
    )

    assert "invalid literal" not in result
    assert "never messaged" in result  # what to do about it
    assert adapter.sent == []  # never reached the adapter
    # …and the advice must not send it round the same loop: retrying a name
    # that can never be an address fails identically every time.
    assert "Do NOT retry" in result
    assert "chats_find" in result


async def test_telegram_adapter_names_a_non_numeric_chat_id(fresh_registry):
    """Defense in depth: paths that bypass target resolution still say why."""
    from unittest.mock import AsyncMock

    from olano.platforms.config import PlatformConfig
    from olano.platforms.telegram import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="t"), agent_name="aurelius")
    adapter._bot = AsyncMock()

    result = await adapter.send("jrivera", "hello")

    assert result.success is False
    assert "not a Telegram chat id" in (result.error or "")
    # Re-sending a name cannot make it numeric — don't spend retries on it.
    assert result.retryable is False
    adapter._bot.send_message.assert_not_awaited()


async def test_missing_channel_and_failed_delivery_report_differently(fresh_registry):
    """A wired-but-failing channel must not read as "no channel configured"."""
    adapter = _DummyPlatform()
    adapter.fail = True
    fresh_registry.register("marcus", "telegram", adapter)

    missing = await asyncio.to_thread(
        _send_sync, _envelope(agent_name="marcus", channel_type="discord")
    )
    assert "has no 'discord' channel" in missing
    assert "telegram" in missing  # tells the agent what it *does* have

    failed = await asyncio.to_thread(
        _send_sync, _envelope(agent_name="marcus", channel_type="telegram")
    )
    assert "boom" in failed  # the adapter's own reason, not a generic stand-in
    assert "has no" not in failed


# ── failures come back as text the agent can act on, never as exceptions ─────
# These tools are called by a model mid-conversation. A raised exception, or a
# message pointing at a log the agent cannot read, leaves it unable to tell
# "wrong path" from "no such channel" — which is how a working Telegram bot got
# reported to the user as unconfigured.


async def test_underlying_reason_survives_to_the_agent(fresh_registry, tmp_path):
    """A dead file path must come back as *that*, not "see runtime logs"."""
    adapter = _DummyPlatform()
    fresh_registry.register("marcus", "telegram", adapter)

    result = await asyncio.to_thread(
        _send_sync,
        _envelope(
            agent_name="marcus",
            channel_type="telegram",
            media_url="file:///nope/ghost.png",
            media_type="image",
        ),
    )

    assert "no such file" in result
    assert "ghost.png" in result
    assert "runtime logs" not in result
    assert "ls" in result  # concrete next step


@pytest.mark.parametrize(
    "envelope_kwargs",
    [
        {"channel_type": "nope"},  # unknown channel
        {"media_url": "file:///nope/ghost.png", "media_type": "image"},  # dead path
        {"target": "owner"},  # unresolvable sentinel (agent has no config)
    ],
)
async def test_every_failure_returns_a_string_and_never_raises(fresh_registry, envelope_kwargs):
    fresh_registry.register("marcus", "telegram", _DummyPlatform())
    fields = {"agent_name": "marcus", "channel_type": "telegram", **envelope_kwargs}

    result = await asyncio.to_thread(_send_sync, _envelope(**fields))

    assert isinstance(result, str) and result
    if result.startswith("Notification failed"):
        # Always ends with something to try next, never a dangling diagnosis.
        assert result.rstrip().endswith((".", "…")) and len(result) > len("Notification failed: .")
