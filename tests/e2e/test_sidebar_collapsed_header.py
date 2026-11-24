"""E2E: the collapsed nav — its header actions, and its section headings.

Collapsed, the sidebar is an icon strip — it was 68px wide, which left 32px of
content box once its 8px margins and 10px padding came off. The bell and the
expand toggle were laid out as a ROW there, and two 29px icon buttons side by
side need about 60px, so the row overflowed by nearly its own width: the expand
toggle rendered outside the panel's rounded edge, half over the page behind it.
That is precisely the control a user goes looking for to get the nav back, so
it had to be the one that spilled.

They stack now, one per row, centred. This asserts the geometry rather than the
CSS: any future layout that puts them side by side again fails here.

The same strip hid every `.nav-label`, which left Pinned, All agents, Group
chats, each custom group and More as bare chevrons — four identical `>` glyphs
naming nothing and separating nothing. The headings keep their words now, the
way the simple shell's collapsed rail has always drawn them: wrapped and
centred under a small chevron, with a hairline above each section.

Desktop only — below the breakpoint the sidebar is an off-canvas drawer with no
collapsed state at all (Layout strips the class).
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import SIDEBAR, is_mobile_viewport

pytestmark = pytest.mark.e2e

# Scoped to the sidebar: the mobile topbar renders its own bell (CSS-hidden on
# desktop but still in the DOM), so a bare `.notification-bell` resolves to two
# elements and fails Playwright's strict mode.
TOGGLE = f"{SIDEBAR} button.sidebar-toggle"
BELL = f"{SIDEBAR} button.notification-bell"
COLLAPSED = ".app-shell.sidebar-collapsed"


def _collapse(page, dashboard) -> None:
    """Land on a view and collapse the nav, skipping where there is no such state.

    The viewport check has to come AFTER the shell mounts: ``is_mobile_viewport``
    asks the DOM whether the mobile topbar is on screen, which is the right
    question and answers ``False`` for every viewport while React is still
    booting. Asked too early it let the mobile runs through to a 30s timeout
    clicking a CSS-hidden button.
    """
    page.goto(dashboard.base_url.rstrip("/") + "/overview", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    if is_mobile_viewport(page):
        pytest.skip("the collapsed rail is a desktop state; mobile gets a drawer")
    if page.locator(COLLAPSED).count() == 0:
        page.locator(TOGGLE).click()
    page.wait_for_selector(COLLAPSED, timeout=5_000)
    page.wait_for_timeout(400)  # the grid-template transition


def test_the_collapsed_header_actions_stay_inside_the_rail(page, dashboard) -> None:
    """Neither button may cross the panel's own edges — the actual defect."""
    _collapse(page, dashboard)

    rail = page.locator(SIDEBAR).bounding_box()
    assert rail is not None
    for name, sel in (("expand toggle", TOGGLE), ("notification bell", BELL)):
        box = page.locator(sel).bounding_box()
        assert box is not None, f"{name} has no layout box"
        assert (
            box["x"] >= rail["x"] - 1 and box["x"] + box["width"] <= rail["x"] + rail["width"] + 1
        ), f"{name} spills out of the collapsed rail: button {box}, rail {rail}"


def test_the_collapsed_header_actions_are_stacked_not_side_by_side(page, dashboard) -> None:
    """The fix itself: a column. Side by side is what overflowed."""
    _collapse(page, dashboard)

    bell = page.locator(BELL).bounding_box()
    toggle = page.locator(TOGGLE).bounding_box()
    assert bell is not None and toggle is not None
    assert toggle["y"] >= bell["y"] + bell["height"] - 1, (
        f"the two actions share a row again: bell {bell}, toggle {toggle}"
    )


def test_the_collapsed_sections_keep_their_headings(page, dashboard) -> None:
    """Every section heading is readable in the strip, and each is a divided
    band rather than one of four identical chevrons."""
    _collapse(page, dashboard)

    heads = page.locator(f"{SIDEBAR} .bot-section-header")
    assert heads.count() >= 2, "the agent list should have at least Pinned and All agents"
    labels = [h.strip() for h in heads.all_inner_texts()]
    for want in ("PINNED", "ALL AGENTS"):
        assert any(want in label.upper() for label in labels), labels

    # Readable, not merely present: a label hidden the old way (width 0,
    # opacity 0) has a box but no visible one.
    first = heads.first.locator(".nav-label").first
    box = first.bounding_box()
    assert box is not None and box["width"] > 8 and box["height"] > 6, box
    assert float(first.evaluate("el => getComputedStyle(el).opacity")) == 1.0

    # The heading wraps inside the strip instead of overflowing it.
    rail = page.locator(SIDEBAR).bounding_box()
    head_box = heads.first.bounding_box()
    assert rail is not None and head_box is not None
    assert head_box["x"] >= rail["x"] - 1
    assert head_box["x"] + head_box["width"] <= rail["x"] + rail["width"] + 1

    # And More, below the rule, is a heading too — not a lone chevron.
    more = page.locator(f"{SIDEBAR} .nav-group-header:not(.bot-section-header)").first
    assert "MORE" in more.inner_text().strip().upper()


def test_the_toggle_still_expands_the_nav(page, dashboard) -> None:
    """It has to be clickable where it now sits, not merely inside the box."""
    _collapse(page, dashboard)

    page.locator(TOGGLE).click()
    page.wait_for_selector(COLLAPSED, state="detached", timeout=5_000)
