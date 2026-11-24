"""E2E: the chat header's three actions — ``+``, history, and the agent's card.

Starting a conversation had **no entry point on a phone**. The only "New chat"
button lived inside the thread drawer, behind the header's clock — a
discoverable place to *find* a conversation and not an obvious place to *start*
one — so the most common action on the screen cost two taps and a guess. The
same hole existed on desktop with the rail collapsed.

The header now leads with a filled pair in the theme's own accent: ``+`` starts
a conversation and the history clock opens the list of them, side by side
because both are about *which* conversation, where every chip after them
describes *this* one. It is the same pair the simple shell's chat header leads
with. The clock used to close the row as a quieter grey glyph, indistinguishable
from the status chips beside it; ``+`` used to step aside while the drawer was
open, which only made the clock jump (the drawer's backdrop covers the row
anyway).

The row now closes with the simple shell's **info** button: the agent's profile
card — face, description, edit, the doors into its settings — as a drawer off
the same edge the conversation list arrives from, so the control sits where a
person coming from the simple shell already expects it.

All three are real tap targets; the first two were 28×24px, under the app's own
44px floor for icon buttons and under every touch guideline.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e

TOGGLE = "[data-testid='chat-history-toggle']"
NEW = "[data-testid='chat-new']"
INFO = "[data-testid='chat-profile-toggle']"
RAIL = "aside.chat-rail"
DRAWER = "aside.chat-rail.is-drawer"
PROFILE = "[data-testid='chat-profile-drawer']"
CARD = f"{PROFILE} [data-testid='simple-profile']"
BACKDROP = ".chat-rail-backdrop"
TAP_TARGET_PX = 38


def _is_mobile(page) -> bool:
    return page.locator("header.mobile-topbar").is_visible()


def _open_chat(page, dashboard) -> None:
    page.goto(dashboard.base_url.rstrip("/") + "/chat", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    page.wait_for_selector(TOGGLE, timeout=15_000)
    page.wait_for_timeout(300)


def _accent(page) -> str:
    """The theme's accent as the browser resolves it — coral in dark/light,
    green in noir. Asserted against rather than hardcoded, so a new theme (or a
    retuned one) does not fail this suite for changing a hex value."""
    return page.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()"
    )


def _rgb(page, selector: str, prop: str = "color") -> str:
    return page.evaluate(
        f"() => getComputedStyle(document.querySelector({selector!r})).{prop}",
    )


def _covered(page, selector: str) -> bool:
    """Whether something else (the drawer's backdrop) sits on top of *selector*
    at its centre — the question "can this be clicked right now", asked of the
    browser's hit-testing rather than of Playwright's visibility, which reports
    an element behind a backdrop as visible."""
    return page.evaluate(
        """sel => {
            const el = document.querySelector(sel);
            if (!el) return true;
            const r = el.getBoundingClientRect();
            const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
            return !(hit === el || el.contains(hit));
        }""",
        selector,
    )


def _close_rail(page) -> None:
    """Dismiss the drawer the way the UI offers. Its backdrop covers the header,
    so the clock that opened it is not the way back — the drawer's own ✕ is (so
    are Escape and the backdrop; the other drawer suite covers those paths)."""
    page.locator(".chat-rail-head button").click()
    page.wait_for_timeout(600)


def test_starting_a_conversation_is_always_one_click(page, dashboard) -> None:
    """The header's + is there in either drawer state — never zero (the bug).
    While the drawer is open its own button takes over and the backdrop covers
    the header's, so the two never compete for the same click."""
    _open_chat(page, dashboard)

    # Closed (how every visit starts): the header's + is the one way in.
    assert page.locator(RAIL).count() == 0
    assert page.locator(NEW).is_visible()
    assert not _covered(page, NEW)

    # Open: the drawer's button is the reachable one; the header's + is behind
    # the backdrop, and it is still there, so nothing beside it moves.
    page.locator(TOGGLE).click()
    page.wait_for_selector(DRAWER, timeout=5_000)
    page.wait_for_timeout(600)
    rail_btn = page.locator(f"{RAIL} button.primary")
    assert rail_btn.count() == 1 and rail_btn.first.is_visible()
    assert page.locator(NEW).count() == 1
    assert _covered(page, NEW), "the header + must sit behind the drawer's backdrop"

    # Back where we came from.
    _close_rail(page)
    assert page.locator(RAIL).count() == 0
    assert not _covered(page, NEW)


def test_the_header_plus_starts_a_new_conversation(page, dashboard) -> None:
    """The button has to do the thing, not just be visible."""
    _open_chat(page, dashboard)
    page.locator(NEW).first.wait_for(state="visible", timeout=10_000)

    page.locator(NEW).first.click()
    page.wait_for_timeout(800)
    # A fresh conversation: the empty-state prompt, and no drawer left open.
    page.locator("text=/Start a conversation with/").first.wait_for(state="visible", timeout=10_000)
    assert page.locator(DRAWER).count() == 0


def test_the_actions_wear_the_theme_accent(page, dashboard) -> None:
    """They act; every other chip in the row only reports. The clock used to be
    a grey glyph, indistinguishable from the status chips beside it."""
    _open_chat(page, dashboard)
    accent = _accent(page)
    assert accent, "the theme defines no --accent"

    # `+` and the clock are the filled pair — accent *behind* a white glyph.
    assert _rgb(page, NEW, "backgroundColor") == _to_rgb(page, accent)
    assert _rgb(page, TOGGLE, "backgroundColor") == _rgb(page, NEW, "backgroundColor"), (
        "history must wear the same fill as +"
    )
    assert _rgb(page, f"{TOGGLE} svg") == _rgb(page, f"{NEW} svg"), "history glyph differs from +"
    # The info button is the quieter level: the accent on the glyph, no fill.
    assert _rgb(page, f"{INFO} svg") == _to_rgb(page, accent), "info glyph is not accent"
    assert _rgb(page, INFO, "backgroundColor") != _rgb(page, NEW, "backgroundColor")


def test_history_sits_beside_plus_and_info_closes_the_row(page, dashboard) -> None:
    """The pair leads, adjacent; the card's button is the last thing on the
    row, at the edge its drawer arrives from — where the simple shell keeps
    its own."""
    _open_chat(page, dashboard)
    plus = page.locator(NEW).bounding_box()
    clock = page.locator(TOGGLE).bounding_box()
    info = page.locator(INFO).bounding_box()
    assert plus and clock and info

    # Same line, and the clock starts where + ends (a chip gap, not a chip).
    assert abs(clock["y"] - plus["y"]) < 2, "history is not on the + line"
    assert 0 <= clock["x"] - (plus["x"] + plus["width"]) <= 12, "history is not beside +"

    # Nothing in the header reaches further right than the info button.
    header = page.locator(".chat-header").bounding_box()
    assert header
    right_edges = page.evaluate(
        """() => Array.from(document.querySelectorAll('.chat-header > *'))
                 .map(el => el.getBoundingClientRect().right)"""
    )
    assert info["x"] + info["width"] >= max(right_edges) - 0.5
    assert info["x"] + info["width"] > clock["x"] + clock["width"]


def test_the_info_button_opens_the_agents_profile_card(page, dashboard) -> None:
    """The simple shell's "About this agent" card, as a drawer: who this is,
    with Escape, the backdrop and its own ✕ all the way back."""
    _open_chat(page, dashboard)
    assert page.locator(PROFILE).count() == 0
    assert page.locator(INFO).get_attribute("aria-expanded") == "false"

    page.locator(INFO).click()
    page.locator(CARD).wait_for(state="visible", timeout=10_000)
    page.wait_for_timeout(400)
    assert page.locator(INFO).get_attribute("aria-expanded") == "true"
    card = page.locator(CARD)
    assert card.locator(".simple-profile-head", has_text="About this agent").count() == 1
    assert card.locator(".simple-profile-name", has_text="merli").count() == 1
    # A drawer, not a page: the conversation is still mounted underneath, and
    # the drawer stays inside the window on either viewport.
    assert page.locator(".chat-log").count() == 1
    box = page.locator(PROFILE).bounding_box()
    viewport = page.viewport_size
    assert box is not None
    assert box["x"] + box["width"] <= viewport["width"] + 1
    assert box["x"] >= 0
    # The conversation list is not open at the same time.
    assert page.locator(RAIL).count() == 0

    # Escape closes it and the button says so.
    page.keyboard.press("Escape")
    page.locator(PROFILE).wait_for(state="detached", timeout=5_000)
    assert page.locator(INFO).get_attribute("aria-expanded") == "false"

    # So does the card's own ✕.
    page.locator(INFO).click()
    page.locator(CARD).wait_for(state="visible", timeout=10_000)
    page.locator(f"{CARD} .simple-profile-head button").click()
    page.locator(PROFILE).wait_for(state="detached", timeout=5_000)

    # And the backdrop, tapped beside the drawer.
    page.locator(INFO).click()
    page.locator(CARD).wait_for(state="visible", timeout=10_000)
    page.wait_for_timeout(400)
    page.locator(BACKDROP).click(position={"x": 10, "y": 10})
    page.locator(PROFILE).wait_for(state="detached", timeout=5_000)


def _to_rgb(page, css_color: str) -> str:
    """Resolve a token value ("#ff6a3d") to the `rgb(...)` form getComputedStyle
    returns, so the comparison is against the theme rather than a literal."""
    return page.evaluate(
        """color => {
            const el = document.createElement('span');
            el.style.color = color;
            document.body.appendChild(el);
            const out = getComputedStyle(el).color;
            el.remove();
            return out;
        }""",
        css_color,
    )


def test_the_actions_are_thumb_sized_on_a_phone(page, dashboard) -> None:
    """The rest of the header is a dense status strip and stays small; these
    three are what the thumb actually goes for."""
    _open_chat(page, dashboard)
    if not _is_mobile(page):
        pytest.skip("tap-target floor applies to the touch viewport")

    for sel in (TOGGLE, NEW, INFO):
        box = page.locator(sel).first.bounding_box()
        assert box is not None, f"{sel} has no layout box"
        assert box["width"] >= TAP_TARGET_PX and box["height"] >= TAP_TARGET_PX, (
            f"{sel} is {box['width']}×{box['height']} — under the {TAP_TARGET_PX}px floor"
        )
