"""E2E: the WhatsApp connector's group-listening controls.

Two settings that decide whether an agent talks in a WhatsApp group at all,
and they used to be invisible or unavailable in the UI:

* **Group listening** (``extra.require_mention``) — the default is "All
  messages": the bridge links as a device on a real account, so the agent sees
  and answers every message in every group that account belongs to. That is a
  surprising default to leave implicit behind an unchecked "Require @mention"
  box, which reads as if the *unchecked* state were the restrictive one. It is
  now an explicit two-option select that names the default.
* **Listen-only (monitor) mode** (``extra.listen_only``) — receive and process
  everything, never send anything back. Without a toggle there was no way to run
  a monitoring agent short of hand-editing ``agent.json5``.

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

AGENT = "watcher"


@pytest.fixture(scope="module")
def whatsapp_dashboard():
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    try:
        with harness.launch_dashboard(gateway=harness.build_mock_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


@pytest.fixture
def _agent_on_disk(lionclaw_home):
    """An agent with a WhatsApp connector and no behavior overrides set."""
    cfg_dir = lionclaw_home / "agents" / AGENT
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": AGENT,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "backend": {"type": "filesystem", "root_dir": "workspace"},
                "channels": [{"type": "platform:whatsapp", "extra": {}}],
            }
        ),
        encoding="utf-8",
    )
    return cfg_dir


@pytest.fixture(params=["desktop", "mobile"])
def page(request, whatsapp_dashboard, _agent_on_disk):
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(
            whatsapp_dashboard.base_url.rstrip("/") + f"/agents/{AGENT}?tab=connectors",
            wait_until="domcontentloaded",
        )
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def _open_connector_editor(page) -> None:
    # The detail tab rail is an ARIA tablist — its items are tabs, not buttons.
    page.get_by_role("tab", name="Connectors").first.wait_for(state="visible", timeout=20_000)
    edit = page.get_by_role("button", name="Edit connector").first
    edit.wait_for(state="visible", timeout=20_000)
    edit.click()
    page.locator(".connector-shell").first.wait_for(state="visible", timeout=15_000)


def _group_listening(page):
    return page.locator(".connector-shell select").first


def test_group_listening_is_a_named_choice_not_a_bare_checkbox(page) -> None:
    """Both postures have to be readable without opening the docs."""
    _open_connector_editor(page)

    select = _group_listening(page)
    select.wait_for(state="visible", timeout=15_000)
    options = select.locator("option").all_inner_texts()

    assert any("All messages" in o for o in options)
    assert any("@mentions only" in o for o in options)
    # Long labels truncate in the 390px select, so the default has to be
    # legible in the collapsed control, not only in the open option list.
    assert all(len(o) < 30 for o in options), f"option labels too long for mobile: {options}"


def test_the_default_shown_is_the_default_the_backend_applies(page) -> None:
    """An agent with nothing set answers every group message — the form must say
    so rather than defaulting the display to the other posture.

    The control is now the three-way ``group_respond`` select (all / mentions /
    none) that superseded the boolean ``require_mention``; "all" is the value
    carrying the old ``require_mention: false`` meaning.
    """
    _open_connector_editor(page)

    select = _group_listening(page)
    select.wait_for(state="visible", timeout=15_000)
    assert select.input_value() == "all"


def test_listen_only_toggle_is_offered_and_off_by_default(page) -> None:
    _open_connector_editor(page)

    body = page.locator(".connector-shell").inner_text()
    assert "Listen-only" in body

    toggle = page.locator(".connector-toggle", has_text="Listen-only").locator("input").first
    toggle.wait_for(state="visible", timeout=15_000)
    assert toggle.is_checked() is False


def test_bridge_transport_is_collapsed_and_last_because_pairing_is_by_qr(page) -> None:
    """The bridge script/port/session knobs are overrides the QR flow never
    needs. Leading with them (as the form used to) buries Access control and
    Behavior — the sections that actually decide how the agent behaves."""
    _open_connector_editor(page)

    headings = page.locator(".connector-fieldset h3").all_inner_texts()
    assert "CREDENTIALS & TRANSPORT" in headings[-1].upper()

    section = page.locator("details.connector-fieldset").last
    # Nothing set on this agent, so it stays shut. (It auto-opens when the
    # agent has an override, so a bad value is never hidden behind a chevron.)
    assert section.evaluate("el => el.hasAttribute('open')") is False


def _open_bridge_section(page):
    section = page.locator("details.connector-fieldset").last
    section.locator("summary").click()
    return section


def test_the_bridge_port_defaults_to_auto_so_nobody_allocates_one_by_hand(page) -> None:
    """Every agent needs its own port, and picking one is not a decision a user
    should have to make — two agents on WhatsApp must just work."""
    _open_connector_editor(page)
    _open_bridge_section(page)

    auto = page.locator(".seg-control button", has_text="Auto").first
    auto.wait_for(state="visible", timeout=15_000)
    assert auto.get_attribute("aria-pressed") == "true"
    # No port input at all until Manual is chosen.
    assert page.locator(".connector-shell input[type='number']").count() == 0


def test_manual_is_available_and_rejects_a_port_that_could_never_bind(page, _agent_on_disk) -> None:
    """The escape hatch stays, but 6 (a typo for 3006 — the live outage) must
    not be storable: ports below 1024 need root and would kill the bridge."""
    _open_connector_editor(page)
    _open_bridge_section(page)

    page.locator(".seg-control button", has_text="Manual").first.click()
    port = page.locator(".connector-shell input[type='number']").first
    port.wait_for(state="visible", timeout=15_000)
    port.fill("6")

    page.get_by_role("button", name="Save").first.click()
    page.wait_for_timeout(1500)

    assert "at least 1024" in page.locator(".connector-error").inner_text()
    written = json.loads((_agent_on_disk / "agent.json5").read_text(encoding="utf-8"))
    channel = next(c for c in written["channels"] if c.get("type") == "platform:whatsapp")
    assert "bridge_port" not in channel.get("extra", {})


def test_a_deliberate_manual_port_still_saves(page, _agent_on_disk) -> None:
    _open_connector_editor(page)
    _open_bridge_section(page)

    page.locator(".seg-control button", has_text="Manual").first.click()
    port = page.locator(".connector-shell input[type='number']").first
    port.wait_for(state="visible", timeout=15_000)
    port.fill("3457")

    page.get_by_role("button", name="Save").first.click()
    page.wait_for_timeout(2000)

    written = json.loads((_agent_on_disk / "agent.json5").read_text(encoding="utf-8"))
    channel = next(c for c in written["channels"] if c.get("type") == "platform:whatsapp")
    assert channel["extra"]["bridge_port"] == 3457


def test_choosing_mentions_only_and_listen_only_persists_to_the_config(
    page, _agent_on_disk
) -> None:
    """The two settings have to survive a save — this is the whole feature."""
    _open_connector_editor(page)

    select = _group_listening(page)
    select.wait_for(state="visible", timeout=15_000)
    select.select_option("mentions")

    page.locator(".connector-toggle", has_text="Listen-only").locator("input").first.check()

    page.get_by_role("button", name="Save").first.click()
    page.wait_for_timeout(2000)

    written = json.loads((_agent_on_disk / "agent.json5").read_text(encoding="utf-8"))
    channel = next(c for c in written["channels"] if c.get("type") == "platform:whatsapp")
    extra = channel.get("extra", {})
    # `group_respond` is the successor to the boolean `require_mention`; picking
    # any option here overrides a legacy value (see connectorCatalog.ts). It is
    # stored as the select's own token, not coerced to a bool.
    assert extra.get("group_respond") == "mentions"
    assert extra.get("listen_only") is True


def test_chat_approvals_toggle_is_off_by_default_and_saves(page, _agent_on_disk) -> None:
    """``chat_approvals`` decides whether an owner is asked in the chat before a
    risky action or approved without asking; the form must offer it, show it
    off (the backend default) and store it where the runtime reads it."""
    _open_connector_editor(page)

    toggle = page.locator(".connector-toggle", has_text="Ask owners in chat").locator("input").first
    toggle.wait_for(state="visible", timeout=15_000)
    assert toggle.is_checked() is False
    assert "YES and the code" in page.locator(".connector-shell").inner_text()

    toggle.check()
    page.locator(".connector-toggle", has_text="Ask owners in chat").first.screenshot(
        path=str(_agent_on_disk / f"chat-approvals-{page.viewport_size['width']}.png")
    )
    page.get_by_role("button", name="Save").first.click()
    page.wait_for_timeout(1500)

    written = json.loads((_agent_on_disk / "agent.json5").read_text(encoding="utf-8"))
    channel = next(c for c in written["channels"] if c.get("type") == "platform:whatsapp")
    assert channel["extra"].get("chat_approvals") is True
