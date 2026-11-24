"""The agent Overview tab fits the viewport at both breakpoints.

`tests/e2e/route_checks.py` proves the Overview tab's config surface renders
(its per-option explanation lists). This is the other half of the standing
both-viewport rule for that page: nothing on it may make the document pan
sideways, which is what a card or a long select label does on a 390px phone.
The `page` fixture is parametrized over the viewports, so this runs at both.
"""

from __future__ import annotations

from tests.e2e import harness
from tests.e2e.conftest import wait_for_app_shell

AGENT = "merli"  # seeded into every test home by harness.seed_agent_configs


def test_overview_tab_has_no_horizontal_overflow(page, dashboard: harness.RunningDashboard) -> None:
    page.goto(
        dashboard.base_url.rstrip("/") + f"/agents/{AGENT}?tab=overview",
        wait_until="domcontentloaded",
    )
    wait_for_app_shell(page)
    # The Mode explanation list is the last thing the Oversight card draws, so
    # once it is visible the whole config surface has laid out.
    page.locator("li:has-text('Asks only before moving money')").wait_for(
        state="visible", timeout=30_000
    )
    page.wait_for_timeout(300)  # let any late-mounting card settle

    overflow = page.evaluate(
        "() => ({ scroll: document.documentElement.scrollWidth,"
        " client: document.documentElement.clientWidth,"
        " body: document.body.scrollWidth })"
    )
    assert overflow["scroll"] <= overflow["client"] + 1, overflow
    assert overflow["body"] <= overflow["client"] + 1, overflow

    # And the card itself sits inside the window, not merely clipped by it.
    width = page.evaluate("() => window.innerWidth")
    card = page.locator("li:has-text('Asks only before moving money')").bounding_box()
    assert card is not None, "the Mode note has no layout box"
    assert card["x"] >= 0, card
    assert card["x"] + card["width"] <= width + 1, card
