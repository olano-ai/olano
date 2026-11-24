"""Context-only conversation notes and their turn-start injection.

Two gaps these close:

1. Telegram Business ("secretary") chats — the owner's own messages in a
   connected chat arrived as ``business_message`` updates and were dropped
   entirely, so the agent replying as the owner never saw half the thread.
2. Proactive deliveries (notification tools, PULSE, scheduled digests) sent
   from *outside* a chat's own conversation were never recorded there, so a
   user replying to a notification faced an agent that had never seen it.

Both now flow through ``AgentRunner.record_context_note`` (JSONL system row +
queued note in ``<thread>.state.json``) and are injected once as a system
message at the start of the thread's next turn.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from olano.config.agent_config import AgentConfig
from olano.core.runner import PENDING_CONTEXT_NOTES_KEY, AgentRunner


@pytest.fixture
def runner(mock_agent, base_config: AgentConfig) -> AgentRunner:
    return AgentRunner(mock_agent, base_config)


# ── record_context_note ─────────────────────────────────────────────────


def test_record_persists_transcript_and_queue(runner: AgentRunner) -> None:
    assert runner.record_context_note("chat1", "the owner said hi") == "chat1"

    session = runner.load_session("chat1")
    assert session[-1]["role"] == "system"
    assert session[-1]["content"] == "the owner said hi"

    notes = runner.load_thread_state("chat1")[PENDING_CONTEXT_NOTES_KEY]
    assert [n["text"] for n in notes] == ["the owner said hi"]
    assert notes[0]["ts"]  # timestamped for the injected rendering


def test_record_follows_active_thread(runner: AgentRunner) -> None:
    # /new swaps the chat's active thread; a note recorded against the base
    # chat id must land where the next reply will actually run.
    runner.save_thread_state("chat1", {"active_thread": "t2"})

    assert runner.record_context_note("chat1", "note") == "t2"

    assert runner.load_session("t2")[-1]["content"] == "note"
    assert runner.load_thread_state("t2")[PENDING_CONTEXT_NOTES_KEY][0]["text"] == "note"
    assert not runner.load_thread_state("chat1").get(PENDING_CONTEXT_NOTES_KEY)


def test_record_skips_currently_executing_thread(runner: AgentRunner) -> None:
    # A notification sent back into the conversation the agent is serving is
    # already in that thread's history as the tool call + result.
    assert runner.record_context_note("chat1", "dup", skip_thread_id="chat1") is None
    assert runner.load_session("chat1") == []
    assert not runner.load_thread_state("chat1").get(PENDING_CONTEXT_NOTES_KEY)


def test_record_rejects_empty_input(runner: AgentRunner) -> None:
    assert runner.record_context_note("", "note") is None
    assert runner.record_context_note("chat1", "   ") is None
    assert runner.load_session("chat1") == []


def test_queue_is_bounded(runner: AgentRunner) -> None:
    for i in range(25):
        runner.record_context_note("chat1", f"note {i}")
    notes = runner.load_thread_state("chat1")[PENDING_CONTEXT_NOTES_KEY]
    assert len(notes) == 20
    assert notes[-1]["text"] == "note 24"
    assert notes[0]["text"] == "note 5"  # oldest dropped
    # The JSONL transcript keeps the full record regardless.
    assert len(runner.load_session("chat1")) == 25


# ── record_turn_error ───────────────────────────────────────────────────


def test_turn_error_reaches_the_next_turn(runner: AgentRunner) -> None:
    """An error the user was shown must be a fact the agent can read.

    The frame is drawn client-side and the assistant's row is written before it,
    so without this the agent's own transcript says the turn went fine — and
    "what is this error?" is answered by guessing from the wording.
    """
    runner.record_turn_error(
        "chat1", "Could not determine home directory.", code="generic_error", stage="after_reply"
    )

    note = runner.load_thread_state("chat1")[PENDING_CONTEXT_NOTES_KEY][0]["text"]
    assert "Could not determine home directory." in note
    assert "delivered in full" in note  # nothing to redo
    assert "generic_error" not in note  # a code the user never sees is noise
    assert runner.load_session("chat1")[-1]["role"] == "system"


def test_turn_error_stage_says_what_the_user_received(runner: AgentRunner) -> None:
    runner.record_turn_error("t-partial", "Upstream ended the stream.", stage="partial")
    runner.record_turn_error("t-turn", "Rate limit reached.", code="rate_limit", stage="turn")

    partial = runner.load_thread_state("t-partial")[PENDING_CONTEXT_NOTES_KEY][0]["text"]
    failed = runner.load_thread_state("t-turn")[PENDING_CONTEXT_NOTES_KEY][0]["text"]
    assert "cut off" in partial
    assert "previous turn failed" in failed
    assert "(code: rate_limit)" in failed


def test_turn_error_ignores_empty_and_bounds_length(runner: AgentRunner) -> None:
    runner.record_turn_error("chat1", "   ")
    assert not runner.load_thread_state("chat1").get(PENDING_CONTEXT_NOTES_KEY)

    runner.record_turn_error("chat2", "x" * 5000)
    note = runner.load_thread_state("chat2")[PENDING_CONTEXT_NOTES_KEY][0]["text"]
    assert note.count("x") == 400


# ── turn-start injection ────────────────────────────────────────────────


def _capturing_agent() -> tuple[MagicMock, dict]:
    captured: dict = {}

    async def capturing_astream(payload, **kwargs):
        captured["payload"] = payload
        if False:  # pragma: no cover - makes this an async generator
            yield None

    agent = MagicMock()
    agent.astream = capturing_astream
    agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    agent.ainvoke = AsyncMock(return_value={"messages": [MagicMock(type="ai", content="ok")]})
    return agent, captured


def _system_contents(payload: dict) -> list[str]:
    return [m["content"] for m in payload["messages"] if m.get("role") == "system"]


async def test_astream_events_injects_and_drains_notes(base_config: AgentConfig) -> None:
    agent, captured = _capturing_agent()
    runner = AgentRunner(agent, base_config)
    runner.record_context_note("chat1", 'You sent this message to this chat: "hello"')

    async for _ in runner.astream_events("user reply", thread_id="chat1"):
        pass

    notices = _system_contents(captured["payload"])
    assert any("hello" in n and "Context updates" in n for n in notices)
    assert captured["payload"]["messages"][-1] == {"role": "user", "content": "user reply"}
    assert runner.load_thread_state("chat1")[PENDING_CONTEXT_NOTES_KEY] == []

    # Drained: the next turn injects nothing.
    captured.clear()
    async for _ in runner.astream_events("again", thread_id="chat1"):
        pass
    assert not any("hello" in n for n in _system_contents(captured["payload"]))


async def test_internal_turns_leave_notes_queued(base_config: AgentConfig) -> None:
    agent, captured = _capturing_agent()
    runner = AgentRunner(agent, base_config)
    runner.record_context_note("chat1", "queued note")

    async for _ in runner.astream_events("directive", thread_id="chat1", internal_message=True):
        pass

    assert not any("queued note" in n for n in _system_contents(captured["payload"]))
    notes = runner.load_thread_state("chat1")[PENDING_CONTEXT_NOTES_KEY]
    assert [n["text"] for n in notes] == ["queued note"]


async def test_ainvoke_injects_notes(base_config: AgentConfig) -> None:
    # `ainvoke` is a wrapper over the stream path, so the note reaches the
    # model through the same payload `astream_events` builds.
    agent, captured = _capturing_agent()
    runner = AgentRunner(agent, base_config)
    runner.record_context_note("chat1", "queued note")

    await runner.ainvoke("hello", thread_id="chat1")

    assert any("queued note" in n for n in _system_contents(captured["payload"]))
    assert runner.load_thread_state("chat1")[PENDING_CONTEXT_NOTES_KEY] == []


# ── outbound delivery recording (platforms.context_log) ─────────────────


class _FakeAdapter:
    def outbound_thread_key(self, target: str, metadata: dict | None = None) -> str:
        return str((metadata or {}).get("thread_id") or target)


def _stub_runner(calls: list) -> MagicMock:
    runner = MagicMock()
    runner.record_context_note = MagicMock(
        side_effect=lambda base, note, skip_thread_id=None: (
            calls.append((base, note, skip_thread_id)) or base
        )
    )
    return runner


def _envelope(**overrides):
    from olano.platforms.delivery import MessageEnvelope

    fields = {
        "text": "digest body",
        "target": "owner",
        "channel_type": "telegram",
        "agent_name": "a1",
    }
    fields.update(overrides)
    return MessageEnvelope(**fields)


def test_outbound_delivery_recorded_on_target_thread(monkeypatch) -> None:
    from olano.platforms import context_log

    calls: list = []
    monkeypatch.setattr(
        context_log, "_active_runner", lambda name: _stub_runner(calls) if name == "a1" else None
    )

    context_log.record_outbound_delivery(_envelope(), _FakeAdapter(), "12345")

    (base, note, skip) = calls[0]
    assert base == "12345"
    assert "digest body" in note and "telegram" in note
    assert skip is None


def test_outbound_delivery_passes_current_thread_for_skip(monkeypatch) -> None:
    from olano.platforms import context_log
    from olano.security.request_context import push_request_context

    calls: list = []
    monkeypatch.setattr(context_log, "_active_runner", lambda name: _stub_runner(calls))

    with push_request_context(session_id="active-x"):
        context_log.record_outbound_delivery(_envelope(), _FakeAdapter(), "12345")

    assert calls[0][2] == "active-x"


def test_outbound_delivery_describes_media(monkeypatch) -> None:
    from olano.platforms import context_log

    calls: list = []
    monkeypatch.setattr(context_log, "_active_runner", lambda name: _stub_runner(calls))

    envelope = _envelope(text="cap", media_url="/tmp/report.pdf", media_type="document")
    context_log.record_outbound_delivery(envelope, _FakeAdapter(), "12345")

    note = calls[0][1]
    assert "document" in note and "/tmp/report.pdf" in note and "cap" in note


def test_outbound_delivery_skips_classic_adapters(monkeypatch) -> None:
    # Classic channels derive process-random inbound thread ids; a note keyed
    # by chat id would strand, so adapters without outbound_thread_key skip.
    from olano.platforms import context_log

    calls: list = []
    monkeypatch.setattr(context_log, "_active_runner", lambda name: _stub_runner(calls))

    context_log.record_outbound_delivery(_envelope(), object(), "12345")

    assert calls == []


def test_outbound_delivery_requires_agent_and_runtime(monkeypatch) -> None:
    from olano.platforms import context_log

    calls: list = []
    monkeypatch.setattr(context_log, "_active_runner", lambda name: None)
    context_log.record_outbound_delivery(_envelope(), _FakeAdapter(), "12345")

    monkeypatch.setattr(context_log, "_active_runner", lambda name: _stub_runner(calls))
    context_log.record_outbound_delivery(_envelope(agent_name=""), _FakeAdapter(), "12345")

    assert calls == []


# ── record-only events bypass the session machinery ─────────────────────


async def test_record_only_event_bypasses_session_machinery() -> None:
    # A context-only event goes straight to the runtime handler: no active
    # session is claimed (it must never interrupt or queue behind a running
    # turn), no background task is spawned, and no response is delivered.
    from olano.platforms.base import MessageEvent
    from olano.platforms.config import Platform, PlatformConfig
    from olano.platforms.session import SessionSource
    from olano.platforms.telegram import TelegramAdapter

    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="t"), agent_name="a1")
    handled: list = []

    async def handler(event):
        handled.append(event)
        return ""

    adapter.set_message_handler(handler)
    event = MessageEvent(
        text="[context] the owner replied themselves",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="biz:c1:200", user_id="100"),
        record_only=True,
    )

    await adapter.handle_message(event)

    assert handled == [event]
    assert adapter._active_sessions == {}
    assert adapter._background_tasks == set()


# ── ChannelRegistry.send hook ───────────────────────────────────────────


async def test_registry_send_records_after_success(monkeypatch) -> None:
    from olano.platforms import context_log
    from olano.platforms.delivery import ChannelRegistry

    recorded: list = []
    monkeypatch.setattr(
        context_log,
        "record_outbound_delivery",
        lambda envelope, adapter, target: recorded.append((envelope.text, target)),
    )

    registry = ChannelRegistry()
    adapter = MagicMock()
    adapter.send_text = AsyncMock(return_value="99")
    registry.register("a1", "telegram", adapter)

    result = await registry.send(_envelope(target="123"))

    assert result == "99"
    assert recorded == [("digest body", "123")]


async def test_registry_send_does_not_record_failures(monkeypatch) -> None:
    from olano.platforms import context_log
    from olano.platforms.delivery import ChannelRegistry

    recorded: list = []
    monkeypatch.setattr(
        context_log,
        "record_outbound_delivery",
        lambda envelope, adapter, target: recorded.append(target),
    )

    registry = ChannelRegistry()
    adapter = MagicMock()
    adapter.send_text = AsyncMock(side_effect=RuntimeError("boom"))
    registry.register("a1", "telegram", adapter)

    assert await registry.send(_envelope(target="123")) is None
    assert recorded == []


async def test_registry_send_survives_recording_failure(monkeypatch) -> None:
    # Recording is best-effort: a broken recorder must never fail a delivery
    # that already reached the user.
    from olano.platforms import context_log
    from olano.platforms.delivery import ChannelRegistry

    def _boom(envelope, adapter, target):
        raise RuntimeError("recorder down")

    monkeypatch.setattr(context_log, "record_outbound_delivery", _boom)

    registry = ChannelRegistry()
    adapter = MagicMock()
    adapter.send_text = AsyncMock(return_value="99")
    registry.register("a1", "telegram", adapter)

    assert await registry.send(_envelope(target="123")) == "99"
