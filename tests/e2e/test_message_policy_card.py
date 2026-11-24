"""The Message policy card configures `message_policy` — every knob, both ways.

"Configurable in the dashboard" is a stronger claim than "renders": a value typed
here must reach `agent.json5`, come back after a reload, and a value the backend
refuses must say so where the operator is looking. This drives the card on the
Multi-user tab against the mock gateway (a real runtime, a real `PUT
/api/agents/{name}`, a real `agent.json5` under the throwaway home) and checks:

* every one of the seven fields has a control, and the dependent rows appear
  only once they have a subject (the oversize action needs a limit, the ignore
  notice needs something that ignores);
* a saved value survives a full page reload and is what `GET /api/agents/{name}`
  reports — the two surfaces cannot disagree;
* an invalid regex is refused with an error toast naming the problem, and the
  stored value is untouched — a rule can never silently do nothing;
* the schema-driven Advanced tab lists `message_policy` as managed and points
  here, so the field is never stranded behind the raw editor.

Both viewports: on a phone the card is a single column and the number inputs
wrap under their hints; nothing may push the page wider than the screen.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

AGENT = "merli"  # seeded by the mock gateway
CARD_TITLE = "Message policy"


@pytest.fixture(scope="module")
def policy_dashboard():
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    try:
        with harness.launch_dashboard(gateway=harness.build_mock_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


@pytest.fixture(params=["desktop", "mobile"])
def viewport(request):
    return request.param


@pytest.fixture
def tab(policy_dashboard, viewport):
    """Open the agent's Multi-user tab; yields ``(page, open_tab, base_url)``."""
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[viewport])
        page = ctx.new_page()

        def _open(which: str = "Multi-user"):
            page.goto(f"{policy_dashboard.base_url}/agents/{AGENT}", wait_until="domcontentloaded")
            page.locator("h1").first.wait_for(state="visible", timeout=30_000)
            page.locator("button", has_text=which).first.click()
            page.wait_for_timeout(400)
            return page

        try:
            yield page, _open, policy_dashboard.base_url
        finally:
            ctx.close()
            browser.close()


def _card(page):
    card = page.locator(".card", has_text=CARD_TITLE).first
    card.wait_for(state="visible", timeout=15_000)
    return card


def _stored_policy(page, base_url: str) -> dict:
    res = page.request.get(f"{base_url}/api/agents/{AGENT}")
    assert res.ok, res.status
    return res.json().get("message_policy") or {}


def _settle(page, ms: int = 900) -> None:
    # The card auto-saves on blur (a PUT); give it a beat, like the Overview.
    page.wait_for_timeout(ms)


def test_every_field_has_a_control_and_dependent_rows_follow_their_subject(tab) -> None:
    page, open_tab, base_url = tab
    open_tab()
    card = _card(page)

    # With nothing set, the two dependent rows are absent — they would be
    # inert controls with nothing to act on.
    assert card.get_by_text("When a message is too long").count() == 0
    assert card.get_by_text("What an ignored sender sees").count() == 0

    numbers = card.locator("input[placeholder='∞']")
    assert numbers.count() == 2  # max input, max output
    max_input, max_output = numbers.nth(0), numbers.nth(1)
    max_input.fill("500")
    max_input.blur()
    _settle(page)
    card.get_by_text("When a message is too long").wait_for(state="visible", timeout=10_000)
    # 'reject' is the default, so the notice row is shown too.
    card.get_by_text("Too-long notice").wait_for(state="visible", timeout=5_000)

    # Switching the action to 'ignore' swaps the notice row for the ignore one.
    card.locator("select").first.select_option("ignore")
    _settle(page)
    card.get_by_text("What an ignored sender sees").wait_for(state="visible", timeout=10_000)
    assert card.get_by_text("Too-long notice").count() == 0

    max_output.fill("300")
    max_output.blur()
    _settle(page)

    textareas = card.locator("textarea")
    assert textareas.count() == 2  # patterns, rules
    textareas.nth(0).fill(r"^\W*$")
    textareas.nth(0).blur()
    page.get_by_text("Ignore patterns saved").first.wait_for(state="visible", timeout=10_000)
    textareas.nth(1).fill("The message is not written in English")
    textareas.nth(1).blur()
    page.get_by_text("Silence rules saved").first.wait_for(state="visible", timeout=10_000)
    _settle(page)

    stored = _stored_policy(page, base_url)
    assert stored.get("max_input_chars") == 500
    assert stored.get("on_oversize") == "ignore"
    assert stored.get("max_output_tokens") == 300
    assert stored.get("ignore_patterns") == [r"^\W*$"]
    assert stored.get("ignore_rules") == ["The message is not written in English"]

    # Phone width: the card must not widen the page.
    scroll_w, inner_w = page.evaluate("() => [document.documentElement.scrollWidth, innerWidth]")
    assert scroll_w <= inner_w + 1, (scroll_w, inner_w)


def test_saved_values_survive_a_reload(tab) -> None:
    page, open_tab, base_url = tab
    open_tab()
    card = _card(page)
    max_input = card.locator("input[placeholder='∞']").nth(0)
    max_input.fill("777")
    max_input.blur()
    _settle(page)

    page = open_tab()  # full navigation, fresh state
    card = _card(page)
    assert card.locator("input[placeholder='∞']").nth(0).input_value() == "777"
    assert _stored_policy(page, base_url).get("max_input_chars") == 777

    # Clearing the box is a real value — null, "no limit" — not a bounce back
    # to the previous number, and the dependent row goes with it.
    max_input = card.locator("input[placeholder='∞']").nth(0)
    max_input.fill("")
    max_input.blur()
    _settle(page)
    page = open_tab()
    card = _card(page)
    assert card.locator("input[placeholder='∞']").nth(0).input_value() == ""
    assert _stored_policy(page, base_url).get("max_input_chars") is None
    assert card.get_by_text("When a message is too long").count() == 0


def test_an_invalid_regex_is_refused_with_a_toast_and_nothing_is_stored(tab) -> None:
    page, open_tab, base_url = tab
    open_tab()
    card = _card(page)
    patterns = card.locator("textarea").nth(0)
    patterns.fill("spam")
    patterns.blur()
    page.get_by_text("Ignore patterns saved").first.wait_for(state="visible", timeout=10_000)
    _settle(page)
    assert _stored_policy(page, base_url).get("ignore_patterns") == ["spam"]

    patterns.fill("(unclosed")
    patterns.blur()
    # The backend's 422 names the problem; the card must show it, not a
    # generic "save failed" — an operator has to know which line is wrong.
    toast = page.locator("[role='alert']").first
    toast.wait_for(state="visible", timeout=10_000)
    assert "regular expression" in toast.inner_text()

    # And the bad line never reached disk: a reload shows the last good value.
    page = open_tab()
    card = _card(page)
    assert card.locator("textarea").nth(0).input_value() == "spam"
    assert _stored_policy(page, base_url).get("ignore_patterns") == ["spam"]


def test_the_advanced_tab_defers_to_the_card(tab) -> None:
    page, open_tab, _ = tab
    open_tab("Advanced")
    managed = page.locator("text=message_policy").first
    managed.wait_for(state="visible", timeout=15_000)
    # It sits under the managed list with a jump button, not as a generic
    # object editor — the fields interact, and a flat JSON box loses that.
    row = page.locator(".kv-row, .row", has_text="message_policy").first
    assert row.locator("button").count() >= 1
    assert (
        page.locator("textarea[name='message_policy'], input[name='message_policy']").count() == 0
    )
