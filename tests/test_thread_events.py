"""Live thread events: every surface's turn reaches an open dashboard chat.

A turn from Telegram, the scheduler, the OpenAI connector or a second browser
used to append to the JSONL transcript and return — a chat open on another
device learned nothing until a reload. The runner now announces every
transcript row (``thread_updated``) and every turn boundary
(``thread_activity``) through the publisher the runtime binds, and
``runtime/api._push_event`` projects the row into the exact shape
``GET /threads/{id}`` serves. These tests pin both halves, plus the
per-thread SSE visibility rule that keeps a member from watching another
human's conversation live.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from olano.config.agent_config import AgentConfig
from olano.core import thread_events
from olano.core.runner import AgentRunner
from olano.security.request_context import push_request_context
from olano.tools import talk_to as tt


@pytest.fixture
def runner(base_config: AgentConfig, mock_agent) -> AgentRunner:
    return AgentRunner(mock_agent, base_config)


@pytest.fixture
def frames(monkeypatch) -> list[tuple[str, dict[str, Any]]]:
    """Capture everything the runner publishes, exactly as the server would."""
    seen: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(tt, "_PUBLISHER", lambda kind, payload: seen.append((kind, payload)))
    return seen


def _of(frames: list[tuple[str, dict]], kind: str) -> list[dict]:
    return [p for k, p in frames if k == kind]


# ── core/thread_events ────────────────────────────────────────────────────────


def test_publish_is_a_noop_without_a_bound_publisher(monkeypatch) -> None:
    """CLI runs, tests and cognition workers have no dashboard to tell."""
    monkeypatch.setattr(tt, "_PUBLISHER", None)
    thread_events.publish_thread_updated("a", "t", {"role": "user", "content": "hi"})
    thread_events.publish_thread_activity("a", "t", "thinking")


def test_publish_never_raises_into_the_turn(monkeypatch) -> None:
    def boom(kind, payload):
        raise RuntimeError("subscriber exploded")

    monkeypatch.setattr(tt, "_PUBLISHER", boom)
    thread_events.publish_thread_updated("a", "t", {"role": "user"})
    thread_events.publish_thread_activity("a", "t", "idle")


def test_stream_preview_throttles_and_flushes(frames, monkeypatch) -> None:
    """One preview per interval, accumulating text; flush() sends the tail."""
    clock = {"now": 100.0}
    monkeypatch.setattr(thread_events.time, "monotonic", lambda: clock["now"])
    preview = thread_events.StreamPreview("merli", "t1", channel="telegram")
    preview.feed("Hel")  # first chunk goes out immediately
    preview.feed("lo")  # inside the interval → held
    clock["now"] += thread_events.STREAM_PREVIEW_INTERVAL_S
    preview.feed(" wor")  # interval elapsed → sent with everything so far
    preview.feed("ld")  # held
    preview.flush()  # tail

    texts = [p["text"] for p in _of(frames, thread_events.THREAD_ACTIVITY)]
    assert texts == ["Hel", "Hello wor", "Hello world"]
    assert all(p["status"] == "streaming" for p in _of(frames, thread_events.THREAD_ACTIVITY))
    assert all(p["channel"] == "telegram" for p in _of(frames, thread_events.THREAD_ACTIVITY))
    preview.flush()  # nothing pending → nothing sent
    assert len(_of(frames, thread_events.THREAD_ACTIVITY)) == 3


# ── runner ────────────────────────────────────────────────────────────────────


def test_every_transcript_append_is_announced(runner: AgentRunner, frames) -> None:
    """Both append paths publish the row as written, under the agent + thread."""
    thread_id = "t-" + uuid.uuid4().hex[:8]
    runner._append_session(thread_id, "user", "hello")
    runner.append_session_entry(thread_id, {"role": "tool_call", "name": "ls", "content": "ls: /"})

    updates = _of(frames, thread_events.THREAD_UPDATED)
    assert [u["entry"]["role"] for u in updates] == ["user", "tool_call"]
    assert all(u["agent"] == runner.config.name and u["thread_id"] == thread_id for u in updates)
    assert updates[0]["entry"]["content"] == "hello"
    assert updates[1]["entry"]["ts"]  # append_session_entry stamps the ts it persisted


async def test_streamed_turn_announces_start_preview_rows_and_end(
    runner: AgentRunner, frames
) -> None:
    """A streamed turn — whatever surface drives it — reports thinking, the
    reply forming, each persisted row, then idle, all naming the thread."""
    thread_id = "t-" + uuid.uuid4().hex[:8]
    with push_request_context(source="telegram", channel="telegram"):
        events = [ev async for ev in runner.astream_events("Hello", thread_id=thread_id)]
    assert any(k == "text" for k, _ in events)

    kinds = [k for k, _ in frames]
    activity = _of(frames, thread_events.THREAD_ACTIVITY)
    assert activity[0]["status"] == "thinking"
    assert activity[-1]["status"] == "idle"
    assert any(
        a["status"] == "streaming" and "Hello from mock agent" in a["text"] for a in activity
    )
    assert all(a["thread_id"] == thread_id and a["agent"] == runner.config.name for a in activity)
    # The surface rides along so the chat can say where the turn is happening.
    assert activity[0]["channel"] == "telegram"
    # The user row and the assistant row were both announced, in order, and
    # before the turn was declared over.
    roles = [u["entry"]["role"] for u in _of(frames, thread_events.THREAD_UPDATED)]
    assert roles == ["user", "assistant"]
    assert kinds.index(thread_events.THREAD_ACTIVITY) == 0
    assert kinds[-1] == thread_events.THREAD_ACTIVITY


async def test_streamed_turn_still_reports_idle_when_the_model_fails(
    base_config: AgentConfig, frames
) -> None:
    """A chat showing 'working from Telegram…' must not stay stuck on a crash."""
    from unittest.mock import MagicMock

    agent = MagicMock()

    async def _astream(*args, **kwargs):
        raise RuntimeError("provider down")
        yield  # pragma: no cover - makes this an async generator

    agent.astream = _astream
    runner = AgentRunner(agent, base_config)
    with pytest.raises(Exception):
        async for _ in runner.astream_events("Hello", thread_id="t-fail"):
            pass
    statuses = [a["status"] for a in _of(frames, thread_events.THREAD_ACTIVITY)]
    assert statuses[0] == "thinking"
    assert statuses[-1] == "idle"


async def test_ainvoke_turn_brackets_the_rows_with_activity(runner: AgentRunner, frames) -> None:
    """The non-streaming entry point (scheduler, peers, webhooks) is a wrapper over
    the stream path, so it gets the same frames: the brackets AND the throttled
    reply preview a chat open on the thread draws while the turn runs."""
    thread_id = "t-" + uuid.uuid4().hex[:8]
    with push_request_context(source="scheduled", channel=""):
        await runner.ainvoke("run the sweep", thread_id=thread_id)
    activity = _of(frames, thread_events.THREAD_ACTIVITY)
    statuses = [a["status"] for a in activity]
    assert statuses[0] == "thinking"
    assert statuses[-1] == "idle"
    assert "streaming" in statuses
    assert activity[0]["channel"] == "scheduler"
    roles = [u["entry"]["role"] for u in _of(frames, thread_events.THREAD_UPDATED)]
    assert roles == ["user", "assistant"]


# ── runtime/api._push_event projection ────────────────────────────────────────


@pytest.fixture
def captured():
    from olano.runtime import api as api_mod

    seen: list[dict] = []
    q = type("Q", (), {"put_nowait": lambda self, e: seen.append(e)})()
    api_mod._subscribers.append(q)
    try:
        yield seen
    finally:
        api_mod._subscribers.remove(q)


def _push(kind: str, data: Any) -> None:
    from olano.runtime import api as api_mod

    api_mod._push_event(kind, data)


def test_thread_updated_is_projected_like_a_thread_reload(captured) -> None:
    """The wire frame carries the message the transcript endpoint would serve:
    sender tag peeled, provenance fields present, raw entry gone."""
    _push(
        thread_events.THREAD_UPDATED,
        {
            "agent": "merli",
            "thread_id": "12345",
            "entry": {
                "schema_version": 1,
                "ts": "2026-09-02T10:00:00+00:00",
                "role": "user",
                "content": "[from @carlos (Jamie, id=1)]: hola",
                "channel": "telegram",
            },
        },
    )
    assert len(captured) == 1
    frame = captured[0]
    assert frame["type"] == thread_events.THREAD_UPDATED
    data = frame["data"]
    assert data["agent"] == "merli" and data["thread_id"] == "12345" and data["role"] == "user"
    assert "entry" not in data
    msg = data["message"]
    assert msg["content"] == "hola"
    assert msg["sender"] == "Jamie"
    assert msg["channel"] == "telegram"
    assert msg["ts"] == "2026-09-02T10:00:00+00:00"
    # Same projection the reload path runs, field for field.
    from olano.runtime import api as api_mod

    assert msg == api_mod._transcript_message(
        "merli",
        {
            "ts": "2026-09-02T10:00:00+00:00",
            "role": "user",
            "content": "[from @carlos (Jamie, id=1)]: hola",
            "channel": "telegram",
        },
    )


def test_thread_updated_hides_what_the_reload_hides(captured) -> None:
    """A prompt-update `system` note is for the model, not the human: the
    reload drops it, so the live frame is never sent either."""
    _push(
        thread_events.THREAD_UPDATED,
        {"agent": "merli", "thread_id": "t", "entry": {"role": "system", "content": "notice"}},
    )
    _push(thread_events.THREAD_UPDATED, {"agent": "merli", "thread_id": "t", "entry": "garbage"})
    _push(thread_events.THREAD_UPDATED, {"agent": "", "thread_id": "t", "entry": {"role": "user"}})
    assert captured == []


def test_thread_updated_keeps_the_magic_link_row(captured) -> None:
    """The one `system` row written FOR the chat rides through with its card fields."""
    _push(
        thread_events.THREAD_UPDATED,
        {
            "agent": "merli",
            "thread_id": "t",
            "entry": {
                "role": "system",
                "event": "magic_link",
                "content": "✅ connected",
                "link_id": "L1",
                "status": "done",
                "flow": "channel-connect",
                "label": "Connect channels",
                "target_agent": "curtis",
                "apply": "restarted",
            },
        },
    )
    msg = captured[0]["data"]["message"]
    assert msg["event"] == "magic_link" and msg["link_id"] == "L1" and msg["apply"] == "restarted"


def test_middleware_tool_rows_are_announced_after_being_persisted(captured, lionclaw_home) -> None:
    """`thread_message` tool rows bypass the runner's append, so the API's own
    write-back announces them — an open chat draws every row a reload will."""
    _push(
        "thread_message",
        {
            "agent": "merli",
            "thread_id": "t9",
            "role": "tool_call",
            "message_id": "task::1",
            "tool_call_id": "1",
            "name": "task",
            "args": "{}",
        },
    )
    kinds = [f["type"] for f in captured]
    assert kinds == [thread_events.THREAD_UPDATED, "thread_message"]
    assert captured[0]["data"]["message"]["name"] == "task"
    assert captured[0]["data"]["message"]["tool_call_id"] == "1"


# ── per-thread SSE visibility ─────────────────────────────────────────────────


def test_event_visible_to_thread_rules() -> None:
    from olano.runtime import auth as auth_layer

    vis = auth_layer.event_visible_to_thread
    # Any authenticated human, and single-user: every thread frame of an agent
    # they can reach (the agent filter runs beside this one) — another human's
    # retired ``u:<uid>:`` thread, a bare platform chat id, a scheduled run.
    every_shape = (
        "u:bob:t",
        "u:alice:t",
        "merli::u:alice:t",
        "12345",
        "scheduled_merli_1a2b3c4d@20260101T000000-ab12",
    )
    for tid in every_shape:
        assert vis({"type": "thread_updated", "data": {"thread_id": tid}}, None) is True, tid
    assert vis({"type": "magic_link_updated", "data": {"thread_id": "u:bob:t"}}, None) is True
    # Frames without a thread are not this filter's business, whoever asks.
    assert vis({"type": "agents_update", "data": {"name": "merli"}}, None) is True
    assert vis({"type": "log", "data": "line"}, None) is True
    assert vis({"type": "agents_update", "data": {"name": "merli"}}, "") is True
    # Anonymous: no thread at all.
    for tid in every_shape:
        assert vis({"type": "thread_updated", "data": {"thread_id": tid}}, "") is False, tid
    # (The connect-time snapshot this comes from is pinned against the
    # endpoint rule in tests/runtime/test_member_scoping.py.)


# ── hidden prompt rows (the onboarding greeting) ──────────────────────────────


def test_hidden_prompt_stamps_only_the_user_row(runner: AgentRunner, frames) -> None:
    """A prompt the platform wrote for the model (request_context.HIDDEN_PROMPT_KEY)
    is persisted with ``hidden`` — on the prompt row alone, never the reply."""
    from olano.security.request_context import HIDDEN_PROMPT_KEY

    thread_id = "welcome-" + uuid.uuid4().hex[:8]
    with push_request_context(source="api", channel="api", metadata={HIDDEN_PROMPT_KEY: True}):
        runner._append_session(thread_id, "user", "a new owner just signed up, say hi")
        runner._append_session(thread_id, "assistant", "Hi Carlos!")
    rows = [u["entry"] for u in _of(frames, thread_events.THREAD_UPDATED)]
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[0]["hidden"] is True
    assert "hidden" not in rows[1]
    # Still schema 1: an optional key every reader `.get`s.
    assert rows[0]["schema_version"] == 1

    # A plain turn stamps nothing.
    with push_request_context(source="api", channel="api"):
        runner._append_session(thread_id, "user", "hello")
    assert "hidden" not in _of(frames, thread_events.THREAD_UPDATED)[-1]["entry"]


def test_hidden_prompt_never_reaches_the_chat(captured) -> None:
    """The live frame and the reload share one projection: a hidden row is
    dropped by both, the reply it produced rides through."""
    from olano.runtime import api as api_mod

    hidden = {"role": "user", "content": "a new owner just signed up", "hidden": True}
    assert api_mod._transcript_message("agentfather", hidden) is None
    _push(thread_events.THREAD_UPDATED, {"agent": "agentfather", "thread_id": "w", "entry": hidden})
    assert captured == []
    _push(
        thread_events.THREAD_UPDATED,
        {
            "agent": "agentfather",
            "thread_id": "w",
            "entry": {"role": "assistant", "content": "Hi!"},
        },
    )
    assert len(captured) == 1 and captured[0]["data"]["message"]["content"] == "Hi!"


def test_thread_list_and_detail_skip_hidden_rows(lionclaw_home) -> None:
    """The rail's preview/title and the transcript never show the instruction —
    a greeting thread reads as Agent Father's hello, nothing before it."""
    import json
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    sessions = lionclaw_home / "sessions" / "agentfather"
    sessions.mkdir(parents=True)
    rows = [
        {
            "schema_version": 1,
            "ts": "2026-09-05T10:00:00+00:00",
            "role": "user",
            "content": "A new owner just signed up: Carlos. Say hi.",
            "channel": "dashboard",
            "hidden": True,
        },
        {
            "schema_version": 1,
            "ts": "2026-09-05T10:00:05+00:00",
            "role": "assistant",
            "content": "Hi Carlos, I'm Agent Father.",
            "channel": "dashboard",
        },
    ]
    (sessions / "welcome-u1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (sessions / "welcome-u1.meta.json").write_text(json.dumps({"name": "Welcome"}))

    runtime = MagicMock()
    runtime._runners = {}
    client = TestClient(create_api(runtime))  # single-user → implicit admin

    listed = client.get("/api/agents/agentfather/threads").json()
    row = next(t for t in listed if t["id"] == "welcome-u1")
    assert row["name"] == "Welcome"
    assert "signed up" not in row["preview"] and "signed up" not in row["title"]
    assert row["message_count"] == 1

    detail = client.get("/api/agents/agentfather/threads/welcome-u1").json()
    assert [m["role"] for m in detail["messages"]] == ["assistant"]
    assert detail["messages"][0]["content"] == "Hi Carlos, I'm Agent Father."


def test_assistant_rows_keep_their_segments_in_the_live_frame(captured) -> None:
    """The interleaving structure the runner persists reaches a chat watching
    from another device exactly as a reload would serve it."""
    entry = {
        "schema_version": 1,
        "ts": "2026-09-02T10:00:05+00:00",
        "role": "assistant",
        "content": "Looking.\n\nDone.",
        "segments": [{"text": "Looking.", "after": None}, {"text": "Done.", "after": "c1"}],
    }
    _push(thread_events.THREAD_UPDATED, {"agent": "merli", "thread_id": "12345", "entry": entry})
    msg = captured[0]["data"]["message"]
    assert msg["segments"] == entry["segments"]
    from olano.runtime import api as api_mod

    assert msg == api_mod._transcript_message("merli", dict(entry))


def test_peer_inbound_row_names_the_calling_agent_as_sender():
    """talk_to writes `from` and no `sender`; the chat labelled the peer's
    question "You" in the callee's thread until the projection mapped it."""
    from olano.runtime import api as api_mod

    msg = api_mod._transcript_message(
        "callee",
        {"role": "peer_inbound", "from": "caller", "content": "Can you check the logs?"},
    )
    assert msg is not None
    assert msg["sender"] == "caller"
    assert msg["from"] == "caller"
    # An explicit sender still wins, and a user row never borrows `from`.
    explicit = api_mod._transcript_message(
        "callee", {"role": "peer_inbound", "from": "caller", "sender": "Caller", "content": "x"}
    )
    assert explicit is not None and explicit["sender"] == "Caller"
    user = api_mod._transcript_message("callee", {"role": "user", "from": "x", "content": "hi"})
    assert user is not None and user["sender"] == ""
