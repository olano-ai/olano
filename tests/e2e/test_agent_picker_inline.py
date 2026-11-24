"""E2E: Memory, Notes and Knowledge carry the app-wide agent picker in their
own top bar — and it is the only dropdown: the sidebar has no agent picker any
more (its agent list is the selector there).

On a phone the sidebar is a closed drawer, so a page about "this agent's
memory" that only named the agent in the drawer read as agent-less. Each view
hosts the inline form of the picker. The property that matters is that there is
one selection, not two: the page's trigger must name the app-wide agent and
changing it changes the selection everywhere. The picker exposes the selection
as ``data-agent`` (emoji prefixes and truncation make text brittle).

The harness has one agent, so the switch assertion runs only when the roster
offers a second one; the rest runs regardless.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import SIDEBAR

pytestmark = pytest.mark.e2e

INLINE = "main [data-testid='agent-picker'][data-variant='inline']"
INLINE_TRIGGER = f"{INLINE} .agent-picker-trigger"
SIDEBAR_PICKER = f"{SIDEBAR} [data-testid='agent-picker']"


@pytest.mark.parametrize("route", ["/memory", "/notes", "/knowledge"])
def test_the_page_picker_is_the_one_agent_dropdown(page, dashboard, route: str) -> None:
    page.goto(dashboard.base_url.rstrip("/") + route, wait_until="domcontentloaded")
    wait_for_app_shell(page)

    trigger = page.locator(INLINE_TRIGGER)
    trigger.wait_for(state="visible", timeout=15_000)
    selected = trigger.get_attribute("data-agent")
    assert selected, "the page picker must name an agent"
    # The sidebar's dropdown is gone — the agent list took its place. Counted,
    # not checked for visibility: on a phone the sidebar is a closed drawer, and
    # a hidden picker would still be a picker.
    assert page.locator(SIDEBAR_PICKER).count() == 0

    # Open the dropdown: the roster's rows, the selected one marked.
    trigger.click()
    menu = page.locator(f"{INLINE} .agent-menu")
    menu.wait_for(state="visible", timeout=10_000)
    options = menu.locator("[role='option']")
    assert options.count() >= 1
    assert menu.locator("[role='option'][aria-selected='true']").count() == 1

    if options.count() < 2:
        pytest.skip("one agent in the harness roster — nothing to switch to")

    # Pick another agent; the trigger follows, and so does the app-wide
    # selection the sidebar's bot rows highlight on /chat.
    other = menu.locator("[role='option'][aria-selected='false']").first
    other_name = other.locator(".agent-item-name").inner_text().strip()
    other.click()
    page.wait_for_function(
        "([sel, name]) => document.querySelector(sel)?.getAttribute('data-agent') === name",
        arg=[INLINE_TRIGGER, other_name],
        timeout=5_000,
    )
