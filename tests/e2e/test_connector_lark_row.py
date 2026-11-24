"""E2E: Lark is back in the connector picker, as its own row.

Lark is Feishu's international edition on the same adapter (``platform:feishu``
with ``extra.domain: "lark"``). Its picker row was folded into "Feishu / Lark"
to shorten the list, and because the form had no domain field that made Lark
unreachable from the dashboard: every connector saved there was a Feishu one.
The row is back as a *variant* of the Feishu definition. These drive the real
browser through ``/agents/<name>?tab=connectors`` and check the three places a
person meets it: the type dropdown (with the renamed WeChat and WeCom rows
beside it), the preset the Lark row applies to a new connector, and the saved
Lark connector opening as Lark again. Both viewports run, per CLAUDE.md.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

AGENT = "larkrow"
LARK_ROW = "platform:feishu@lark"


@pytest.fixture(scope="module")
def lark_dashboard():
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    try:
        with harness.launch_dashboard(gateway=harness.build_mock_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


@pytest.fixture
def _agent_on_disk(lionclaw_home):
    """An agent already holding a Lark connector (a Feishu one pointed at Lark)."""
    cfg_dir = lionclaw_home / "agents" / AGENT
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": AGENT,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "backend": {"type": "filesystem", "root_dir": "workspace"},
                "channels": [{"type": "platform:feishu", "extra": {"domain": "lark"}}],
            }
        ),
        encoding="utf-8",
    )
    return cfg_dir


@pytest.fixture(params=["desktop", "mobile"])
def page(request, lark_dashboard, _agent_on_disk):
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(
            lark_dashboard.base_url.rstrip("/") + f"/agents/{AGENT}?tab=connectors",
            wait_until="domcontentloaded",
        )
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def _type_select(page):
    """The modal's Type dropdown: the first select in the connector modal."""
    modal = page.locator(".modal-card").first
    modal.wait_for(state="visible", timeout=15_000)
    return modal.locator("select.input").first


def _domain_select(page):
    """The Feishu form's Brand / region field, found by its Lark option."""
    return (
        page.locator(".modal-card select.input")
        .filter(has=page.locator('option[value="lark"]'))
        .first
    )


def test_the_connector_list_names_the_lark_row(page) -> None:
    """A saved Feishu connector pointed at Lark reads "Lark", not "Feishu"."""
    page.get_by_role("tab", name="Connectors").first.wait_for(state="visible", timeout=20_000)
    row = page.locator("#agent-tabpanel strong", has_text="Lark").first
    row.wait_for(state="visible", timeout=20_000)
    assert row.inner_text().strip() == "Lark"


def test_the_picker_offers_lark_and_the_renamed_wechat_rows(page) -> None:
    page.get_by_role("tab", name="Connectors").first.wait_for(state="visible", timeout=20_000)
    page.get_by_role("button", name="Add connector").first.click()
    select = _type_select(page)
    labels = select.locator("option").all_inner_texts()
    values = select.locator("option").evaluate_all("els => els.map(e => e.value)")

    assert LARK_ROW in values
    assert any(label.startswith("Lark (") for label in labels), labels
    assert any(label.startswith("Feishu / Lark") for label in labels), labels
    # The two WeChats, each with the brand word in exactly one label.
    assert any(label.startswith("WeChat (Weixin / iLink Bot") for label in labels), labels
    assert any(label.startswith("WeCom (Enterprise WeChat") for label in labels), labels
    assert sum(label.startswith("WeChat (") for label in labels) == 1


def test_picking_lark_presets_the_domain(page) -> None:
    page.get_by_role("tab", name="Connectors").first.wait_for(state="visible", timeout=20_000)
    page.get_by_role("button", name="Add connector").first.click()
    select = _type_select(page)

    select.select_option(value="platform:feishu")
    domain = _domain_select(page)
    domain.wait_for(state="visible", timeout=10_000)
    # The Feishu row leaves the field on its first option (the backend default).
    assert domain.input_value() in ("", "feishu")

    select.select_option(value=LARK_ROW)
    domain = _domain_select(page)
    domain.wait_for(state="visible", timeout=10_000)
    assert domain.input_value() == "lark"


def test_a_saved_lark_connector_opens_as_lark(page) -> None:
    page.get_by_role("tab", name="Connectors").first.wait_for(state="visible", timeout=20_000)
    edit = page.get_by_role("button", name="Edit connector").first
    edit.wait_for(state="visible", timeout=20_000)
    edit.click()
    select = _type_select(page)
    assert select.input_value() == LARK_ROW
    assert select.is_disabled()
    assert _domain_select(page).input_value() == "lark"
