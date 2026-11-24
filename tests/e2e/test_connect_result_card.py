"""E2E: a magic link that finished out-of-band is on screen when you come back.

The bug this guards: a link minted from the dashboard chat completed on the
user's phone and the chat showed **nothing** — no confirmation, no error — so
the only way to find out was to ask the agent. The backend now writes the
outcome into the thread's transcript, and this proves the round trip the
in-process tests cannot see: the persisted row survives the thread endpoint's
`system`-row filter, reaches the browser, and renders as a card rather than as
raw text or nothing at all.

Both halves are here, because they fail differently. The **persisted** path is
where a missing projection key or a wrong render branch shows a blank row — the
thread endpoint hides `system` rows as a class, and this proves the one
exception survives it. The **live** path is the user's actual scenario (they
are sitting in the chat when the link lands) and covers the plumbing nothing
else does: the server's SSE queue, the browser's EventSource, and the hook that
routes a frame into the right thread.
"""

from __future__ import annotations

import json
import uuid

import pytest

pytestmark = pytest.mark.e2e

RESULT_CARD = ".connect-result"
RESTART_BUTTON = ".connect-result__btn"


def _seed_thread(agent: str, rows: list[dict]) -> str:
    """Write a transcript into the home the runtime is *currently* reading.

    Resolved at call time, not from the dashboard fixture: the root conftest
    repoints ``LIONCLAW_HOME`` at a fresh tmp_path per test and the runtime
    shares this process, so it follows that move (see
    ``conftest._seed_agents_into_the_active_home``). Writing to the home the
    session-scoped dashboard *booted* with would land in a directory nothing
    reads.
    """
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


def _outcome_row(**over) -> dict:
    row = {
        "ts": "2026-08-31T00:00:02Z",
        "role": "system",
        "event": "magic_link",
        "content": "✅ curtis is connected — weixin: 35025ac810c0@im.bot.",
        "link_id": "hs_e2e0000000000001",
        "status": "done",
        "flow": "channel-connect",
        "label": "Connect channels",
        "target_agent": "curtis",
        "apply": "restarted",
    }
    row.update(over)
    return row


def test_a_completed_link_renders_its_outcome_card(page, dashboard):
    """A flow that applied itself states that and offers no button — asking to
    authorize a restart the backend already ran is noise, not safety."""
    thread_id = _seed_thread(
        "merli",
        [
            {"ts": "2026-08-31T00:00:00Z", "role": "user", "content": "connect wechat for curtis"},
            _outcome_row(),
        ],
    )
    _open_thread(page, dashboard.base_url, thread_id)

    card = page.locator(RESULT_CARD)
    card.wait_for(state="visible", timeout=20_000)
    assert "Connected" in card.inner_text()
    assert "curtis is connected" in card.inner_text()
    # It already restarted, so there is nothing to press.
    assert page.locator(RESTART_BUTTON).count() == 0


def test_a_link_awaiting_a_restart_offers_the_button(page, dashboard):
    """The case the user hit: connected, but the agent is still running its old
    config. The card has to turn that into an action, not a sentence."""
    thread_id = _seed_thread(
        "merli",
        [
            {"ts": "2026-08-31T00:00:00Z", "role": "user", "content": "add the stripe key"},
            _outcome_row(
                content="✅ STRIPE_API_KEY saved for curtis.",
                flow="secret-entry",
                label="API key",
                apply="restart_needed",
            ),
        ],
    )
    _open_thread(page, dashboard.base_url, thread_id)

    card = page.locator(RESULT_CARD)
    card.wait_for(state="visible", timeout=20_000)
    button = page.locator(RESTART_BUTTON)
    button.wait_for(state="visible", timeout=10_000)
    assert "curtis" in button.inner_text()


def test_a_failed_link_reports_and_offers_nothing(page, dashboard):
    """A sign-in that never completed applied nothing — restarting for it would
    be a lie about what just happened."""
    thread_id = _seed_thread(
        "merli",
        [
            {"ts": "2026-08-31T00:00:00Z", "role": "user", "content": "connect google"},
            _outcome_row(
                content="⚠️ The Google magic link expired before it was used.",
                flow="google",
                label="Google",
                status="expired",
                apply="none",
            ),
        ],
    )
    _open_thread(page, dashboard.base_url, thread_id)

    card = page.locator(RESULT_CARD)
    card.wait_for(state="visible", timeout=20_000)
    assert "expired before it was used" in card.inner_text()
    assert page.locator(RESTART_BUTTON).count() == 0


def test_an_ordinary_system_row_never_reaches_the_chat(page, dashboard):
    """The projection allowance is keyed on the marker, not on the role.

    Prompt-update notices and context notes are written for the model's
    cold-start rehydration; widening the filter to `system` as a class would put
    every one of them on screen.
    """
    thread_id = _seed_thread(
        "merli",
        [
            {"ts": "2026-08-31T00:00:00Z", "role": "user", "content": "hello"},
            {
                "ts": "2026-08-31T00:00:01Z",
                "role": "system",
                "content": "PROMPT UPDATE: your instructions changed.",
            },
        ],
    )
    _open_thread(page, dashboard.base_url, thread_id)

    page.locator(".bubble.user").first.wait_for(state="visible", timeout=20_000)
    assert "PROMPT UPDATE" not in page.locator(".chat-log").inner_text()
    assert page.locator(RESULT_CARD).count() == 0


def test_a_completed_link_appears_live_without_a_reload(page, dashboard):
    """The user's actual scenario: they are sitting in the chat when the link
    completes on their phone.

    The frame is pushed from this process because the runtime runs in it — the
    same call the watcher makes. Everything after that is the real path: the
    server's `/api/stream` queue, the browser's EventSource, `useConnectNotices`
    (mounted in the app shell, above both UI modes) and `pushConnectResult`.

    A generous timeout on purpose: the subscriber loop waits on its queue with
    a 15 s keepalive tick, so a push that does not wake the waiter immediately
    is still delivered on the next tick. Bounded, not flaky.
    """
    from olano.runtime.api import _push_event

    thread_id = _seed_thread(
        "merli",
        [{"ts": "2026-08-31T00:00:00Z", "role": "user", "content": "add the stripe key"}],
    )
    _open_thread(page, dashboard.base_url, thread_id)
    # The thread must be loaded before the frame lands: a notice for a thread
    # this tab has not opened is dropped (the persisted row covers that case).
    page.locator(".bubble.user").first.wait_for(state="visible", timeout=20_000)
    assert page.locator(RESULT_CARD).count() == 0

    _push_event(
        "magic_link_updated",
        {
            "agent": "merli",
            "thread_id": thread_id,
            "text": "✅ STRIPE_API_KEY saved for curtis.",
            "link_id": "hs_live0000000001",
            "status": "done",
            "flow": "secret-entry",
            "label": "API key",
            "target_agent": "curtis",
            "apply": "restart_needed",
        },
    )

    card = page.locator(RESULT_CARD)
    card.wait_for(state="visible", timeout=40_000)
    assert "STRIPE_API_KEY saved for curtis" in card.inner_text()
    page.locator(RESTART_BUTTON).wait_for(state="visible", timeout=10_000)


def test_a_repeated_frame_does_not_duplicate_the_card(page, dashboard):
    """Delivery is once-per-link server-side, but a frame can still meet a
    persisted row (or arrive twice across a reconnect) — `linkId` is the key
    that keeps one event from becoming two cards."""
    from olano.runtime.api import _push_event

    thread_id = _seed_thread(
        "merli",
        [{"ts": "2026-08-31T00:00:00Z", "role": "user", "content": "connect google"}],
    )
    _open_thread(page, dashboard.base_url, thread_id)
    page.locator(".bubble.user").first.wait_for(state="visible", timeout=20_000)

    frame = {
        "agent": "merli",
        "thread_id": thread_id,
        "text": "✅ Google is connected for curtis.",
        "link_id": "hs_dupe000000001",
        "status": "done",
        "flow": "google",
        "label": "Google",
        "target_agent": "curtis",
        "apply": "restart_needed",
    }
    _push_event("magic_link_updated", frame)
    page.locator(RESULT_CARD).wait_for(state="visible", timeout=40_000)
    _push_event("magic_link_updated", frame)
    page.wait_for_timeout(1_500)

    assert page.locator(RESULT_CARD).count() == 1
