"""E2E: a tool approval in the simple shell is said in words.

The full dashboard's card shows the tool name and its raw arguments, which is
right for an operator. The simple shell's reader is often not technical, so
there the card says what the agent wants to do ("Send an email"), the one
detail the decision turns on ("To: ana@example.com"), and keeps the tool name
and JSON behind a "Technical details" disclosure.

There is no model behind the harness that pauses for approval, so the chat
stream is answered in the page (Playwright request interception on
``/api/chat/stream``) with the ``approval`` frame the runtime sends, and the rejoin that follows
is left pending, the state a paused turn is in.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.e2e

ACTIONS = [
    {
        "name": "gmail_send",
        "interrupt_id": "abcdef1234567890",
        "args": {"to": "ana@example.com", "subject": "Quarterly numbers", "body": "Hi Ana"},
        "risk": "high",
    }
]


def _set_ui_mode(value: str) -> None:
    from olano.config.home import ensure_olano_home, update_home_config

    ensure_olano_home()
    update_home_config({"ui_mode": value})


def _sse(frames: list[dict]) -> str:
    return "".join(f"data: {json.dumps(f)}\n\n" for f in frames)


def test_the_simple_shell_says_what_the_agent_wants_to_do(page, dashboard) -> None:
    def stream(route) -> None:
        route.fulfill(
            status=200,
            content_type="text/event-stream",
            body=_sse(
                [
                    {
                        "type": "start",
                        "thread_id": "merli::e2e-approval",
                        "run_id": "run-e2e-approval",
                        "seq": 1,
                    },
                    {"type": "approval", "actions": ACTIONS, "seq": 2},
                ]
            ),
        )

    page.route("**/api/chat/stream", stream)
    # The fulfilled stream ends after the approval frame; the client then
    # rejoins the run. Left unanswered, that rejoin keeps the run "live", which
    # is the state a real paused turn is in while it waits for the decision.
    page.route("**/api/chat/attach**", lambda route: None)
    _set_ui_mode("simple")
    try:
        page.goto(dashboard.base_url.rstrip("/") + "/", wait_until="domcontentloaded")
        box = page.locator("[data-simple-composer] textarea")
        box.wait_for(state="visible", timeout=20_000)
        box.fill("Email Ana the quarterly numbers")
        # A phone's Enter is a newline; the send button works at both sizes.
        page.locator("[data-simple-composer]").get_by_role(
            "button", name="Send", exact=True
        ).click()

        card = page.get_by_test_id("approval-plain")
        card.wait_for(state="visible", timeout=15_000)
        assert "Send an email" in card.inner_text()
        assert "ana@example.com" in card.inner_text()
        # The raw call is there, but folded away.
        details = card.locator("details.approval-plain-details")
        assert details.get_attribute("open") is None
        assert not card.locator(".approval-row-args").is_visible()
        details.locator("summary").click()
        assert "gmail_send" in card.locator(".approval-row-name").inner_text()
        assert '"to": "ana@example.com"' in card.locator(".approval-row-args").inner_text()
    finally:
        _set_ui_mode("advanced")
