"""E2E: the agent detail page shows both halves of an agent's identity.

An agent has two names now: `name`, the permanent handle every API path,
thread key and `talk_to_agent` call uses, and `display_name`, the editable
thing it is *called*. The header has to show the face without losing the
handle — a page that renders only the pretty name leaves the reader with no
way to find the agent's folder, and one that renders only the handle makes
the rename pointless.

These run at both viewports because the header is the page's first line and a
two-line identity block is exactly the shape that overflows on a phone.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

NAMED = "merli"
DISPLAY = "Merli the Analyst"
PLAIN = "plainbot"
TITLED = "hillary"
TITLED_DISPLAY = "Hillary"
TITLE = "Recruitment Specialist"


@pytest.fixture(scope="module")
def identity_dashboard():
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    try:
        with harness.launch_dashboard(gateway=harness.build_mock_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


@pytest.fixture
def _agents_on_disk(lionclaw_home):
    """Three agents: a display name, no display name, and a name plus a title."""
    for name, display, title in (
        (NAMED, DISPLAY, ""),
        (PLAIN, "", ""),
        (TITLED, TITLED_DISPLAY, TITLE),
    ):
        cfg_dir = lionclaw_home / "agents" / name
        cfg_dir.mkdir(parents=True, exist_ok=True)
        body = {
            "name": name,
            "display_name": display,
            "model": "anthropic:claude-haiku-4-5-20251001",
            "backend": {"type": "filesystem", "root_dir": "workspace"},
        }
        if title:
            body["org"] = {"title": title}
        (cfg_dir / "agent.json5").write_text(json.dumps(body), encoding="utf-8")
    return lionclaw_home


@pytest.fixture(params=["desktop", "mobile"])
def viewport(request):
    return request.param


@pytest.fixture
def detail_page(identity_dashboard, _agents_on_disk, viewport):
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[viewport])
        page = ctx.new_page()

        def _open(agent: str):
            page.goto(
                f"{identity_dashboard.base_url}/agents/{agent}",
                wait_until="domcontentloaded",
            )
            page.locator("h1").first.wait_for(state="visible", timeout=30_000)
            page.wait_for_timeout(300)
            return page

        try:
            yield _open, viewport
        finally:
            ctx.close()
            browser.close()


def test_header_leads_with_the_display_name(detail_page):
    open_agent, _ = detail_page
    page = open_agent(NAMED)
    assert page.locator("h1").first.inner_text().strip() == DISPLAY


def test_header_still_shows_the_handle(detail_page):
    """Without it the reader cannot address the agent or find its folder."""
    open_agent, _ = detail_page
    page = open_agent(NAMED)
    header = page.locator(".page > .row").first
    assert NAMED in header.inner_text()


def test_header_does_not_repeat_the_handle_when_it_is_the_only_name(detail_page):
    """An agent with no display name gets one line, not the same word twice."""
    open_agent, _ = detail_page
    page = open_agent(PLAIN)
    assert page.locator("h1").first.inner_text().strip() == PLAIN
    header_text = page.locator(".page > .row").first.inner_text()
    assert header_text.count(PLAIN) == 1


def test_header_shows_the_title_under_the_name(detail_page):
    """The third half of the identity: the position, between the name and the
    handle — "Hillary / Recruitment Specialist / hillary"."""
    open_agent, _ = detail_page
    page = open_agent(TITLED)
    assert page.locator("h1").first.inner_text().strip() == TITLED_DISPLAY
    title = page.get_by_test_id("agent-title").first
    title.wait_for(state="visible", timeout=15_000)
    assert title.inner_text().strip() == TITLE
    header = page.locator(".page > .row").first
    assert TITLED in header.inner_text()


def test_header_has_no_title_line_when_there_is_no_title(detail_page):
    open_agent, _ = detail_page
    page = open_agent(NAMED)
    assert page.get_by_test_id("agent-title").count() == 0


def test_identity_header_does_not_overflow_its_column(detail_page):
    """The two-line block is what would push the header off a 390px screen."""
    open_agent, _ = detail_page
    page = open_agent(NAMED)
    header = page.locator(".page > .row").first
    box = header.bounding_box()
    viewport_width = page.viewport_size["width"]
    assert box is not None
    assert box["x"] >= 0
    assert box["x"] + box["width"] <= viewport_width + 1


def test_display_name_field_is_editable_and_persists(detail_page):
    """The whole point of the field: it saves, and the handle does not move."""
    open_agent, _ = detail_page
    page = open_agent(PLAIN)

    field = page.get_by_placeholder(PLAIN).first
    field.wait_for(state="visible", timeout=15_000)
    field.click()
    field.fill("Plain Bot Renamed")
    # The Overview fields auto-save on blur, not on Enter — clicking away is
    # what a user does and what actually fires the PUT.
    field.blur()
    page.wait_for_timeout(900)

    page = open_agent(PLAIN)
    assert page.locator("h1").first.inner_text().strip() == "Plain Bot Renamed"
    # The handle is untouched — the URL that just loaded still resolves.
    assert PLAIN in page.locator(".page > .row").first.inner_text()
