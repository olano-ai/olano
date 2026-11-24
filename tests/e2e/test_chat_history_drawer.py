"""E2E: the chat thread rail is a right-hand drawer, closed on arrival.

It used to be a permanent 260px column — a fifth of a desktop window, and about
a third of a phone's chrome-free height — spent on a list the reader consults
for a second and is then done with. On a phone that letterboxed the messages and
pushed the composer toward the fold; on a desktop it simply never gave the space
back.

Now the conversation owns the full width at every viewport and the list arrives
from the right edge behind the header's history button, over the conversation
rather than beside it, dismissed by the backdrop, the drawer's own ✕, Escape, or
picking a conversation.

Every case runs at both viewports, since the behaviour no longer differs between
them — only the drawer's width cap does, which is what the last test checks.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e

TOGGLE = "[data-testid='chat-history-toggle']"
RAIL = "aside.chat-rail"
DRAWER = "aside.chat-rail.is-drawer"
BACKDROP = ".chat-rail-backdrop"


def _is_mobile(page) -> bool:
    return page.locator("header.mobile-topbar").is_visible()


def _open_chat(page, dashboard) -> None:
    page.goto(dashboard.base_url.rstrip("/") + "/chat", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    page.wait_for_selector(TOGGLE, timeout=15_000)


def test_history_is_closed_until_the_button_asks_for_it(page, dashboard) -> None:
    """The conversation starts with the whole width — no rail in the DOM at all."""
    _open_chat(page, dashboard)
    assert page.locator(RAIL).count() == 0

    page.locator(TOGGLE).click()
    page.wait_for_selector(DRAWER, timeout=5_000)
    assert page.locator(BACKDROP).is_visible()


def test_the_drawer_hangs_off_the_right_edge_over_the_conversation(page, dashboard) -> None:
    """Out of flow and right-anchored: the point is that the conversation never
    reflows to make room, and that the panel arrives from the edge the button
    that opens it sits on."""
    _open_chat(page, dashboard)

    log_before = page.locator(".chat-log").bounding_box()
    page.locator(TOGGLE).click()
    page.wait_for_selector(DRAWER, timeout=5_000)
    page.wait_for_timeout(400)  # let the slide-in settle before measuring

    assert page.locator(DRAWER).evaluate("el => getComputedStyle(el).position") == "fixed"

    box = page.locator(DRAWER).bounding_box()
    assert box is not None
    viewport = page.viewport_size["width"]
    # Flush with the right edge (allow a sub-pixel rounding slack).
    assert abs((box["x"] + box["width"]) - viewport) <= 1, f"drawer is not right-anchored: {box}"

    log_after = page.locator(".chat-log").bounding_box()
    assert log_before is not None and log_after is not None
    assert abs(log_after["width"] - log_before["width"]) <= 1, (
        "the conversation reflowed — the drawer is in flow, not over the content"
    )


def test_the_backdrop_dismisses_it(page, dashboard) -> None:
    """Click the strip of page left of the drawer, not its middle — the drawer
    covers the right of the viewport, which is the point of a drawer."""
    _open_chat(page, dashboard)
    page.locator(TOGGLE).click()
    page.wait_for_selector(DRAWER, timeout=5_000)
    page.wait_for_timeout(400)

    page.locator(BACKDROP).click(position={"x": 16, "y": 200})
    page.wait_for_selector(RAIL, state="detached", timeout=5_000)


def test_escape_dismisses_it(page, dashboard) -> None:
    """It is a dialog — the shared drawer a11y behaviour has to be wired up."""
    _open_chat(page, dashboard)
    page.locator(TOGGLE).click()
    page.wait_for_selector(DRAWER, timeout=5_000)
    page.wait_for_timeout(400)

    page.keyboard.press("Escape")
    page.wait_for_selector(RAIL, state="detached", timeout=5_000)


def test_new_chat_closes_the_drawer(page, dashboard) -> None:
    """Picking a conversation is what the drawer is FOR — it gets out of the way."""
    _open_chat(page, dashboard)
    page.locator(TOGGLE).click()
    page.wait_for_selector(DRAWER, timeout=5_000)
    page.wait_for_timeout(400)

    page.locator(f"{DRAWER} button.primary").click()  # "New chat"
    page.wait_for_selector(RAIL, state="detached", timeout=5_000)


def test_the_drawer_always_leaves_a_strip_to_dismiss_against(page, dashboard) -> None:
    """A panel that covers the whole window is a screen, not a drawer: there is
    nothing left to click to get rid of it. Desktop leaves the conversation
    itself readable beside it; a phone caps the width at 85vw."""
    _open_chat(page, dashboard)
    page.locator(TOGGLE).click()
    page.wait_for_selector(DRAWER, timeout=5_000)
    page.wait_for_timeout(400)

    box = page.locator(DRAWER).bounding_box()
    assert box is not None
    viewport = page.viewport_size["width"]
    if _is_mobile(page):
        assert box["width"] <= viewport * 0.85 + 1, (
            f"phone drawer is {box['width']}px of {viewport}px"
        )
    else:
        # The desktop clamp keeps a usable conversation beside it (CHAT_W_MIN).
        assert viewport - box["width"] >= 400, (
            f"desktop drawer left only {viewport - box['width']}px"
        )
