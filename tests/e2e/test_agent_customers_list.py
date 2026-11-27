"""E2E: the Multi-user tab lists an agent's end users and blocks/unblocks one.

The user guide promised this list long before it existed (it was CLI-only).
Both viewports run (the ``page`` fixture is parametrized), per CLAUDE.md.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import expect, sync_playwright

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
                "customer_service": {"enabled": True},
            }
        ),
        encoding="utf-8",
    )
    customers = cfg_dir / "workspace" / "customers"
    (customers / "telegram-449281").mkdir(parents=True, exist_ok=True)
    (customers / "whatsapp-6591234567").mkdir(parents=True, exist_ok=True)
    return customers


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
            dashboard.base_url.rstrip("/") + f"/agents/{AGENT}?tab=multiuser",
            wait_until="domcontentloaded",
        )
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def test_lists_end_users_and_blocks_one(page, _agent_on_disk) -> None:
    card = page.get_by_test_id("mu-customers")
    card.wait_for(state="visible", timeout=20_000)
    expect(card).to_contain_text("telegram-449281")
    expect(card).to_contain_text("whatsapp-6591234567")
    # The help says what Block does and does not do.
    expect(card).to_contain_text("does NOT stop live chat")

    row_button = card.get_by_role("button", name="Block").first
    row_button.click()
    expect(card.get_by_role("button", name="Unblock")).to_have_count(1)
    prefs = json.loads((_agent_on_disk / "telegram-449281" / ".preferences.json").read_text())
    assert prefs["blocked"] is True

    # Nothing on the card overflows the window at either viewport.
    box = card.bounding_box()
    assert box is not None
    assert box["x"] >= 0 and box["x"] + box["width"] <= page.viewport_size["width"] + 1

    card.get_by_role("button", name="Unblock").click()
    expect(card.get_by_role("button", name="Unblock")).to_have_count(0)
    prefs = json.loads((_agent_on_disk / "telegram-449281" / ".preferences.json").read_text())
    assert prefs["blocked"] is False
