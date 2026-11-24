"""E2E: the Cortex Autonomy panel's two-tier settings table.

Exercises the reworked ``AutonomyPanel`` in a real browser — both tiers on
screen at once ("All agents" vs this agent), the three-state Inherit/On/Off
overrides, the resolved-value readout, and the property that made the panel
wrong before: a per-agent control must stay usable when the fleet-wide default
is off, because escaping that is what an override is for. Runs at both viewports
via the ``page`` fixture.
"""

from __future__ import annotations

import re

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.cortex_ui import open_cortex_settings

pytestmark = pytest.mark.e2e


def _collapse_sidebar(page) -> None:
    """Collapse the desktop nav to a rail so it can't overlay the content and
    intercept clicks. Below the breakpoint the sidebar is an off-canvas drawer
    (closed by default) whose toggle sits inside the hidden drawer — nothing
    overlays the content, so there is nothing to collapse."""
    if page.locator("header.mobile-topbar").is_visible():
        return
    toggle = page.locator("button.sidebar-toggle")
    if toggle.count() and toggle.first.get_attribute("aria-expanded") == "true":
        toggle.first.click()
        page.wait_for_timeout(300)


def _open_cortex(page):
    base = page.url.split("/overview")[0].rstrip("/")
    page.goto(base + "/cortex", wait_until="domcontentloaded", timeout=20_000)
    wait_for_app_shell(page)
    _collapse_sidebar(page)
    # Everything configurable lives in one card that is collapsed by default,
    # so opening it is now part of reaching any of these controls.
    open_cortex_settings(page)


def test_cortex_shows_both_tiers_at_once(page) -> None:
    """Scope is a column, not a mode. The panel used to hide one tier behind a
    segmented control, so the same row meant different things depending on a
    toggle in the header that nothing on the row referred to."""
    _open_cortex(page)

    # Every settings row is present, with its explanation.
    for label in ("Cortex engine", "Autonomous", "Fully autonomous", "Cadence"):
        page.get_by_text(label, exact=True).first.wait_for(state="visible", timeout=10_000)

    # The engine row carries both tiers: a binary default and a three-state
    # override, plus the resolved answer.
    engine_row = page.locator(".cortex-tier-row").first
    engine_row.wait_for(state="visible", timeout=10_000)
    assert engine_row.locator(".seg-group").count() == 2, (
        "expected an All-agents and an agent control"
    )
    page.get_by_role("button", name="Inherit").first.wait_for(state="visible", timeout=8_000)
    # "→ On" / "→ Off" — what this agent actually runs with.
    assert engine_row.locator(".cortex-tier-effective").inner_text().strip().startswith("→")

    # The old scope switch is gone.
    assert page.get_by_role("button", name="All agents (default)").count() == 0


def test_per_agent_controls_stay_usable_when_the_fleet_default_is_off(page) -> None:
    """The reported bug, from the user's side: with the global master off, every
    per-agent control was greyed out — so the one arrangement the three-state
    chip promised ("off for all, on for this one") could not even be attempted."""
    _open_cortex(page)
    engine_row = page.locator(".cortex-tier-row").first
    engine_row.wait_for(state="visible", timeout=10_000)

    # Turn the All-agents default off.
    global_seg = engine_row.locator(".seg-group").first
    global_seg.get_by_role("button", name="Off").click()
    page.wait_for_timeout(900)  # debounce + write

    # The agent column is still live, and can force this agent back on.
    agent_seg = engine_row.locator(".seg-group").nth(1)
    on_btn = agent_seg.get_by_role("button", name="On")
    assert on_btn.is_enabled(), "a fleet-wide off must not disable the per-agent override"
    on_btn.click()
    page.wait_for_timeout(900)

    # The override sticks and the resolved value follows it, not the default.
    assert on_btn.get_attribute("aria-pressed") == "true"
    effective = engine_row.locator(".cortex-tier-effective").inner_text()
    assert re.search(r"On", effective), f"expected the agent to resolve On, got {effective!r}"

    # Reset: clear the override, then put the fleet default back.
    page.get_by_role("button", name="Reset to defaults").click()
    page.wait_for_timeout(900)
    global_seg.get_by_role("button", name="On").click()
    page.wait_for_timeout(900)


def test_toggling_a_knob_does_not_disable_it(page) -> None:
    """Writes are optimistic: the knob moves on click instead of freezing for
    three sequential round trips."""
    _open_cortex(page)
    row = page.locator(".cortex-tier-row").nth(1)  # Autonomous
    row.wait_for(state="visible", timeout=10_000)
    agent_seg = row.locator(".seg-group").nth(1)
    off_btn = agent_seg.get_by_role("button", name="Off")

    off_btn.click()
    # Immediately — no awaiting the network.
    assert off_btn.get_attribute("aria-pressed") == "true"
    assert off_btn.is_enabled(), "an optimistic control must not disable itself mid-write"

    page.wait_for_timeout(900)
    agent_seg.get_by_role("button", name="Inherit").click()
    page.wait_for_timeout(900)
