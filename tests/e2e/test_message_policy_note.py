"""A declined turn on the console: the note is drawn, the marker never is.

Drives a real turn through the SPA against :func:`harness.build_silence_gateway`
— a real ``AgentRunner`` whose model prefaces its decline ("Let me check
that.", blank line, the marker on its own line). Three things must hold at both
viewports: the marker text never appears anywhere on the page, the console
draws its "No reply …" note (a silent turn on the operator's own screen reads
as a dropped connection), and the preamble that streamed before the decision
is still there above it — the note explains the silence, it does not rewrite
history.

Runs at both viewports: a phone-width chat wraps the note under the bubble and
must not push it off-screen.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness
from tests.e2e.harness import NO_REPLY_NOTE

COMPOSER = ".composer textarea"
SEND_BUTTON = ".composer button[aria-label='Send']"


@pytest.fixture(scope="module")
def silence_dashboard():
    with harness.launch_dashboard(gateway=harness.build_silence_gateway()) as running:
        yield running


@pytest.fixture(params=["desktop", "mobile"])
def page(request, silence_dashboard):
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(silence_dashboard.base_url, wait_until="domcontentloaded")
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def test_declined_turn_shows_the_note_and_never_the_marker(page, silence_dashboard) -> None:
    from olano.security.reply_policy import SILENCE_MARKER

    page.goto(silence_dashboard.base_url.rstrip("/") + "/chat", wait_until="domcontentloaded")
    composer = page.locator(COMPOSER)
    composer.wait_for(state="visible", timeout=15_000)
    composer.click()
    composer.fill("hola, ¿me puedes ayudar?")
    send = page.locator(SEND_BUTTON)
    send.wait_for(state="visible", timeout=10_000)
    page.wait_for_function("el => el && !el.disabled", arg=send.element_handle(), timeout=10_000)
    send.click()

    note = page.get_by_text(NO_REPLY_NOTE, exact=False).first
    note.wait_for(state="visible", timeout=30_000)

    body = page.inner_text("body")
    assert SILENCE_MARKER not in body
    assert "Let me check that." in body  # the preamble already streamed; kept
    # The note is emphasised markdown, not literal markup: the first cut drew
    # `_No reply … policy)._` with both underscores visible.
    assert f"_{NO_REPLY_NOTE}" not in body and f"*{NO_REPLY_NOTE}" not in body
    assert note.evaluate("el => el.closest('em') !== null || el.tagName === 'EM'")
    # The note is inside the viewport, not wrapped off the bottom of a phone.
    box = note.bounding_box()
    assert box is not None
    height = page.evaluate("() => window.innerHeight")
    assert 0 <= box["y"] <= height - box["height"], (box, height)
