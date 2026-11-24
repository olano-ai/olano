"""E2E: the WhatsApp over Twilio connector's setup form.

The first version of this form asked for a **Public webhook URL** plus a bind
host and port: the operator was expected to stand up a public endpoint and
paste its address into Twilio. A person setting up WhatsApp for a shop cannot
do that, and on a managed deployment the port was unreachable anyway. The
connector now registers the deployment's own address with Twilio when it
starts, so the form asks only for what Twilio's Console shows (Account SID,
Auth Token, a WhatsApp number) and says, in a note where the URL field used to
be, that there is nothing else to set. The setup guide beside the form walks
through where each value lives.

Drives the real browser through ``/agents/<name>?tab=connectors``. Both
viewports run (the ``page`` fixture is parametrized), per the dashboard rule in
CLAUDE.md.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

AGENT = "wabot"


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


def test_the_form_asks_for_no_webhook(page) -> None:
    """No URL, host or port to fill in: the connector registers its own."""
    _open_connector_editor(page)
    shell = page.locator(".connector-shell").first

    note = shell.locator(
        ".connector-readonly", has_text="Incoming messages are set up automatically"
    )
    note.wait_for(state="visible", timeout=15_000)
    assert "no webhook to copy or paste" in note.inner_text()

    text = shell.inner_text()
    for gone in ("Public webhook URL", "Local bind host", "Local bind port"):
        assert gone not in text, f"{gone!r} is still on the form"


def test_saved_values_open_in_the_form(page) -> None:
    """The adapter reads ``extra``, so that is where the form reads and writes.

    The first form had no ``extraKey`` on any field: it wrote the Account SID
    to the top level, where the runtime drops it, and opened a correctly
    saved connector with every field empty.
    """
    _open_connector_editor(page)
    shell = page.locator(".connector-shell").first

    sid = shell.locator("input[placeholder^='ACx']").first
    sid.wait_for(state="visible", timeout=15_000)
    assert sid.input_value() == "AC" + "0" * 32
    assert shell.locator("input[placeholder='+14155238886']").first.input_value() == "+14155238886"


def test_the_guide_walks_through_the_twilio_console(page) -> None:
    """The guide names where each value lives, instead of the generic fallback."""
    _open_connector_editor(page)

    guide = page.locator(".connector-guide").first
    guide.wait_for(state="visible", timeout=15_000)
    text = guide.inner_text()
    assert "WhatsApp via Twilio" in text
    assert "console.twilio.com" in text
    assert "Account Info" in text
    assert "join code" in text
    assert "When a message comes in" in text
    # Not the generic three-step fallback.
    assert "Fill the required fields." not in text


def test_owner_numbers_and_chat_approvals_are_offered(page) -> None:
    """Owners are what a business connector's customer allowlist is not: the
    form has to offer them, and say the allowlist makes nobody an owner."""
    _open_connector_editor(page)
    shell = page.locator(".connector-shell").first
    text = shell.inner_text()
    assert "Owner numbers" in text
    assert "never makes anyone an owner" in text
    toggle = page.locator(".connector-toggle", has_text="Ask owners in chat").locator("input").first
    toggle.wait_for(state="visible", timeout=15_000)
    assert toggle.is_checked() is False
