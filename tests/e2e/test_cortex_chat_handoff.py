"""Cortex "Chat" / "Explore more" hand the conversation to the chat view.

Cortex used to answer the Chat button with a miniature transcript inline under
the card — a second chat UI with none of the real one's composer, history, model
picker or approvals — and Explore more with a background worker whose result
came back as a toast. Both now navigate to the chat view on the item's OWN
thread (``pulse:<item id>`` / ``cortex:<item id>``), which is the same id the
inline box streamed to, so an older discussion continues rather than starting
over.

The properties below were each silently easy to break:

* the click lands on ``/chat?thread=<id>`` — the URL is the durable half of the
  hand-off, so a refresh or a copied link reopens the same conversation;
* a NEW thread opens with the composer **pre-filled and never sent**: the
  framing ("The user wants to chat about this pulse …", the full workspace path
  to the artifacts) ends in ``User: `` and waits for the person's own words.
  The earlier design auto-sent an opening question nobody had typed;
* an EXISTING thread reopens on its history with the composer left alone;
* the simple shell lands it too. It stays mounted while the Cortex panel is on
  screen, redirects ``/chat`` to ``/`` and used to drop the query on the way —
  the button did nothing visible there, which is the bug that prompted this;
* nothing renders a chat inside Cortex any more.
"""

from __future__ import annotations

import json
import re
from urllib.parse import unquote, urlsplit

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import SIMPLE_COMPOSER, SIMPLE_ROOT

pytestmark = pytest.mark.e2e

HISTORY_TOGGLE = "[data-testid='chat-history-toggle']"
COMPOSER = ".composer textarea"

ITEM_ID = "pulse-e2e-1"
THREAD_ID = f"pulse:{ITEM_ID}"
WORKSPACE_ROOT = "/srv/olano/workspace"

PULSE = {
    "lane": "default",
    "count": 1,
    "items": [
        {
            "id": ITEM_ID,
            "source": "cognition",
            "lane": "default",
            "type": "finding",
            "priority": "normal",
            "title": "Connector readiness matrix",
            "content": "A v1 readiness one-pager for the three reusable templates.",
            "why_now": "A background worker you approved finished.",
            "suggested_next_step": "Review the artifact.",
            "references": ["memories/readiness.md"],
            "workspace_root": WORKSPACE_ROOT,
            "status": "pending",
            "created_at": "2026-09-04T10:00:00Z",
            "action": None,
            "metadata": {},
            "agent": "merli",
        }
    ],
}

IDEA_ID = "idea-e2e-1"
IDEA_THREAD_ID = f"cortex:{IDEA_ID}"
IDEA_PATH = "ideas/solar-kiln"
IDEAS = {
    "agent": "merli",
    "counts": {"idea": 1},
    "items": [
        {
            "id": IDEA_ID,
            "kind": "idea",
            "slug": "solar-kiln",
            "title": "Solar kiln",
            "status": "active",
            "summary": "Dry lumber with a passive solar kiln.",
            "preview": "Dry lumber with a passive solar kiln.",
            "tags": [],
            "path": IDEA_PATH,
            "abs_path": f"{WORKSPACE_ROOT}/{IDEA_PATH}",
            "detail_file": f"{IDEA_PATH}/idea.md",
            "source": "autonomous",
            "created_at": "2026-09-04T10:00:00Z",
            "updated_at": "2026-09-04T10:00:00Z",
            "links": {},
        }
    ],
}


def _route_pulse(page) -> None:
    page.route(
        "**/api/pulse?*",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(PULSE)
        ),
    )


def _open_pulse(page, base_url: str) -> None:
    _route_pulse(page)
    page.goto(base_url.rstrip("/") + "/cortex", wait_until="domcontentloaded", timeout=20_000)
    wait_for_app_shell(page)
    page.get_by_text("Connector readiness matrix", exact=True).first.wait_for(
        state="visible", timeout=15_000
    )


def _wait_for_chat_on(page, thread_id: str) -> None:
    # The thread id rides in the URL (percent-encoded — it carries a colon),
    # which is what makes a refresh or a copied link reopen this conversation.
    page.wait_for_url(
        lambda url: "/chat" in url and thread_id in unquote(urlsplit(url).query),
        timeout=15_000,
    )
    page.locator(COMPOSER).wait_for(state="visible", timeout=15_000)


def _composer_value(page, selector: str = COMPOSER) -> str:
    ta = page.locator(selector).first
    # The draft lands in the store and reaches the textarea on the next render.
    page.wait_for_function(
        "sel => (document.querySelector(sel)?.value ?? '').length > 0", arg=selector, timeout=10_000
    )
    return ta.input_value()


def test_chat_button_preloads_the_composer_and_sends_nothing(page, dashboard) -> None:
    _open_pulse(page, dashboard.base_url)

    # Cortex draws no chat of its own — the card offers a way OUT to one.
    assert page.locator(COMPOSER).count() == 0

    page.get_by_role("button", name="Chat", exact=True).first.click()
    _wait_for_chat_on(page, THREAD_ID)

    draft = _composer_value(page)
    # The framing names what the person is here to do, the item, and the FULL
    # workspace path to its artifacts — then stops and waits for their words.
    assert draft.startswith(
        'The user wants to chat about this pulse: "Connector readiness matrix".'
    )
    assert f"{WORKSPACE_ROOT}/memories/readiness.md" in draft
    assert draft.endswith("\n\nUser: ")

    # Nothing was sent on the person's behalf: no user turn, no reply pending.
    page.wait_for_timeout(1_500)
    assert page.locator(".bubble.user").count() == 0
    assert page.locator(".bubble.assistant").count() == 0

    # The caret is parked after "User: " so typing continues the draft.
    assert page.locator(COMPOSER).first.evaluate(
        "el => document.activeElement === el && el.selectionStart === el.value.length"
    )


def test_an_existing_discussion_reopens_on_its_history_with_an_empty_composer(
    page, dashboard
) -> None:
    """Returning to a Cortex conversation continues it — the history is the
    context, so the draft framing must not be pushed in front of it."""
    page.route(
        re.compile(r"/api/agents/[^/]+/threads/pulse(?:%3A|:)pulse-e2e-1$"),
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "messages": [
                        {"role": "user", "content": "Earlier question about the matrix"},
                        {"role": "assistant", "content": "Earlier answer about the matrix"},
                    ]
                }
            ),
        ),
    )
    _open_pulse(page, dashboard.base_url)
    page.get_by_role("button", name="Chat", exact=True).first.click()
    _wait_for_chat_on(page, THREAD_ID)

    page.locator(".bubble.user", has_text="Earlier question about the matrix").first.wait_for(
        state="visible", timeout=15_000
    )
    page.wait_for_timeout(500)
    assert page.locator(COMPOSER).first.input_value() == ""
    assert page.locator(".bubble.user").count() == 1


def test_explore_more_hands_the_item_to_chat_with_an_explore_draft(page, dashboard) -> None:
    """Explore more is a conversation the person steers, not a background
    worker: same thread as Chat, a different framing in the composer."""
    # Both shapes: the view primes every tab's count with a bare
    # /api/cortex/items (no agent yet) and the tab itself asks with a query.
    # A glob that catches only the second leaves the count fetch hitting the
    # real backend, which is how this test's own selector bug stayed hidden
    # locally — see the tab locator below.
    page.route(
        re.compile(r"/api/cortex/items(\?|$)"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(IDEAS)
        ),
    )
    page.goto(
        dashboard.base_url.rstrip("/") + "/cortex", wait_until="domcontentloaded", timeout=20_000
    )
    wait_for_app_shell(page)
    # Scoped to the strip and matched on substring: a tab button carries its
    # count badge inside the same element, so its accessible name is "Ideas 1"
    # the moment the counts land — an exact-name match is a race, and one this
    # suite lost on CI while passing locally. "Ideas" is unique among the tabs.
    page.locator(".cortex-tabstrip button", has_text="Ideas").first.click()
    page.get_by_text("Solar kiln", exact=True).first.wait_for(state="visible", timeout=15_000)

    page.get_by_role("button", name="Explore more", exact=True).first.click()
    _wait_for_chat_on(page, IDEA_THREAD_ID)

    draft = _composer_value(page)
    assert draft.startswith('The user wants to explore more about this idea: "Solar kiln".')
    assert f"{WORKSPACE_ROOT}/{IDEA_PATH}" in draft
    assert draft.endswith("\n\nUser: ")
    page.wait_for_timeout(1_000)
    assert page.locator(".bubble.user").count() == 0


def test_the_landing_url_keeps_its_thread_while_the_open_is_in_flight(page, dashboard) -> None:
    """The URL must not lose the parameter the landing is honouring.

    Opening a thread is async, so on the very commit that starts a landing
    there is still no session — and the effect that mirrors the open
    conversation back into the URL would read "no thread" and delete the
    `?thread=` it was about to restore. Whether that window is observable is
    pure timing: it stayed hidden on a fast machine and dropped the onboarding
    wizard's own thread on a slow one, landing the owner on a bare /chat after
    setup.

    Holding the transcript request open makes the window permanent, so this is
    a fact about the code rather than a race the machine happens to win.
    """
    pending: list[object] = []
    page.route(
        re.compile(r"/api/agents/[^/]+/threads/pulse(?:%3A|:)pulse-e2e-1$"),
        lambda route: pending.append(route),  # never fulfilled: the open hangs
    )
    _open_pulse(page, dashboard.base_url)
    page.get_by_role("button", name="Chat", exact=True).first.click()

    page.wait_for_url(
        lambda url: "/chat" in url and THREAD_ID in unquote(urlsplit(url).query),
        timeout=15_000,
    )
    # And it stays there while the open is still in flight, rather than being
    # stripped a render later.
    page.wait_for_timeout(2_000)
    assert THREAD_ID in unquote(urlsplit(page.url).query)


@pytest.fixture
def simple_ui_mode():
    from olano.config.home import ensure_olano_home, update_home_config

    ensure_olano_home()
    update_home_config({"ui_mode": "simple"})
    yield
    update_home_config({"ui_mode": "advanced"})


def test_the_simple_shell_lands_the_hand_off_too(page, dashboard, simple_ui_mode) -> None:
    """The shell that most people use. It embeds the same Cortex panel, stays
    mounted while it is on screen, and folds ``/chat`` back into ``/`` — three
    reasons the hand-off used to vanish here without a trace."""
    _route_pulse(page)
    page.goto(
        dashboard.base_url.rstrip("/") + "/cortex", wait_until="domcontentloaded", timeout=20_000
    )
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)
    page.get_by_text("Connector readiness matrix", exact=True).first.wait_for(
        state="visible", timeout=15_000
    )

    page.get_by_role("button", name="Chat", exact=True).first.click()

    # Folded back into the conversation route WITH its thread — the query is
    # the durable half of the hand-off and must survive the redirect.
    page.wait_for_url(
        lambda url: urlsplit(url).path == "/" and THREAD_ID in unquote(urlsplit(url).query),
        timeout=15_000,
    )
    composer = f"{SIMPLE_COMPOSER} textarea"
    page.locator(composer).first.wait_for(state="visible", timeout=15_000)
    draft = _composer_value(page, composer)
    assert draft.startswith(
        'The user wants to chat about this pulse: "Connector readiness matrix".'
    )
    assert draft.endswith("\n\nUser: ")
    page.wait_for_timeout(1_000)
    assert page.locator(".bubble.user").count() == 0


# A thread the runtime has classified as Cortex-born (core/thread_labels.py
# derives this from the `pulse:` / `cortex:` id prefix — see
# tests/test_thread_labels.py), as the rail receives it.
THREADS = [
    {
        "id": THREAD_ID,
        "name": "",
        "preview": "Let's discuss this pulse.",
        "title": "Connector readiness matrix",
        "origin": "cortex",
        "channel": "dashboard",
        "sender": "",
        "message_count": 2,
        "created_at": "2026-09-04T10:00:00Z",
        "updated_at": "2026-09-04T10:01:00Z",
    },
    {
        "id": "3f6a1b2c-0000-4000-8000-000000000000",
        "name": "",
        "preview": "hello there",
        "title": "hello there",
        "origin": "interactive",
        "channel": "dashboard",
        "sender": "",
        "message_count": 2,
        "created_at": "2026-09-04T09:00:00Z",
        "updated_at": "2026-09-04T09:01:00Z",
    },
]


def test_thread_history_labels_a_cortex_conversation_in_violet(page, dashboard) -> None:
    """The rail says which conversations came out of Cortex.

    A hand-off lands its thread among every other dashboard chat, where nothing
    in the title distinguishes it. The pill does — in the Cortex violet, so the
    two surfaces read as one feature — and only for those threads: an ordinary
    chat is the baseline the badge is read against, so it stays bare.
    """
    page.route(
        "**/api/agents/*/threads",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(THREADS)
        ),
    )
    page.goto(
        dashboard.base_url.rstrip("/") + "/chat", wait_until="domcontentloaded", timeout=20_000
    )
    wait_for_app_shell(page)
    # The rail is an off-canvas drawer at every viewport — nothing is in the DOM
    # until its toggle asks for it (see test_chat_history_drawer.py).
    page.wait_for_selector(HISTORY_TOGGLE, timeout=15_000)
    page.locator(HISTORY_TOGGLE).click()
    page.wait_for_selector("aside.chat-rail", timeout=10_000)

    # Scoped to the drawer: the sidebar's per-agent chat list wears the same
    # pill on the same thread, and it sits earlier in the DOM (behind the
    # drawer's backdrop on desktop, inside the closed nav drawer on a phone).
    pill = page.locator("aside.chat-rail .thread-origin-cortex").first
    pill.wait_for(state="visible", timeout=15_000)
    assert (pill.text_content() or "").strip().upper() == "CORTEX"

    # The violet is the Cortex token, not a colour typed into this component.
    color, token = pill.evaluate(
        "el => [getComputedStyle(el).color,"
        " getComputedStyle(document.documentElement).getPropertyValue('--cortex').trim()]"
    )
    assert color == page.evaluate(
        "hex => { const d = document.createElement('div'); d.style.color = hex;"
        " document.body.appendChild(d); const c = getComputedStyle(d).color;"
        " d.remove(); return c; }",
        token,
    )

    # An ordinary conversation carries no origin pill at all.
    assert page.locator("aside.chat-rail .thread-origin").count() == 1
