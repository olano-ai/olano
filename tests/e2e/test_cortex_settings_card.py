"""E2E: everything configurable about Cortex lives in one collapsed card.

The page used to open on two always-expanded peer cards — "Autonomous Cortex"
(a six-row two-tier table plus an eleven-row phase list) and "Customize your
Cortex Engine". Both headings promise engine settings, so finding a knob meant
opening both and comparing; and on a phone the pair pushed the tabs the page
exists for (Pulse, Ideas, Projects…) three screens below the fold, on every
visit, whether or not you came to change anything.

One card now — **Cortex settings** — collapsed by default, with three parallel
sub-sections inside: *How it runs*, *Phases & schedule*, *Creative direction*.
These pin the properties that make that an improvement rather than a rename:
the content is reachable without scrolling past the settings, the card's live
state is readable without opening it, and the poll behind that state (and
behind the over-budget confirmations elsewhere in the view) keeps running while
the card is shut.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.cortex_ui import SETTINGS_HEAD, SETTINGS_SUMMARY, open_cortex_settings

pytestmark = pytest.mark.e2e

SUBSECTIONS = ("How it runs", "Phases & schedule", "Creative direction")


def _open_cortex(page):
    base = page.url.split("/overview")[0].rstrip("/")
    page.goto(base + "/cortex", wait_until="domcontentloaded", timeout=20_000)
    wait_for_app_shell(page)
    page.locator(SETTINGS_HEAD).first.wait_for(state="visible", timeout=15_000)
    page.wait_for_timeout(400)


def test_the_settings_start_collapsed_and_the_content_is_above_the_fold(page) -> None:
    """The reason to collapse them: the page is for the items, not the knobs."""
    _open_cortex(page)
    assert page.locator(".cortex-tier-row").count() > 0, "the table must be in the DOM"
    assert not page.locator(".cortex-tier-row").first.is_visible(), (
        "the settings card must start collapsed"
    )

    # The tab strip — what the page is actually for — is on the first screen.
    strip = page.locator(".cortex-tabstrip")
    strip.wait_for(state="visible", timeout=10_000)
    box = strip.bounding_box()
    assert box is not None
    assert box["y"] < page.viewport_size["height"], (
        f"the tabs start {box['y']}px down — still below the fold"
    )


def test_the_card_says_what_the_engine_is_doing_without_opening_it(page) -> None:
    """A collapsed settings card that says nothing is just a hidden card. The
    summary is the resolved state for this agent, from the backend's own gates."""
    _open_cortex(page)
    summary = page.locator(SETTINGS_SUMMARY).first
    summary.wait_for(state="visible", timeout=15_000)
    text = summary.inner_text().strip()
    assert text.startswith(("On", "Off")), f"summary does not lead with the engine state: {text!r}"


def test_one_card_holds_all_three_parts(page) -> None:
    """The consolidation itself: three named parts, one card, no peer card left
    outside it promising the same thing."""
    _open_cortex(page)
    open_cortex_settings(page)
    card = page.locator("section.cortex-settings")
    assert card.count() == 1
    for name in SUBSECTIONS:
        assert card.locator(f".cortex-subsection-toggle:has-text('{name}')").count() == 1, (
            f"{name!r} is not a sub-section of the settings card"
        )
    # Nothing else on the page still offers Cortex settings of its own.
    assert page.get_by_text("Customize your Cortex Engine", exact=True).count() == 0


def test_the_status_poll_survives_a_collapsed_card(page) -> None:
    """The card hides its body rather than unmounting it: AutonomyPanel owns the
    /api/cognition/status poll that feeds this summary, the "Think now" button's
    live state and the over-budget confirmations. Unmounting it would take all
    three away for anyone who never opens the card."""
    _open_cortex(page)
    page.locator(SETTINGS_SUMMARY).first.wait_for(state="visible", timeout=15_000)

    calls: list[str] = []
    page.on("request", lambda r: calls.append(r.url) if "/api/cognition/status" in r.url else None)
    # The idle poll is 30s; the panel also catches up on `visibilitychange`, so
    # fire that rather than sitting out a full interval.
    page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(2_500)
    assert calls, "the collapsed card stopped polling cognition status"
