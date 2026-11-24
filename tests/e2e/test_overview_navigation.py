"""E2E: the Overview's Agents tile navigates, and the nav's shipped shape is
one More group under the agents, led by Overview, Insights, Cortex and Issues.

Both are DOM-shape facts an in-process API test can't see: whether the tile is
a real anchor (so keyboard activation and cmd-click work, not just a synthetic
onClick) and where in the sidebar each nav item is rendered.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import MORE_HEADER, SIDEBAR, is_mobile_viewport, open_mobile_nav

pytestmark = pytest.mark.e2e


def _open_nav(page) -> None:
    """Make the sidebar's links reachable at either viewport (on mobile it is a
    closed off-canvas drawer until the hamburger is tapped)."""
    wait_for_app_shell(page)
    if is_mobile_viewport(page):
        open_mobile_nav(page)


def test_agents_stat_tile_is_a_link_to_the_agents_view(page) -> None:
    """Tapping or clicking the Overview "Agents" tile opens /agents."""
    wait_for_app_shell(page)
    page.wait_for_url("**/overview", timeout=15_000)

    tile = page.locator("a.lc-ov-stat-link[href$='/agents']")
    tile.wait_for(state="visible", timeout=15_000)
    # An anchor, not a div with a handler — that is what keeps keyboard
    # activation and open-in-new-tab working.
    assert tile.evaluate("el => el.tagName") == "A"

    tile.click()
    page.wait_for_url("**/agents", timeout=15_000)


def test_every_tab_sits_under_more_and_the_four_lead_it(page) -> None:
    """The shipped nav: there is no always-visible Primary group any more — the
    agents are the top shelf, and every day-to-day tab lives under the
    collapsible More group beneath them, with Overview, Insights, Cortex and
    Issues (the four that used to be Primary) leading it in that order. Agents
    and Agent to Agent are in there too — reachable, just not on top. The Chat
    tab is gone: the bot list above More is the door to a conversation."""
    _open_nav(page)

    sidebar = page.locator(SIDEBAR)
    sidebar.wait_for(state="attached", timeout=15_000)
    assert sidebar.locator(".nav-group-primary").count() == 0
    # Nor a global agent dropdown: the agent list is the selector.
    assert sidebar.locator("[data-testid='agent-picker']").count() == 0

    # More ships collapsed; its links render only while it is open. The
    # collapse state is a server-side pref, so a click that lands before the
    # prefs fetch settles can be overwritten by it — expand with a retry.
    more = page.locator(f"{SIDEBAR} .nav-group-more")
    for _ in range(3):
        header = page.locator(f"{MORE_HEADER}.collapsed")
        if header.count() == 0:
            break
        header.first.click()
        try:
            more.wait_for(state="visible", timeout=4_000)
            break
        except Exception:
            continue
    more.wait_for(state="visible", timeout=10_000)
    hrefs = more.evaluate("el => [...el.querySelectorAll('a')].map(a => a.getAttribute('href'))")
    assert hrefs[:4] == ["/overview", "/insights", "/cortex", "/issues"], hrefs
    assert "/agents" in hrefs and "/activity" in hrefs, hrefs
    assert "/chat" not in hrefs
    # Cortex keeps its violet glow inside More, exactly as it had on top.
    assert "cortex-nav" in (more.locator("a[href='/cortex']").get_attribute("class") or "")
