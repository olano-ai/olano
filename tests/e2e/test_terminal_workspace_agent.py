"""E2E: the Terminal's per-agent model — a workspace-agent header, one tab set
per agent, and a legal "no tab" state.

A terminal belongs to one agent (its shell runs in that agent's workspace or
sandbox), so the view now says whose shells it is showing and the strip shows
only that agent's tabs. What a browser can prove about it:

* the header names the selected agent (``data-agent`` on the inline picker) and
  the posture hint reads "Host shell" on this open-posture harness;
* the picker's row for the agent carries the count of its open tabs;
* closing the LAST tab leaves an "Open terminal" empty state instead of silently
  respawning a shell, and that button opens one that connects.

Agent switching is not asserted: the harness roster has one agent. The rest of
the terminal's behaviour (recall, key bar) is covered by
``test_terminal_input_aids.py``, whose selectors this change kept.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e

AGENT = "merli"
HEADER = "[data-testid='term-header']"
PICKER_TRIGGER = (
    f"{HEADER} [data-testid='agent-picker'][data-variant='inline'] .agent-picker-trigger"
)
POSTURE = "[data-testid='term-posture']"
OPEN_DOT = ".term-tab.active .term-dot.open"
EMPTY = "[data-testid='term-empty']"


def _open_terminal(page, dashboard) -> None:
    page.goto(dashboard.base_url.rstrip("/") + "/terminal", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    if page.locator("text=Terminal is disabled").count():
        pytest.skip("terminal disabled on this deployment")
    page.wait_for_selector(".term-mount", timeout=20_000)
    page.wait_for_selector(OPEN_DOT, timeout=20_000)


def test_header_names_the_workspace_agent_and_where_its_shell_runs(page, dashboard) -> None:
    _open_terminal(page, dashboard)
    trigger = page.locator(PICKER_TRIGGER)
    trigger.wait_for(state="visible", timeout=10_000)
    assert trigger.get_attribute("data-agent") == AGENT
    posture = page.locator(POSTURE)
    posture.wait_for(state="visible", timeout=10_000)
    assert "Host shell" in posture.inner_text()
    assert AGENT in posture.inner_text()

    # The strip is that agent's, and the picker says how many shells it has.
    assert page.locator(".term-tabs").get_attribute("aria-label") == f"Terminals for {AGENT}"
    trigger.click()
    row = page.locator(f"{HEADER} .agent-menu [role='option'][aria-selected='true']")
    row.wait_for(state="visible", timeout=10_000)
    assert "1" in row.locator(".term-agent-count").inner_text()
    page.keyboard.press("Escape")


def test_closing_the_last_tab_leaves_an_open_terminal_button_not_a_respawn(page, dashboard) -> None:
    _open_terminal(page, dashboard)
    tabs = page.locator(".term-tab")
    assert tabs.count() == 1
    # Every tab has its close button now — including the last one.
    page.locator(".term-tab.active .term-tab-x").click()
    page.locator(EMPTY).wait_for(state="visible", timeout=10_000)
    assert page.locator(".term-tab").count() == 0, "no respawn: the agent simply has no tab"

    page.locator(f"{EMPTY} button").click()
    page.wait_for_selector(OPEN_DOT, timeout=20_000)
    assert page.locator(".term-tab").count() == 1
