"""E2E: the agent Overview tab's Gmail watch card.

``gmail_watch`` reads Gmail only through the agent's Google connection, so the
card must say plainly when there is none (and offer the way to connect), show
every field with its explanation, and save what it shows where the runtime
reads it. Both viewports run (the ``page`` fixture is parametrized), per the
dashboard rule in CLAUDE.md.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

AGENT = "desk"


@pytest.fixture(scope="module")
def dashboard():
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    try:
        with harness.launch_dashboard(gateway=harness.build_mock_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


@pytest.fixture
def _agent_on_disk(lionclaw_home):
    cfg_dir = lionclaw_home / "agents" / AGENT
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": AGENT,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "backend": {"type": "filesystem", "root_dir": "workspace"},
            }
        ),
        encoding="utf-8",
    )
    return cfg_dir


@pytest.fixture(params=["desktop", "mobile"])
def page(request, dashboard, _agent_on_disk):
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(
            dashboard.base_url.rstrip("/") + f"/agents/{AGENT}?tab=overview",
            wait_until="domcontentloaded",
        )
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def _card(page):
    card = page.get_by_test_id("gmail-watch-card")
    card.wait_for(state="visible", timeout=20_000)
    return card


def test_card_explains_itself_and_offers_to_connect_google(page) -> None:
    card = _card(page)
    text = card.inner_text()
    assert "Gmail watch" in text
    # The connection the watch depends on, and the way to get one.
    assert "Not connected" in text
    assert card.get_by_role("button", name="Connect Google").is_visible()
    # Every field is on the card with its help.
    for label in (
        "Watch Gmail for new mail",
        "Gmail search",
        "Check every",
        "What to do with new mail",
        "Messages per turn",
    ):
        assert label in text, label
    assert "in:inbox" in text
    assert "Email connector" in text  # the IMAP alternative when Google can't be connected


def test_turning_it_on_without_google_warns_and_saves(page, _agent_on_disk) -> None:
    card = _card(page)
    card.locator("input#gw-query").fill("from:(@broker.example) has:attachment")
    card.locator("textarea#gw-prompt").click()  # blur the search box: it saves
    card.get_by_role("switch", name="Watch Gmail for new mail").click()

    card.get_by_role("alert").wait_for(state="visible", timeout=15_000)
    assert "cannot read Gmail" in card.get_by_role("alert").inner_text()
    card.screenshot(path=str(_agent_on_disk / f"gmail-watch-{page.viewport_size['width']}.png"))

    written = json.loads((_agent_on_disk / "agent.json5").read_text(encoding="utf-8"))
    assert written["gmail_watch"]["enabled"] is True
    assert written["gmail_watch"]["query"] == "from:(@broker.example) has:attachment"


def test_connect_google_opens_connections_for_this_agent(page) -> None:
    _card(page).get_by_role("button", name="Connect Google").click()
    page.wait_for_url("**/connections**", timeout=15_000)
