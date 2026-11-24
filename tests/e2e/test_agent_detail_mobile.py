"""E2E: the agent detail page is usable on a phone.

The MCP tab was the worst offender in the app at 390px. Its server card was one
flex row — ``[icon][identity][six actions]`` — and the action cluster's
min-content width squeezed the identity column down to a few pixels, where
``word-break: break-all`` wrapped the endpoint URL **one character per line**
into a ~600px vertical ribbon. Around it: the status chips used a ``.pill``
class that was never defined in CSS (so the icon and the label broke onto
separate lines behind a ragged inline background), the 14-item tab bar wrapped
into a four-row block taller than the content it introduced, the section head
starved "MCP servers" down to its narrowest word, and every icon action was a
~22×24px tap target.

These tests pin the properties that make the page usable, at both viewports —
the desktop layout must not regress into the phone one either.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

AGENT = "merli"
TAP_TARGET_PX = 44  # the usual floor for a touch target
# The endpoint the phone screenshots died on. Long enough that a collapsed
# identity column is unmistakable in a bounding box.
MCP_URL = "https://mcp.notion.com/mcp"


@pytest.fixture(scope="module")
def detail_dashboard():
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    try:
        with harness.launch_dashboard(gateway=harness.build_mock_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


@pytest.fixture
def _agent_on_disk(lionclaw_home):
    """An agent carrying the MCP server whose URL used to wrap per character."""
    cfg_dir = lionclaw_home / "agents" / AGENT
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": AGENT,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "backend": {"type": "filesystem", "root_dir": "workspace"},
                "mcp_servers": [{"url": MCP_URL, "transport": "streamable_http", "auth": "oauth"}],
            }
        ),
        encoding="utf-8",
    )
    return cfg_dir


@pytest.fixture(params=["desktop", "mobile"])
def viewport(request):
    return request.param


@pytest.fixture
def mcp_page(detail_dashboard, _agent_on_disk, viewport):
    """The agent detail page on its MCP tab, sized for ``viewport``."""
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[viewport])
        page = ctx.new_page()
        try:
            page.goto(
                f"{detail_dashboard.base_url}/agents/{AGENT}?tab=mcp",
                wait_until="domcontentloaded",
            )
            page.locator(".mcp-card").first.wait_for(state="visible", timeout=30_000)
            page.wait_for_timeout(400)
            yield page, viewport
        finally:
            ctx.close()
            browser.close()


def _box(locator):
    box = locator.bounding_box()
    assert box is not None, "element has no layout box"
    return box


def test_the_endpoint_url_reads_as_a_line_not_a_column(mcp_page) -> None:
    """The regression that started this: a starved identity column.

    A URL box taller than it is wide is the character-per-line ribbon. Assert
    the shape directly rather than the CSS that produces it — a future layout
    can change how the column gets its width, but not that it must have one.
    """
    page, _ = mcp_page
    url = page.locator(".mcp-card-url").first
    box = _box(url)
    assert box["width"] > 150, f"identity column collapsed to {box['width']}px"
    assert box["height"] < box["width"], (
        f"URL is {box['width']}×{box['height']} — taller than wide means it is "
        "wrapping per character again"
    )


def test_the_status_chips_are_chips(mcp_page) -> None:
    """`.pill` was used but never defined, so the icon and the label stacked.

    Assert the rendered shape, not the declaration: a pill is a flex *item*
    here, so ``inline-flex`` blockifies to ``flex`` in the computed style and
    proves nothing either way.
    """
    page, _ = mcp_page
    pill = page.locator(".mcp-card-pills .pill").first
    pill.wait_for(state="visible", timeout=10_000)
    box = _box(pill)
    # One line: an icon (13px) beside a 12px label plus padding. The stacked
    # icon-over-label of an undefined class cannot fit under 32px.
    assert box["height"] < 32, f"chip is {box['height']}px tall — icon and label stacked"
    assert box["width"] > box["height"] * 2, "chip is not laid out as a horizontal chip"
    # The rounding is the cheapest proof the class resolved at all.
    radius = page.evaluate("el => getComputedStyle(el).borderRadius", pill.element_handle())
    assert radius.startswith("999px"), f"pill has no pill rounding ({radius})"


def test_the_tab_rail_is_one_row_on_a_phone(mcp_page) -> None:
    """14 tabs wrapped into a four-row block before any content."""
    page, viewport = mcp_page
    strip = page.locator(".tabstrip").first
    tab_h = _box(page.locator(".tabstrip-tab").first)["height"]
    if viewport == "mobile":
        assert _box(strip)["height"] < tab_h * 2, "tab rail wrapped instead of scrolling"
        assert page.evaluate("el => el.scrollWidth > el.clientWidth", strip.element_handle()), (
            "the rail must scroll — otherwise the later tabs are unreachable"
        )
    else:
        # Desktop keeps wrapping; nothing may scroll off-screen there.
        assert not page.evaluate(
            "el => el.scrollWidth > el.clientWidth + 1", strip.element_handle()
        )


def test_the_tab_rail_is_a_real_tablist(mcp_page) -> None:
    """Declaring role=tablist promises arrow-key navigation; keep the promise."""
    page, _ = mcp_page
    tabs = page.get_by_role("tab")
    assert tabs.count() > 1
    selected = page.locator('.tabstrip-tab[aria-selected="true"]')
    assert selected.count() == 1
    # Roving tabindex: exactly the selected tab is in the page's tab order.
    assert selected.first.get_attribute("tabindex") == "0"
    assert page.locator('.tabstrip-tab[aria-selected="false"][tabindex="0"]').count() == 0
    # Arrow keys move the selection (MCP → External access, the next tab along:
    # the inbound-access tab sits right after the MCP tab it is so easily
    # confused with, so the two read as neighbours).
    selected.first.focus()
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(300)
    assert (
        page.locator('.tabstrip-tab[aria-selected="true"]')
        .first.inner_text()
        .startswith("External access")
    )


def test_the_section_head_keeps_its_title_intact(mcp_page) -> None:
    """The title was squeezed to its narrowest word by the button cluster."""
    page, viewport = mcp_page
    title = page.locator(".section-head-title").first
    box = _box(title)
    # Two stacked words ("MCP" over "servers") doubles the height of one line.
    assert box["height"] < 30, f"section title wrapped ({box['height']}px tall)"
    if viewport == "mobile":
        # Actions drop below the title rather than competing with it for width.
        actions = _box(page.locator(".section-head-actions").first)
        assert actions["y"] >= box["y"] + box["height"] - 1


def test_every_card_action_is_thumb_sized_on_a_phone(mcp_page) -> None:
    """Icon actions were ~22×24px — half the floor every touch guideline sets."""
    page, viewport = mcp_page
    if viewport != "mobile":
        pytest.skip("tap-target floor applies to the touch viewport")
    buttons = page.locator(".mcp-card-actions .btn")
    assert buttons.count() >= 3
    for i in range(buttons.count()):
        box = _box(buttons.nth(i))
        assert box["height"] >= TAP_TARGET_PX, (
            f"action {i} is {box['width']}×{box['height']} — under the {TAP_TARGET_PX}px tap target"
        )


def test_nothing_on_the_page_overflows_the_viewport(mcp_page) -> None:
    """A card wider than the window is what made the whole page pan sideways."""
    page, _ = mcp_page
    width = page.evaluate("() => window.innerWidth")
    card = _box(page.locator(".mcp-card").first)
    assert card["x"] >= 0
    assert card["x"] + card["width"] <= width + 1
