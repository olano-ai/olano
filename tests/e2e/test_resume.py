"""A turn the server cancels is reported as stopped, never as a lost connection.

These two states used to be the same pixel. A chat stream that ended without a
terminal frame meant "the turn died with your socket", because it always had —
the turn ran inside its own SSE response generator. Detached runs
(``olano/core/runs.py``) split them apart, and the split is the point:

* the socket dropping is now a **viewer leaving**, and the browser rejoins the
  still-running turn (``test_reattach.py`` covers that half);
* a turn that genuinely ends early — cancelled by ``POST /api/chat/stop``, or by
  the runtime shutting down — says so on the run itself, as a ``stopped`` status
  frame followed by the ordinary terminal ``done``.

So the cut-stream runner, which raises ``CancelledError`` from inside the agent
stream, must now produce a turn that settles cleanly: the partial reply stays,
no raw ``**Error:**`` is appended, and **no Interrupted/Retry affordance
appears** — because the connection was never the thing that failed. Interrupted
is reserved for the one case that is still honest: the stream ended, and there
is no run left to rejoin.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.e2e import harness

pytestmark = pytest.mark.e2e

COMPOSER = ".composer textarea"
SEND_BUTTON = ".composer button[aria-label='Send']"
ASSISTANT_BUBBLE = ".bubble.assistant"
# The cut-stream runner's last streamed word — once visible, the turn is
# established (start frame seen) and about to be cancelled.
PARTIAL_MARKER = harness.CHAT_STALL_PREFIX.split(" ")[-1]  # "QWXYZ"


@pytest.fixture(scope="module")
def cut_dashboard() -> Iterator[harness.RunningDashboard]:
    """A dashboard whose only agent's turn is cancelled mid-stream."""
    if not harness.npm_available():
        pytest.skip("npm not on PATH — cannot launch the dashboard UI")
    if not harness.node_modules_present():
        pytest.skip(
            "dashboard node_modules missing — run `cd olano/dashboard_2.0 && npm install` first"
        )
    try:
        with harness.launch_dashboard(harness.build_cut_stream_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


def test_a_cancelled_turn_settles_without_claiming_a_lost_connection(cut_dashboard) -> None:
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
            base = cut_dashboard.base_url.rstrip("/")
            page.goto(base + "/chat", wait_until="domcontentloaded")

            # Send a message once the roster has resolved (mirrors test_chat).
            composer = page.locator(COMPOSER)
            composer.wait_for(state="visible", timeout=15_000)
            page.wait_for_function(
                "(label) => { const e = document.querySelector('.topbar .muted');"
                " return e && e.textContent.trim() === label; }",
                arg="merli",
                timeout=15_000,
            )
            composer.click()
            composer.fill("ping")
            send = page.locator(SEND_BUTTON)
            send.wait_for(state="visible", timeout=10_000)
            page.wait_for_function(
                "el => el && !el.disabled", arg=send.element_handle(), timeout=10_000
            )
            send.click()

            # The partial reply streams in; ~2s later the runner is cancelled.
            bubble = page.locator(ASSISTANT_BUBBLE).first
            bubble.wait_for(state="visible", timeout=30_000)
            page.wait_for_function(
                "([el, marker]) => el && el.textContent.includes(marker)",
                arg=[bubble.element_handle(), PARTIAL_MARKER],
                timeout=30_000,
            )

            # Give the cancellation time to land and the turn to settle.
            page.wait_for_function(
                "() => !document.querySelector('.composer button[aria-label=\"Stop\"]')",
                timeout=30_000,
            )

            body = (bubble.text_content() or "").strip()
            assert PARTIAL_MARKER in body, "the partial reply was dropped on cancellation"
            assert "Error:" not in body, f"cancellation rendered as an error: {body[:200]!r}"
            # The turn ended on the server's own terms, so nothing may tell the
            # reader their connection failed or offer to resend.
            assert page.get_by_text("Interrupted", exact=True).count() == 0
            assert page.get_by_role("button", name="Retry").count() == 0

            # And the thread is idle: a new turn may start on it immediately
            # (the one-run-per-thread rule released the key).
            composer.click()
            composer.fill("again")
            send.wait_for(state="visible", timeout=10_000)
            page.wait_for_function(
                "el => el && !el.disabled", arg=send.element_handle(), timeout=10_000
            )
        finally:
            context.close()
            browser.close()
