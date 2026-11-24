"""E2E: the WhatsApp (Twilio) connector's panel lists its tools, with switches.

The connector brings twelve tools, and the agent's Tools tab lists them among
hundreds of others. The connector's own panel shows exactly those, split into
the everyday ones (on by default for every agent with the connector) and
account administration (off until an administrator switches them on), and the
connector's Save writes the switches into the agent's config: an everyday tool
switched off lands in ``disabled_tools``, the only way to drop a tool the
platform grants by itself, and an admin tool switched on lands in ``tools``.

Drives the real browser through ``/agents/<name>?tab=connectors``; both
viewports run (the ``page`` fixture is parametrized), per the dashboard rule in
CLAUDE.md.
"""

from __future__ import annotations

import json
import time

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

AGENT = "wabot"
EVERYDAY = [
    "whatsapp_template_list",
    "whatsapp_template_send",
    "whatsapp_message_status",
    "whatsapp_window_status",
]
ADMIN = [
    "whatsapp_template_create",
    "whatsapp_template_submit",
    "whatsapp_template_status",
    "whatsapp_template_delete",
    "whatsapp_sender_status",
    "whatsapp_profile_update",
    "twilio_account_balance",
    "twilio_usage_summary",
]


@pytest.fixture(scope="module")
def wa_dashboard():
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
                "tools": ["web_search"],
                "channels": [
                    {
                        "type": "platform:whatsapp_twilio",
                        "extra": {
                            "account_sid": "AC" + "0" * 32,
                            "from_number": "+14155238886",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return cfg_dir


@pytest.fixture(params=["desktop", "mobile"])
def page(request, wa_dashboard, _agent_on_disk):
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(
            wa_dashboard.base_url.rstrip("/") + f"/agents/{AGENT}?tab=connectors",
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


def _switch(page, name: str):
    return page.locator(f'[data-connector-tool="{name}"] [role="switch"]').first


def _wait_enabled(page, name: str) -> None:
    """The switches are disabled until the agent's current state has loaded."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _switch(page, name).is_enabled():
            return
        page.wait_for_timeout(200)
    raise AssertionError(f"the {name} switch never became enabled")


def test_the_panel_lists_the_toolkit_with_the_agents_switches(page) -> None:
    _open_connector_editor(page)
    section = page.locator("[data-connector-tools]").first
    section.wait_for(state="visible", timeout=15_000)
    section.scroll_into_view_if_needed()

    names = [
        row.get_attribute("data-connector-tool")
        for row in section.locator("[data-connector-tool]").all()
    ]
    assert names == EVERYDAY + ADMIN, "everyday tools first, account administration below"
    text = section.inner_text()
    assert "Everyday tools" in text and "Account administration" in text

    _wait_enabled(page, "whatsapp_template_send")
    for name in EVERYDAY:
        assert _switch(page, name).get_attribute("aria-checked") == "true", name
    for name in ADMIN:
        assert _switch(page, name).get_attribute("aria-checked") == "false", name


def test_save_writes_the_switches_into_the_agents_config(page, _agent_on_disk) -> None:
    """Off for an everyday tool lands in disabled_tools; on for an admin tool
    in tools. Everything else the agent had is kept."""
    _open_connector_editor(page)
    page.locator("[data-connector-tools]").first.wait_for(state="visible", timeout=15_000)
    _wait_enabled(page, "whatsapp_template_send")

    _switch(page, "whatsapp_template_send").click()
    _switch(page, "whatsapp_template_create").click()
    assert _switch(page, "whatsapp_template_send").get_attribute("aria-checked") == "false"
    assert _switch(page, "whatsapp_template_create").get_attribute("aria-checked") == "true"

    page.get_by_role("button", name="Save", exact=True).click()

    deadline = time.monotonic() + 15
    written: dict = {}
    while time.monotonic() < deadline:
        written = json.loads((_agent_on_disk / "agent.json5").read_text(encoding="utf-8"))
        if "whatsapp_template_create" in (written.get("tools") or []):
            break
        page.wait_for_timeout(250)

    assert written.get("tools") == ["web_search", "whatsapp_template_create"]
    assert written.get("disabled_tools") == ["whatsapp_template_send"]
    channel = written["channels"][0]
    assert channel["type"] == "platform:whatsapp_twilio", "the connector itself was saved too"
    assert channel["extra"]["account_sid"] == "AC" + "0" * 32
