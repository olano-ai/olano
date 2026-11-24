"""Config page: the Save button never scrolls away, and edits are never lost quietly.

The Config page is ~30 cards long. Its Save button used to sit in the header
row and scroll off-screen with it, so someone editing a field near the bottom
had no cue that anything needed saving — and nothing stopped them clicking away.
Three behaviours guard that now, and each is exercised here at both viewports:

* **Save stays reachable** — the actions live in the sticky topbar; after
  scrolling to the very bottom the Save button is still inside the viewport.
* **Leaving asks** — with an unsaved edit, a sidebar click opens a 3-way
  dialog. Esc stays (the edit survives); *Leave without saving* navigates and
  the edit is gone; *Save and leave* persists it, then navigates.
* **Closing asks** — the tab's own ``beforeunload`` prompt fires while dirty and
  does not fire once clean. Playwright only surfaces that dialog on
  ``page.close(run_before_unload=True)``, so this runs on a second page.

Leaving clicks the sidebar's "New chat" link (the one anchor every layout
shows; the nav groups sit collapsed under MORE by default). Edits use the
*Messages / minute* rate-limit input — a plain number field with
a harmless value — and every path that saves puts the field back through the
UI afterwards, so the session-scoped dashboard is unchanged for the next test.
"""

from __future__ import annotations

import pytest

from tests.e2e.route_checks import HAMBURGER, SIDEBAR, open_mobile_nav

pytestmark = pytest.mark.e2e

ACTIONS = "[data-testid='config-actions']"
SAVE = f"{ACTIONS} button.primary"
PILL = "[data-testid='config-unsaved']"
FIELD = "section.card:has-text('Message rate limits') input[type='number']"
DIALOG_TITLE = "Save your changes?"
LEAVE = "[data-testid='unsaved-leave']"
SAVE_LEAVE = "[data-testid='unsaved-save-leave']"


def _open_config(page, dashboard):
    page.goto(dashboard.base_url.rstrip("/") + "/config", wait_until="domcontentloaded")
    page.locator(ACTIONS).wait_for(state="visible", timeout=20_000)
    field = page.locator(FIELD).first
    field.wait_for(state="visible", timeout=20_000)  # the form has loaded
    return field


def _edit(page, field, value: str) -> None:
    field.scroll_into_view_if_needed()
    field.fill(value)
    field.blur()
    page.wait_for_timeout(200)


def _click_sidebar(page, href: str) -> None:
    if page.locator(HAMBURGER).is_visible():
        open_mobile_nav(page)
    page.locator(f"{SIDEBAR} a[href='{href}']").first.click()


def _in_viewport(page, locator) -> bool:
    box = locator.bounding_box()
    height = page.evaluate("() => window.innerHeight")
    return box is not None and box["y"] >= 0 and box["y"] + box["height"] <= height


def test_save_stays_reachable_at_the_bottom_of_the_page(page, dashboard) -> None:
    _open_config(page, dashboard)
    # The content pane scrolls inside <main class="main"> (shared across
    # routes), not the window — scrolling the window would be a silent no-op.
    page.evaluate(
        "() => { const m = document.querySelector('main.main'); m.scrollTop = m.scrollHeight; }"
    )
    page.wait_for_timeout(300)
    scrolled = page.evaluate("() => document.querySelector('main.main').scrollTop")
    assert scrolled > 500, scrolled  # it really is a long page
    save = page.locator(SAVE)
    assert save.is_visible()
    assert _in_viewport(page, save), save.bounding_box()
    # Nothing in the topbar widened the page (the buttons wrap on a phone).
    scroll_w, inner_w = page.evaluate("() => [document.documentElement.scrollWidth, innerWidth]")
    assert scroll_w <= inner_w + 1, (scroll_w, inner_w)


def test_leaving_with_an_edit_asks_and_esc_stays(page, dashboard) -> None:
    field = _open_config(page, dashboard)
    assert page.locator(PILL).count() == 0
    _edit(page, field, "30")
    page.locator(PILL).wait_for(state="visible", timeout=5_000)

    _click_sidebar(page, "/chat/merli")  # the agent row links to its chat
    page.get_by_text(DIALOG_TITLE).first.wait_for(state="visible", timeout=10_000)
    page.keyboard.press("Escape")
    page.get_by_text(DIALOG_TITLE).first.wait_for(state="detached", timeout=5_000)
    # Still here, still dirty, edit intact.
    assert page.url.rstrip("/").endswith("/config")
    assert page.locator(PILL).is_visible()
    assert page.locator(FIELD).first.input_value() == "30"

    # Leave without saving: the navigation happens and the edit is gone.
    _click_sidebar(page, "/chat/merli")  # the agent row links to its chat
    page.locator(LEAVE).wait_for(state="visible", timeout=10_000)
    page.locator(LEAVE).click()
    # The chat lands on its agent: /chat/merli (core/routeUrls.ts).
    page.wait_for_url("**/chat/merli**", timeout=10_000)
    field = _open_config(page, dashboard)
    assert field.input_value() == ""
    assert page.locator(PILL).count() == 0


def test_save_and_leave_persists_then_navigates(page, dashboard) -> None:
    field = _open_config(page, dashboard)
    _edit(page, field, "30")
    _click_sidebar(page, "/chat/merli")  # the agent row links to its chat
    page.locator(SAVE_LEAVE).wait_for(state="visible", timeout=10_000)
    page.locator(SAVE_LEAVE).click()
    page.wait_for_url("**/chat/merli**", timeout=15_000)

    field = _open_config(page, dashboard)
    assert field.input_value() == "30"  # it was saved, not dropped
    assert page.locator(PILL).count() == 0

    # Put it back through the UI so later tests see the default (unlimited).
    _edit(page, field, "")
    page.locator(PILL).wait_for(state="visible", timeout=5_000)
    page.locator(SAVE).click()
    page.locator(PILL).wait_for(state="detached", timeout=15_000)
    field = _open_config(page, dashboard)
    assert field.input_value() == ""


def test_closing_the_tab_prompts_only_while_dirty(page, dashboard) -> None:
    seen: list[str] = []

    # Clean page: no prompt.
    clean = page.context.new_page()
    _open_config(clean, dashboard)
    clean.once("dialog", lambda d: (seen.append(d.type), d.accept()))
    clean.close(run_before_unload=True)
    page.wait_for_timeout(300)
    assert seen == [], seen

    # Dirty page: the browser's beforeunload prompt.
    dirty = page.context.new_page()
    field = _open_config(dirty, dashboard)
    _edit(dirty, field, "30")
    dirty.locator(PILL).wait_for(state="visible", timeout=5_000)
    dirty.once("dialog", lambda d: (seen.append(d.type), d.accept()))
    dirty.close(run_before_unload=True)
    page.wait_for_timeout(500)
    assert seen == ["beforeunload"], seen
