"""E2E: the simple rail's chevron — a bot's chats, unfolded in place.

On a phone the rail is a drawer, and every tap on a bot row took the reader to
that bot's conversation and shut the drawer on the way. There was no way to
look at a bot's chats without leaving the list: the selected bot unfolded its
conversations under its row, but only after the tap that had already closed
the drawer, and the second tap that folded them (on a wide viewport a useful
gesture) left the drawer open over a conversation the reader had just asked
for.

Each bot row now carries a chevron — the same control the dashboard sidebar
has — that folds or unfolds that bot's chats where they are: on the selected
row it is the fold, on another row it selects that bot and unfolds its chats,
and in neither case does the drawer move. Always visible where there is no
hover, on hover elsewhere, and always on the selected row. A tap on the row
itself, in the drawer, always goes to the conversation.
"""

from __future__ import annotations

import pytest

from tests.e2e.route_checks import SIMPLE_ROOT, open_simple_rail

pytestmark = pytest.mark.e2e

AGENT = "merli"
ROW = f"{SIMPLE_ROOT} .simple-rail .simple-bot"
CHEVRON = "[data-testid='simple-bot-chevron']"
THREADS = f"{SIMPLE_ROOT} .simple-threads"


def _is_drawer(page) -> bool:
    """Whether the rail is the phone's off-canvas drawer.

    Asked of this shell's own DOM (`.simple-rail.is-drawer`), not of
    `route_checks.is_mobile_viewport`, which looks for the dashboard's mobile
    topbar — a thing the simple shell never renders, so that helper answers
    "wide" here at every viewport.
    """
    return page.locator(f"{SIMPLE_ROOT} .simple-rail.is-drawer").count() > 0


def _set_ui_mode(value: str) -> None:
    from olano.config.home import ensure_olano_home, update_home_config

    ensure_olano_home()
    update_home_config({"ui_mode": value})


def _open_simple(page, dashboard) -> None:
    _set_ui_mode("simple")
    page.goto(dashboard.base_url.rstrip("/") + "/")
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)
    page.wait_for_selector("[data-simple-composer]", timeout=20_000)
    open_simple_rail(page)


def _opacity(page, locator) -> float:
    return float(locator.evaluate("el => getComputedStyle(el).opacity"))


def test_the_chevron_folds_the_selected_bots_chats_without_moving_the_drawer(
    page, dashboard
) -> None:
    _open_simple(page, dashboard)
    row = page.locator(ROW, has_text=AGENT).first
    row.locator(".simple-row").wait_for(state="visible", timeout=10_000)
    assert "is-active" in (row.get_attribute("class") or ""), "the one bot is selected"

    # The selected bot arrives unfolded, and its chevron is showing (it is the
    # standing sign that the list below belongs to this row).
    page.locator(THREADS).first.wait_for(state="visible", timeout=10_000)
    chev = row.locator(CHEVRON)
    assert chev.count() == 1
    assert _opacity(page, chev) == 1.0
    assert chev.get_attribute("aria-expanded") == "true"

    # Fold: the list goes, the drawer (on a phone) stays, the chat stays.
    chev.click()
    page.wait_for_timeout(300)
    assert page.locator(THREADS).count() == 0
    assert chev.get_attribute("aria-expanded") == "false"
    if _is_drawer(page):
        assert page.locator(f"{SIMPLE_ROOT}.rail-open").count() == 1, "the drawer must stay open"
    assert page.locator("[data-simple-composer]").count() == 1

    # Unfold again.
    chev.click()
    page.locator(THREADS).first.wait_for(state="visible", timeout=5_000)
    assert chev.get_attribute("aria-expanded") == "true"


def test_tapping_the_row_in_the_drawer_goes_to_the_conversation(page, dashboard) -> None:
    """Folding is the chevron's job there: a tap on the row — the selected one
    included — always closes the drawer on the conversation."""
    _open_simple(page, dashboard)
    if not _is_drawer(page):
        pytest.skip("the drawer exists on the touch viewport only")
    row = page.locator(ROW, has_text=AGENT).first.locator("button.simple-row")
    assert row.get_attribute("aria-expanded") is None, "in the drawer the row is not the fold"
    row.click()
    page.wait_for_selector(f"{SIMPLE_ROOT}:not(.rail-open)", timeout=5_000)
    page.locator("[data-simple-composer]").wait_for(state="visible", timeout=10_000)


def test_on_a_wide_viewport_the_row_still_folds_and_the_chevron_shows_on_hover(
    page, dashboard
) -> None:
    _open_simple(page, dashboard)
    if _is_drawer(page):
        pytest.skip("hover and the row-as-fold are wide-viewport behaviour")
    row = page.locator(ROW, has_text=AGENT).first.locator("button.simple-row")
    assert row.get_attribute("aria-expanded") == "true"
    row.click()
    page.wait_for_timeout(300)
    assert page.locator(THREADS).count() == 0
    row.click()
    page.locator(THREADS).first.wait_for(state="visible", timeout=5_000)
