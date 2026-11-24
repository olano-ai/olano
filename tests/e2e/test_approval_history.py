"""E2E: the Overview Approvals section's history.

The approval record (``<home>/audit/approvals.jsonl``) is the answer to "who
approved that email?", including actions an owner's own WhatsApp messages had
approved without asking. It has to be readable from the dashboard, not only
from the file. Both viewports run.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e


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
def _record(lionclaw_home):
    path = lionclaw_home / "audit" / "approvals.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "event": "requested",
            "id": "r1",
            "agent": "desk",
            "action": "gmail_send",
            "args": {"to": "chen.yi@example.com"},
            "at": "2026-09-22T08:00:00+00:00",
        },
        {
            "event": "decided",
            "id": "r1",
            "agent": "desk",
            "action": "gmail_send",
            "approved": True,
            "decided_by": "whatsapp:6591234567@s.whatsapp.net",
            "via": "whatsapp",
            "note": "",
            "at": "2026-09-22T08:01:00+00:00",
        },
        {
            "event": "requested",
            "id": "r2",
            "agent": "desk",
            "action": "wallet_send",
            "args": {},
            "at": "2026-09-22T09:00:00+00:00",
        },
        {
            "event": "decided",
            "id": "r2",
            "agent": "desk",
            "action": "wallet_send",
            "approved": False,
            "decided_by": "system",
            "via": "timeout",
            "note": "no answer in time",
            "at": "2026-09-22T09:05:00+00:00",
        },
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return lionclaw_home


@pytest.fixture(params=["desktop", "mobile"])
def page(request, dashboard, _record):
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(dashboard.base_url.rstrip("/") + "/overview", wait_until="domcontentloaded")
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def test_history_lists_decisions_with_who_and_how(page, _record) -> None:
    history = page.get_by_test_id("approval-history")
    history.wait_for(state="visible", timeout=20_000)
    history.locator("summary").click()
    history.get_by_text("wallet_send").wait_for(state="visible", timeout=15_000)

    text = history.inner_text()
    assert "approved" in text and "timed out" in text
    assert "by whatsapp:6591234567@s.whatsapp.net" in text
    assert "through no answer in time" in text
    # Newest first.
    assert text.index("wallet_send") < text.index("gmail_send")
    history.screenshot(path=str(_record / f"approval-history-{page.viewport_size['width']}.png"))
