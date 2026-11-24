"""E2E: a thread moves on screen when the turn happens somewhere else.

The bug this guards: a message sent to an agent over Telegram, from a
scheduled job, or through the OpenAI-compatible connector was invisible to a
dashboard chat already open on that thread — on the laptop, on the phone —
until a page reload. The runner now announces every transcript row and every
turn boundary on ``/api/stream``; this proves the half the in-process tests
cannot see: the server's SSE queue, the browser's EventSource, ``useThreadSync``
routing a frame into the right thread, and the row folding into the same
bubbles a reload draws.

The frames are pushed from this process because the runtime runs in it — the
same ``_push_event`` the runner's publisher is bound to. The runner's own
publishing is pinned by ``tests/test_thread_events.py``.
"""

from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.e2e

HISTORY_TOGGLE = "[data-testid='chat-history-toggle']"


def _seed_thread(agent: str, rows: list[dict]) -> str:
    """Write a transcript into the home the runtime is *currently* reading
    (see ``test_connect_result_card._seed_thread`` for why it is resolved
    at call time)."""
    from olano.config.home import get_olano_paths

    thread_id = str(uuid.uuid4())
    sessions = get_olano_paths().sessions_dir / agent
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{thread_id}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    return thread_id


def _open_thread(page, base_url: str, thread_id: str) -> None:
    page.goto(f"{base_url.rstrip('/')}/chat?thread={thread_id}", wait_until="domcontentloaded")
    # The thread must be loaded before a frame lands: a row for a thread this
    # tab has not opened is dropped (the persisted row covers that case).
    page.locator(".bubble.user").first.wait_for(state="visible", timeout=20_000)


def _row(role: str, content: str, **extra) -> dict:
    return {"ts": "2026-09-02T10:00:00+00:00", "role": role, "content": content, **extra}


def test_a_turn_from_telegram_plays_out_live_without_a_reload(page, dashboard):
    """The user's scenario: watching the thread on a laptop while the agent
    answers a Telegram message. Working indicator naming the surface, the
    reply forming, the tool call, the final answer, then the indicator gone."""
    from olano.runtime.api import _push_event

    thread_id = _seed_thread("merli", [_row("user", "hola desde el movil", channel="telegram")])
    _open_thread(page, dashboard.base_url, thread_id)
    log = page.locator(".chat-log")
    assert "Working on this from" not in log.inner_text()

    # Turn start, from Telegram.
    _push_event(
        "thread_activity",
        {"agent": "merli", "thread_id": thread_id, "status": "thinking", "channel": "telegram"},
    )
    page.get_by_text("Working on this from Telegram").wait_for(state="visible", timeout=40_000)

    # The reply forming: a throttled preview of the text so far.
    _push_event(
        "thread_activity",
        {
            "agent": "merli",
            "thread_id": thread_id,
            "status": "streaming",
            "channel": "telegram",
            "text": "Preview token LIVEPREV",
        },
    )
    page.get_by_text("Preview token LIVEPREV").wait_for(state="visible", timeout=20_000)

    # The persisted rows, exactly as the runner writes them: a tool call, then
    # the final answer. Both go through the server-side projection.
    _push_event(
        "thread_updated",
        {
            "agent": "merli",
            "thread_id": thread_id,
            "entry": _row("tool_call", "ls: {}", name="ls", tool_call_id="c1"),
        },
    )
    _push_event(
        "thread_updated",
        {
            "agent": "merli",
            "thread_id": thread_id,
            "entry": _row("assistant", "Final answer token LIVEFINAL", channel="telegram"),
        },
    )
    bubble = page.locator(".bubble.assistant", has_text="Final answer token LIVEFINAL")
    bubble.wait_for(state="visible", timeout=20_000)

    # Turn end: the indicator and the preview are gone; the answer stays.
    _push_event("thread_activity", {"agent": "merli", "thread_id": thread_id, "status": "idle"})
    page.get_by_text("Working on this from Telegram").wait_for(state="hidden", timeout=20_000)
    assert "Preview token LIVEPREV" not in log.inner_text()
    assert bubble.count() == 1


def test_a_conversation_started_elsewhere_appears_in_the_list(page, dashboard):
    """A scheduled run or a first Telegram message creates a thread the
    dashboard never saw. It has to show up in the conversation list without a
    reload — the frame, not a poll, is what tells the rail to refetch."""
    from olano.runtime.api import _push_event

    # Land on an existing thread so the chat (and its rail subscription) is up.
    anchor = _seed_thread("merli", [_row("user", "an older chat")])
    _open_thread(page, dashboard.base_url, anchor)

    # A brand-new thread is created by another surface: persisted first, then
    # announced — the order the runner uses.
    fresh_text = "Fresh from Telegram token LIVENEW"
    fresh = _seed_thread("merli", [_row("user", fresh_text, channel="telegram")])
    _push_event(
        "thread_updated",
        {
            "agent": "merli",
            "thread_id": fresh,
            "entry": _row("user", fresh_text, channel="telegram"),
        },
    )

    page.locator(HISTORY_TOGGLE).first.click()
    row = page.locator(".thread-item", has_text="Fresh from Telegram token")
    row.wait_for(state="visible", timeout=40_000)


def test_a_hidden_row_never_reaches_the_open_chat(page, dashboard):
    """The live frame and the reload agree on what is NOT shown: a model-facing
    `system` note is dropped server-side before it can be broadcast."""
    from olano.runtime.api import _push_event

    thread_id = _seed_thread("merli", [_row("user", "hello")])
    _open_thread(page, dashboard.base_url, thread_id)

    _push_event(
        "thread_updated",
        {
            "agent": "merli",
            "thread_id": thread_id,
            "entry": _row("system", "PROMPT UPDATE token LIVESYS: your instructions changed."),
        },
    )
    # Something else on the same thread, to prove the stream is flowing.
    _push_event(
        "thread_updated",
        {
            "agent": "merli",
            "thread_id": thread_id,
            "entry": _row("assistant", "visible token LIVEOK"),
        },
    )
    page.locator(".bubble.assistant", has_text="visible token LIVEOK").wait_for(
        state="visible", timeout=40_000
    )
    assert "LIVESYS" not in page.locator(".chat-log").inner_text()
