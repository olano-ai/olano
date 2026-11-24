"""Context-bar E2E: a tool-heavy turn must not report past the context window.

Regression for the dashboard showing e.g. ``433.2k / 128k`` — a context bar
reading far above 100%. Root cause: the runner's ``usage`` frame ``input_tokens``
SUMS every model sub-call in a turn (correct for billing), but the header bar
was reusing that sum as the live context-window *fill*. A turn with several
tool-calling rounds therefore stacked each round's full prompt into one number.

This drives the REAL ``AgentRunner`` (via ``build_context_sum_gateway``) over a
mock deepagent that streams three sub-calls (90k → 110k → 120k input tokens),
then asserts the header shows the LAST sub-call's fill (``120.0k``) and never
the summed ``320.0k``. The window it is shown against comes from the synced
catalog, so only the fill is asserted — see the note in the first test.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness
from tests.e2e.harness import CONTEXT_LAST_FILL, CONTEXT_SUBCALL_SUM

pytestmark = pytest.mark.e2e

COMPOSER = ".composer textarea"
SEND_BUTTON = ".composer button[aria-label='Send']"
ASSISTANT_BUBBLE = ".bubble.assistant"
CTX_BAR = ".chat-header .chat-chip.is-context .tabular"


def _drive(page, base_url: str) -> None:
    """Open /chat, wait for the roster, send one message, wait for the reply."""
    page.goto(base_url.rstrip("/") + "/chat", wait_until="domcontentloaded")
    composer = page.locator(COMPOSER)
    composer.wait_for(state="visible", timeout=15_000)
    page.wait_for_function(
        "(label) => { const e = document.querySelector('.topbar .muted');"
        " return e && e.textContent.trim() === label; }",
        arg="merli",
        timeout=15_000,
    )
    composer.click()
    composer.fill("run a big multi-step task")
    send = page.locator(SEND_BUTTON)
    send.wait_for(state="visible", timeout=10_000)
    page.wait_for_function("el => el && !el.disabled", arg=send.element_handle(), timeout=10_000)
    send.click()
    page.locator(ASSISTANT_BUBBLE).first.wait_for(state="visible", timeout=30_000)


# The bar renders "0.0k / 128k" from mount (a fresh session has no fill yet); the
# usage frame that carries the real fill only lands at the END of the turn. So
# wait for a NON-zero fill before asserting, not merely for the bar to exist.
_NONZERO_FILL = (
    "el => el && /k \\/ /.test(el.textContent || '')"
    " && !/^0\\.0k /.test((el.textContent || '').trim())"
)


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_context_bar_shows_last_call_fill_not_turn_sum(page, context_dashboard) -> None:
    _drive(page, context_dashboard.base_url)

    bar = page.locator(CTX_BAR)
    bar.wait_for(state="visible", timeout=15_000)
    page.wait_for_function(_NONZERO_FILL, arg=bar.element_handle(), timeout=15_000)
    text = (bar.text_content() or "").strip()

    # Assert the FILL only. The denominator is whatever the synced catalog says
    # this model's window is, and pinning it here made the test pass for the
    # wrong reason: it matched "128k" — the provider-prefix *fallback* in
    # models.ts — which only shows up while the catalog hasn't resolved yet.
    # Once it does, the real 1M window lands and the string no longer matches.
    used_k = float(text.split(" / ")[0].replace("k", ""))
    last_k = CONTEXT_LAST_FILL / 1000
    sum_k = CONTEXT_SUBCALL_SUM / 1000
    assert used_k == pytest.approx(last_k, abs=0.1), f"context bar showed {text!r}"
    # The pre-fix bug printed the summed turn input (320k) as the live fill.
    assert used_k != pytest.approx(sum_k, abs=0.1), f"context bar showed the turn sum: {text!r}"


@pytest.mark.parametrize("page", ["desktop"], indirect=True)
def test_context_bar_never_exceeds_window(page, context_dashboard) -> None:
    """Whatever the numbers, used/total must stay ≤ 100% (no overflow)."""
    _drive(page, context_dashboard.base_url)
    bar = page.locator(CTX_BAR)
    bar.wait_for(state="visible", timeout=15_000)
    page.wait_for_function(_NONZERO_FILL, arg=bar.element_handle(), timeout=15_000)
    text = (bar.text_content() or "").strip()
    used_k, total_k = (float(p.replace("k", "")) for p in text.split(" / "))
    assert used_k <= total_k, f"context fill {used_k}k exceeds window {total_k}k: {text!r}"


@pytest.fixture(scope="module")
def context_dashboard():
    """A dashboard backed by the real-runner context-sum gateway."""
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    try:
        with harness.launch_dashboard(harness.build_context_sum_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


# `context_dashboard` (module-scoped) must drive the `page` fixture instead of
# the session `dashboard`. Rebind `page` locally so it consumes our dashboard.
@pytest.fixture
def page(request, context_dashboard):
    try:
        browser_pw = sync_playwright
    except NameError:  # pragma: no cover
        pytest.skip("playwright not installed")
    from tests.e2e.conftest import VIEWPORTS

    with browser_pw() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(context_dashboard.base_url, wait_until="domcontentloaded")
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()
