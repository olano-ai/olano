"""E2E: the chat header's dropdowns stay inside the window on a phone.

The header is a chip row that **wraps** on a narrow viewport, so a chip lands
wherever the wrap puts it — and every dropdown in that row was pinned to its
chip with a plain CSS offset, which cannot know where that is:

* the MCP status popover hung from ``right: 0`` at 330px wide, so from a chip in
  the left half of a 390px screen most of the panel sat off the **left** edge
  (reported from a phone: "the menu is outside the window");
* the model and reasoning-effort menus hang from ``left: 0`` at 300px, which is
  the same defect mirrored — off the **right** edge from any chip past the
  middle. Their ``max-width: 80vw`` capped the width and did nothing about the
  position.

They are all measured and clamped now (``ui/AnchoredPopover`` over
``ui/anchoredMenu``), which is what these checks pin: whatever the row's wrap
does, an open panel is fully on screen. Asserted at both viewports by the
``page`` fixture — the desktop case is the regression guard for a fix aimed at
the phone.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e

MODEL_CHIP = ".chat-header .model-picker button.chat-chip"
MENU = ".model-menu"
MCP_CHIP = ".chat-header button.chat-chip:has-text('MCP')"
MCP_POP = ".mcp-pop"

# Enough servers, with long endpoints, that the panel is a real box rather than
# a one-line card — a short popover can fit on screen by accident.
MCP_HEALTH = {
    "ok": True,
    "running": True,
    "overall": "yellow",
    "servers": [
        {
            "url": f"https://mcp.example-{i}.com/very/long/streamable-http/endpoint",
            "transport": "http",
            "status": "connected" if i % 2 else "auth_required",
            "ok": bool(i % 2),
            "tool_count": 3 + i,
            "enabled": True,
        }
        for i in range(6)
    ],
}


def _open_chat(page, dashboard) -> None:
    page.goto(dashboard.base_url.rstrip("/") + "/chat", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    page.wait_for_selector(MODEL_CHIP, timeout=15_000)
    page.wait_for_timeout(300)


def _assert_on_screen(page, selector: str, what: str) -> None:
    """The panel's box lies inside the viewport on every side."""
    box = page.locator(selector).bounding_box()
    view = page.viewport_size
    assert box is not None, f"{what} has no box"
    assert box["width"] > 0 and box["height"] > 0, f"{what} rendered empty"
    assert box["x"] >= 0, f"{what} runs off the left edge (x={box['x']})"
    assert box["x"] + box["width"] <= view["width"] + 1, (
        f"{what} runs off the right edge (right={box['x'] + box['width']}, "
        f"viewport={view['width']})"
    )
    assert box["y"] >= 0, f"{what} runs off the top edge (y={box['y']})"
    assert box["y"] + box["height"] <= view["height"] + 1, (
        f"{what} runs off the bottom edge (bottom={box['y'] + box['height']}, "
        f"viewport={view['height']})"
    )


def test_the_mcp_popover_opens_inside_the_window(page, dashboard) -> None:
    """The reported bug, from the chip's real position in the wrapped row."""
    page.route(
        re.compile(r"/api/agents/[^/]+/mcp/health(\?|$)"),
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(MCP_HEALTH)
        ),
    )
    _open_chat(page, dashboard)

    chip = page.locator(MCP_CHIP).first
    chip.wait_for(state="visible", timeout=15_000)
    chip.click()
    page.locator(MCP_POP).wait_for(state="visible", timeout=10_000)
    page.wait_for_timeout(250)

    _assert_on_screen(page, MCP_POP, "the MCP popover")
    # It is the panel for THIS chip, not a box that happens to fit: the servers
    # are listed and the Setup deep-link is in it.
    pop = page.locator(MCP_POP)
    assert pop.locator("text=mcp.example-0.com").count() == 1
    assert pop.locator("a[href$='/mcp']").count() == 1  # /agents/<agent>/mcp

    # Escape closes it — the shared popover owns dismissal now.
    page.keyboard.press("Escape")
    page.locator(MCP_POP).wait_for(state="detached", timeout=5_000)


def test_the_model_menu_opens_inside_the_window(page, dashboard) -> None:
    """The same defect mirrored: a `left: 0` menu off the right edge."""
    _open_chat(page, dashboard)

    page.locator(MODEL_CHIP).first.click()
    page.locator(MENU).wait_for(state="visible", timeout=10_000)
    page.wait_for_timeout(250)

    _assert_on_screen(page, MENU, "the model menu")
    assert page.locator(f"{MENU} .model-item").count() >= 1, "the menu listed nothing"

    page.keyboard.press("Escape")
    page.locator(MENU).wait_for(state="detached", timeout=5_000)


def test_every_header_dropdown_stays_on_screen(page, dashboard) -> None:
    """Whatever chips this build puts in the row (the effort chip appears only
    for a model whose catalog entry advertises effort levels), each one that
    opens a `.model-menu` opens it fully on screen."""
    _open_chat(page, dashboard)

    chips = page.locator(MODEL_CHIP)
    total = chips.count()
    assert total >= 1, "the header has no dropdown chips at all"
    for i in range(total):
        chips.nth(i).click()
        page.locator(MENU).wait_for(state="visible", timeout=10_000)
        page.wait_for_timeout(200)
        _assert_on_screen(page, MENU, f"header dropdown #{i}")
        page.keyboard.press("Escape")
        page.locator(MENU).wait_for(state="detached", timeout=5_000)
