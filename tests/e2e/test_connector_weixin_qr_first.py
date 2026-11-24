"""E2E: the WeChat (weixin) connector form is QR-first.

WeChat has no developer console: Tencent mints the credentials when a personal
account scans the iLink QR, so the **Link / Connect** pairing card is the whole
happy path. The bot-id/token fields exist only as a manual fallback for
multi-account servers — leading with them (as the form used to) sent users
hunting for a token that does not exist before a scan.

Mirrors ``test_connector_whatsapp_listening.py``'s collapsed-transport test:
drives the real browser through ``/agents/<name>?tab=connectors``; both
viewports run (the ``page`` fixture is parametrized), per the dashboard rule in
CLAUDE.md.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

AGENT = "wxfirst"


@pytest.fixture(scope="module")
def weixin_dashboard():
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    try:
        with harness.launch_dashboard(gateway=harness.build_mock_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


@pytest.fixture
def _agent_on_disk(lionclaw_home):
    """An agent with a weixin connector and no credential overrides set."""
    cfg_dir = lionclaw_home / "agents" / AGENT
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": AGENT,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "backend": {"type": "filesystem", "root_dir": "workspace"},
                "channels": [{"type": "platform:weixin", "extra": {}}],
            }
        ),
        encoding="utf-8",
    )
    return cfg_dir


@pytest.fixture(params=["desktop", "mobile"])
def page(request, weixin_dashboard, _agent_on_disk):
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(
            weixin_dashboard.base_url.rstrip("/") + f"/agents/{AGENT}?tab=connectors",
            wait_until="domcontentloaded",
        )
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def _open_connector_editor(page) -> None:
    page.get_by_role("tab", name="Connectors").first.wait_for(state="visible", timeout=20_000)
    edit = page.get_by_role("button", name="Edit connector").first
    edit.wait_for(state="visible", timeout=20_000)
    edit.click()
    page.locator(".connector-shell").first.wait_for(state="visible", timeout=15_000)


def test_qr_pairing_card_leads_the_form(page) -> None:
    """The Link/Connect pairing card is the first thing the form offers."""
    _open_connector_editor(page)

    card = page.locator(".connector-live-card").first
    card.wait_for(state="visible", timeout=15_000)
    assert "WeChat" in card.inner_text()
    # The card carries a real primary action, not just prose.
    assert card.locator("button").count() >= 1


def test_credential_fields_are_collapsed_because_pairing_is_by_qr(page) -> None:
    """bot_id/app_token are a manual fallback — collapsed below, like WhatsApp's
    bridge overrides. (They auto-open when the agent has one set, so an
    existing pin is never hidden behind a chevron.)"""
    _open_connector_editor(page)

    section = page.locator("details.connector-fieldset").last
    section.wait_for(state="attached", timeout=15_000)
    # Nothing set on this agent, so it stays shut.
    assert section.evaluate("el => el.hasAttribute('open')") is False
