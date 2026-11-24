"""Detached-run E2E: the turn survives losing its viewer, and the browser rejoins.

This is the reported defect, end to end. A long reply was sent from a phone, the
app was switched and the network moved 5G→4G; the browser came back to
"Connection lost while … was replying" over a plan frozen mid-way, and the work
had genuinely stopped — ``POST /api/chat/stream`` ran the turn inside its own
response generator, so the socket dying killed the agent (``olano/core/runs.py``
has the full account).

The dashboard boots with a *slow* runner: half the reply, a several-second gap,
then the rest. The test cuts the browser off inside that gap by **reloading the
page** — the bluntest, most faithful version of "the tab went away", and the one
that also proves the hardest recovery path, since a reload empties the whole
in-memory session store. Everything the client needs to rejoin therefore has to
come back from the server: the thread payload's ``run`` object, and the cursor
in it.

What must be true afterwards: the second half of the reply arrives in the
reloaded tab, and no Interrupted/Retry affordance appears — because nothing was
interrupted.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.e2e import harness

pytestmark = pytest.mark.e2e

COMPOSER = ".composer textarea"
SEND_BUTTON = ".composer button[aria-label='Send']"
ASSISTANT_BUBBLE = ".bubble.assistant"
# The halves of the slow runner's reply: the first is on screen before the cut,
# the second can only arrive if the run kept going without a viewer.
FIRST_HALF = harness.CHAT_SLOW_PREFIX.split(" ")[-1]  # "QWXYZ"
SECOND_HALF = harness.CHAT_SLOW_SUFFIX.split(" ")[-1]  # "PLMNB"


@pytest.fixture(scope="module")
def slow_dashboard() -> Iterator[harness.RunningDashboard]:
    if not harness.npm_available():
        pytest.skip("npm not on PATH — cannot launch the dashboard UI")
    if not harness.node_modules_present():
        pytest.skip(
            "dashboard node_modules missing — run `cd olano/dashboard_2.0 && npm install` first"
        )
    try:
        with harness.launch_dashboard(harness.build_slow_stream_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


def _send(page, base: str, message: str) -> None:
    page.goto(base + "/chat", wait_until="domcontentloaded")
    composer = page.locator(COMPOSER)
    composer.wait_for(state="visible", timeout=15_000)
    page.wait_for_function(
        "(label) => { const e = document.querySelector('.topbar .muted');"
        " return e && e.textContent.trim() === label; }",
        arg="merli",
        timeout=15_000,
    )
    composer.click()
    composer.fill(message)
    send = page.locator(SEND_BUTTON)
    send.wait_for(state="visible", timeout=10_000)
    page.wait_for_function("el => el && !el.disabled", arg=send.element_handle(), timeout=10_000)
    send.click()


def test_a_reloaded_tab_rejoins_the_running_turn(slow_dashboard) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - environment dependent
        pytest.skip("playwright not installed")

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable — run `playwright install chromium` ({exc})")
        context = browser.new_context()
        page = context.new_page()
        try:
            base = slow_dashboard.base_url.rstrip("/")
            _send(page, base, "ping")

            # First half on screen — the turn is established and mid-gap.
            bubble = page.locator(ASSISTANT_BUBBLE).first
            bubble.wait_for(state="visible", timeout=30_000)
            page.wait_for_function(
                "([el, marker]) => el && el.textContent.includes(marker)",
                arg=[bubble.element_handle(), FIRST_HALF],
                timeout=30_000,
            )
            thread_url = page.url

            # The tab goes away mid-turn. Before detached runs this killed the
            # agent; now it only kills the view.
            page.reload(wait_until="domcontentloaded")
            page.goto(thread_url, wait_until="domcontentloaded")

            # The reloaded tab knows nothing except what the server tells it —
            # and the server says a run is live on this thread, with a cursor.
            reloaded = page.locator(ASSISTANT_BUBBLE).first
            reloaded.wait_for(state="visible", timeout=30_000)
            page.wait_for_function(
                "([el, marker]) => el && el.textContent.includes(marker)",
                arg=[reloaded.element_handle(), SECOND_HALF],
                timeout=45_000,
            )

            body = reloaded.text_content() or ""
            assert FIRST_HALF in body, "the reattached turn lost the half it had already streamed"
            assert "Error:" not in body, f"reattach rendered as an error: {body[:200]!r}"
            # Nothing was interrupted, so nothing may claim it was.
            assert page.get_by_text("Interrupted", exact=True).count() == 0
        finally:
            context.close()
            browser.close()
