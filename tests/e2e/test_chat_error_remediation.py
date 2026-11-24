"""E2E: a stuck-thread error must offer the one fix that works.

`message_format_unsupported` is the classification for a thread whose
checkpoint holds a content block the current model has no format for — the
failure that reached a user as an opaque `API error (ValueError)`. It is the
first classified error whose remedy is neither a credential nor a retry: the
block is persisted, so only a *new conversation* clears it.

That makes it the case the chat's fix-it CTA was closest to getting wrong.
`views/chat/ErrorRemediation.tsx` renders **nothing at all** for a remediation
without an `action`, so a backend rule carrying only a `summary` produces a
message that says "start a new thread" above no way to start one. Nothing in
the Python suite can see that — `to_error_frame` looks perfectly correct on
both sides of the bug — which is exactly why this lives in the browser.

Run at both viewports: the CTA sits under the last bubble in a wrapping flex
row, and on a 390px phone a button that overflows its bubble is as broken as
one that never rendered.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.e2e import harness
from tests.e2e.conftest import VIEWPORTS

pytestmark = pytest.mark.e2e

COMPOSER = ".composer textarea"
SEND_BUTTON = ".composer button[aria-label='Send']"
ASSISTANT_BUBBLE = ".bubble.assistant"
NEW_CHAT_LABEL = "Start a new chat"
# Distinctive half of the classified message — proves the ValueError was
# classified rather than surfaced raw.
CLASSIFIED_MARKER = "could not read part of this conversation"


@pytest.fixture(scope="module")
def refusal_dashboard() -> Iterator[harness.RunningDashboard]:
    """A dashboard whose only agent fails with the unsupported-block error."""
    if not harness.npm_available():
        pytest.skip("npm not on PATH — cannot launch the dashboard UI")
    if not harness.node_modules_present():
        pytest.skip(
            "dashboard node_modules missing — run `cd olano/dashboard_2.0 && npm install` first"
        )
    try:
        with harness.launch_dashboard(harness.build_block_refusal_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


def _send(page, text: str) -> None:
    composer = page.locator(COMPOSER)
    composer.wait_for(state="visible", timeout=15_000)
    page.wait_for_function(
        "(label) => { const e = document.querySelector('.topbar .muted');"
        " return e && e.textContent.trim() === label; }",
        arg="merli",
        timeout=15_000,
    )
    composer.click()
    composer.fill(text)
    send = page.locator(SEND_BUTTON)
    send.wait_for(state="visible", timeout=10_000)
    page.wait_for_function("el => el && !el.disabled", arg=send.element_handle(), timeout=10_000)
    send.click()


@pytest.mark.parametrize("viewport", sorted(VIEWPORTS))
def test_stuck_thread_error_offers_a_new_chat(refusal_dashboard, viewport: str) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - environment dependent
        pytest.skip("playwright not installed")

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable — run `playwright install chromium` ({exc})")
        context = browser.new_context(**VIEWPORTS[viewport])
        page = context.new_page()
        try:
            base = refusal_dashboard.base_url.rstrip("/")
            page.goto(base + "/chat", wait_until="domcontentloaded")
            _send(page, "ping")

            bubble = page.locator(ASSISTANT_BUBBLE).first
            bubble.wait_for(state="visible", timeout=30_000)
            page.wait_for_function(
                "([el, marker]) => el && el.textContent.includes(marker)",
                arg=[bubble.element_handle(), CLASSIFIED_MARKER],
                timeout=30_000,
            )
            body = bubble.text_content() or ""
            # The raw exception must never reach the bubble.
            assert "ValueError" not in body, f"error surfaced unclassified: {body[:200]!r}"

            # The regression: with no `action` on the remediation this button
            # does not exist, and the message's instruction has no affordance.
            cta = page.get_by_role("button", name=NEW_CHAT_LABEL)
            cta.wait_for(state="visible", timeout=10_000)

            # It must sit inside the conversation, not overflow it on a phone.
            box = cta.bounding_box()
            assert box is not None and box["width"] > 0
            assert (
                box["x"] >= 0
                and box["x"] + box["width"] <= VIEWPORTS[viewport]["viewport"]["width"] + 1
            ), f"CTA overflows the {viewport} viewport: {box}"

            # And it must actually work: the failed conversation is replaced by
            # an empty one, so the error (and the CTA with it) is gone.
            cta.click()
            page.wait_for_function(
                "(marker) => !document.body.textContent.includes(marker)",
                arg=CLASSIFIED_MARKER,
                timeout=10_000,
            )
            assert page.locator(ASSISTANT_BUBBLE).count() == 0
        finally:
            browser.close()
