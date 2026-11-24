"""E2E: the "N offline" fold in both shells' agent lists.

A fleet with many stopped agents used to list every one of them under All
agents, between the agents that could answer. Now All agents draws the agents
that can answer (or need someone to look: a failed boot, a deployment block)
and folds the paused and not-running ones behind one "N offline" row, closed by
default and remembered on the device. Pinned agents and the reader's own groups
are never folded, a section with nothing awake draws its rows flat, and the
simple rail's search ignores the fold. The rules live in
``views/agents/presence.ts``; this proves both shells draw them.

The roster is the real ``/api/agents`` answer with its rows replaced by a
crafted fleet, so every state is on screen at once without booting six agents.
The test never opens a fake agent's chat, which would ask the server about an
agent it does not have.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import (
    SIMPLE_ROOT,
    is_mobile_viewport,
    open_mobile_nav,
    open_simple_rail,
)

pytestmark = pytest.mark.e2e

BOTS = "[data-testid='nav-bot-sections']"
NAV_ALL = f"{BOTS} .bot-section[data-section='all']"
NAV_PINNED = f"{BOTS} .bot-section[data-section='pinned']"
NAV_TOGGLE = "[data-testid='nav-offline-toggle']"
SIMPLE_TOGGLE = "[data-testid='simple-offline-toggle']"


def _agent(name: str, **state) -> dict:
    row = {
        "name": name,
        "display_name": name,
        "title": "",
        "emoji": "",
        "model": "fake:instant",
        "channels": [],
        "description": f"{name} helps with things",
        "running": False,
        "paused": False,
        "status": "configured",
        "error": "",
        "blocked": False,
        "blocked_reason": "",
    }
    row.update(state)
    return row


# merli first: an unknown remembered agent falls back to the roster's first
# row, and merli is the one agent the server really has.
FLEET = [
    _agent("ada", running=True, status="running"),
    _agent("bea", running=True, paused=True, status="running"),
    _agent("cyd"),
    _agent("dex", status="error", error="no API key for the model"),
    _agent("eve", status="blocked", blocked=True, blocked_reason="over the agent cap"),
]


def _use_roster(page, fleet: list[dict]) -> None:
    def handle(route):
        resp = route.fetch()
        real = [a for a in resp.json() if a.get("name") == "merli"]
        route.fulfill(response=resp, json=real + fleet)

    page.route(re.compile(r".*/api/agents$"), handle)


def _seed_storage(page, key: str, value) -> None:
    """Set one localStorage key before the app reads it (every navigation)."""
    page.add_init_script(
        f"try {{ localStorage.setItem({json.dumps(key)}, {json.dumps(json.dumps(value))}); }} catch (e) {{}}"
    )


def _set_ui_mode(value: str) -> None:
    from olano.config.home import ensure_olano_home, update_home_config

    ensure_olano_home()
    update_home_config({"ui_mode": value})


# ── dashboard sidebar (views/nav/BotNav.tsx) ─────────────────────────────────


def _open_nav(page, dashboard) -> None:
    page.goto(dashboard.base_url.rstrip("/") + "/")
    wait_for_app_shell(page)
    if is_mobile_viewport(page):
        open_mobile_nav(page)
    page.locator(f"{NAV_ALL} .bot-row-wrap[data-presence]", has_text="ada").wait_for(
        state="visible", timeout=10_000
    )


def _nav_names(page, scope: str) -> list[str]:
    return page.locator(f"{scope} .bot-row-name").all_inner_texts()


def test_dashboard_folds_offline_agents_and_remembers_the_choice(page, dashboard) -> None:
    _use_roster(page, FLEET)
    _open_nav(page, dashboard)

    # Awake and attention-needing agents show; the paused and stopped ones do
    # not, and the toggle says how many are tucked away.
    assert _nav_names(page, NAV_ALL) == ["ada", "dex", "eve", "merli"]
    toggle = page.locator(NAV_TOGGLE)
    assert toggle.get_attribute("aria-expanded") == "false"
    assert "2 offline" in toggle.inner_text()

    # A failed boot is red and says why; a block is amber and says why.
    dex = page.locator(f"{NAV_ALL} .bot-row-wrap", has_text="dex")
    assert dex.get_attribute("data-presence") == "error"
    assert dex.locator(".botmark-pip.is-error").count() == 1
    assert "no API key for the model" in (dex.locator(".bot-row").get_attribute("title") or "")
    eve = page.locator(f"{NAV_ALL} .bot-row-wrap", has_text="eve")
    assert eve.locator(".botmark-pip.is-warn").count() == 1
    assert "over the agent cap" in (eve.locator(".bot-row").get_attribute("title") or "")

    # Open the tail: the sleepers appear after the awake band, dimmed, and say
    # what they are.
    toggle.click()
    assert toggle.get_attribute("aria-expanded") == "true"
    assert _nav_names(page, NAV_ALL) == ["ada", "dex", "eve", "merli", "bea", "cyd"]
    bea = page.locator(f"{NAV_ALL} .bot-row-wrap", has_text="bea")
    assert "is-asleep" in (bea.get_attribute("class") or "")
    assert "Paused" in (bea.locator(".bot-row").get_attribute("title") or "")
    cyd = page.locator(f"{NAV_ALL} .bot-row-wrap", has_text="cyd")
    assert "Offline" in (cyd.locator(".bot-row").get_attribute("title") or "")
    opacity = float(cyd.locator(".botmark").evaluate("el => getComputedStyle(el).opacity"))
    assert opacity < 0.7, opacity

    # Remembered on this device.
    page.reload()
    _open_nav(page, dashboard)
    assert page.locator(NAV_TOGGLE).get_attribute("aria-expanded") == "true"
    assert "bea" in _nav_names(page, NAV_ALL)


def test_dashboard_never_folds_pinned_agents_or_an_all_asleep_section(page, dashboard) -> None:
    # bea is pinned while paused; every agent left under All agents is asleep.
    _seed_storage(
        page,
        "lc.nav.botArrangement",
        {"pins": ["merli", "bea"], "order": [], "groups": [], "assign": {}, "folded": []},
    )
    _use_roster(page, [_agent("bea", paused=True, status="paused"), _agent("cyd"), _agent("zed")])
    page.goto(dashboard.base_url.rstrip("/") + "/")
    wait_for_app_shell(page)
    if is_mobile_viewport(page):
        open_mobile_nav(page)
    page.locator(f"{NAV_ALL} .bot-row-name", has_text="cyd").wait_for(
        state="visible", timeout=10_000
    )

    assert _nav_names(page, NAV_PINNED) == ["merli", "bea"]
    # Nothing awake to sit above a fold, so the rows are drawn flat and there
    # is no toggle that would toggle nothing.
    assert _nav_names(page, NAV_ALL) == ["cyd", "zed"]
    assert page.locator(NAV_TOGGLE).count() == 0


# ── simple rail (views/simple/BotRail.tsx) ───────────────────────────────────


def _open_simple(page, dashboard) -> None:
    page.goto(dashboard.base_url.rstrip("/") + "/")
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)
    open_simple_rail(page)
    page.locator(f"{SIMPLE_ROOT} .simple-bot", has_text="ada").first.wait_for(
        state="visible", timeout=10_000
    )


def _simple_names(page) -> list[str]:
    return page.locator(f"{SIMPLE_ROOT} .simple-bot-list .simple-row-name").all_inner_texts()


def test_simple_rail_folds_offline_agents_and_search_ignores_the_fold(page, dashboard) -> None:
    _set_ui_mode("simple")
    try:
        _use_roster(page, FLEET)
        _open_simple(page, dashboard)

        assert _simple_names(page) == ["ada", "dex", "eve", "merli"]
        toggle = page.locator(SIMPLE_TOGGLE)
        assert toggle.get_attribute("aria-expanded") == "false"
        assert "2 offline" in toggle.inner_text()

        # A phone has no hover, so a red pip explains itself on the row's own
        # muted line, not only on a tooltip.
        dex = page.locator(f"{SIMPLE_ROOT} .simple-bot", has_text="dex").first
        assert dex.get_attribute("data-presence") == "error"
        assert "Failed to start" in dex.locator(".simple-row-sub").inner_text()
        assert dex.locator(".botmark-pip.is-error").count() == 1

        toggle.click()
        assert _simple_names(page) == ["ada", "dex", "eve", "merli", "bea", "cyd"]
        bea = page.locator(f"{SIMPLE_ROOT} .simple-bot", has_text="bea").first
        assert "is-asleep" in (bea.get_attribute("class") or "")

        # Close it again, then search: a match behind the fold is still found,
        # and the toggle steps aside while the filter is on.
        toggle.click()
        assert "bea" not in _simple_names(page)
        page.locator(f"{SIMPLE_ROOT} .simple-search-input").fill("bea")
        page.locator(f"{SIMPLE_ROOT} .simple-row-name", has_text="bea").wait_for(
            state="visible", timeout=5_000
        )
        assert _simple_names(page) == ["bea"]
        assert page.locator(SIMPLE_TOGGLE).count() == 0
    finally:
        _set_ui_mode("advanced")
