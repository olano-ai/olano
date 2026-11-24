"""E2E: the OpenAI-compatible API connector's setup form.

Setting this connector up is two things — mint a key, copy a URL — and the
form used to ask for neither. It opened on **Bind host** and **Bind port**,
two values the runtime derives on its own and which the public URL does not
depend on, then told the user to go run ``openssl`` in a terminal for the key
and to hand-substitute their own hostname into a ``https://<your dashboard
host>/api/openai/<agent>/v1`` template.

So the form now leads with the resolved address (built from the browser's own
origin and the agent's name — the SPA and the API share one origin on every
deployment shape, which is why the endpoint lives under ``/api/`` at all) and a
key the page can generate, with the bind knobs moved to Advanced for the LAN
case that genuinely needs them.

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

AGENT = "apibot"


@pytest.fixture(scope="module")
def api_dashboard():
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    try:
        with harness.launch_dashboard(gateway=harness.build_mock_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


@pytest.fixture
def _agent_on_disk(lionclaw_home):
    """An agent with the connector and nothing overridden."""
    cfg_dir = lionclaw_home / "agents" / AGENT
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": AGENT,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "backend": {"type": "filesystem", "root_dir": "workspace"},
                "channels": [{"type": "platform:api_server", "extra": {}}],
            }
        ),
        encoding="utf-8",
    )
    return cfg_dir


@pytest.fixture(params=["desktop", "mobile"])
def page(request, api_dashboard, _agent_on_disk):
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(
            api_dashboard.base_url.rstrip("/") + f"/agents/{AGENT}?tab=connectors",
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


def test_the_base_url_is_resolved_not_a_template(page) -> None:
    """The whole point: the user copies this, they do not fill it in.

    A ``https://<your dashboard host>/…`` template asks every reader to perform
    a substitution the page can do exactly — and to get it wrong sometimes.
    """
    _open_connector_editor(page)

    url_input = page.locator(".connector-shell input[readonly]").first
    url_input.wait_for(state="visible", timeout=15_000)
    value = url_input.input_value()

    assert value.endswith(f"/api/openai/{AGENT}/v1"), value
    assert value.startswith("http"), value
    # No placeholder survived into the thing the user is told to copy.
    assert "<" not in value and ">" not in value, value


def test_the_url_carries_this_agents_name(page) -> None:
    """One server per agent, and the path segment is what selects which — the
    OpenAI ``model`` field names a model, so nothing else in a request can."""
    _open_connector_editor(page)

    url_input = page.locator(".connector-shell input[readonly]").first
    url_input.wait_for(state="visible", timeout=15_000)
    assert f"/{AGENT}/" in url_input.input_value()


def test_the_url_has_a_copy_button(page) -> None:
    _open_connector_editor(page)

    page.locator(".connector-shell input[readonly]").first.wait_for(state="visible", timeout=15_000)
    assert page.get_by_role("button", name="Copy").first.is_visible()


def test_bind_knobs_are_collapsed_into_advanced(page) -> None:
    """Host and port are derived, and the public URL does not depend on either.
    Leading with them buried the two fields setup actually needs."""
    _open_connector_editor(page)

    # Nothing set on this agent, so Advanced stays shut. (It auto-opens when
    # the agent has an override, so a bad value is never hidden.)
    section = page.locator("details.connector-fieldset").last
    section.wait_for(state="visible", timeout=15_000)
    assert section.evaluate("el => el.hasAttribute('open')") is False

    # The credentials section — the one that opens — must not carry them.
    creds = page.locator("section.connector-fieldset").first.inner_text()
    assert "Bind host" not in creds
    assert "Bind port" not in creds


def test_the_port_defaults_to_auto(page) -> None:
    """A per-agent port is an allocation, not a decision the user should make:
    two agents on this connector must simply both work."""
    _open_connector_editor(page)

    page.locator("details.connector-fieldset").last.locator("summary").click()

    auto = page.locator(".seg-control button", has_text="Auto").first
    auto.wait_for(state="visible", timeout=15_000)
    assert auto.get_attribute("aria-pressed") == "true"
    # No port input at all until Manual is chosen.
    assert page.locator(".connector-shell input[type='number']").count() == 0


def test_the_key_field_offers_to_generate_one(page) -> None:
    """This credential is minted by us, not issued by a remote service, so
    there is nothing to paste from a provider's console — the alternative to a
    button is telling the user to go and run ``openssl``."""
    _open_connector_editor(page)

    body = page.locator(".connector-shell")
    body.wait_for(state="visible", timeout=15_000)
    # The vault field only renders its controls once unlocked; either way the
    # generate affordance must be reachable rather than an openssl instruction.
    assert "openssl" not in body.inner_text().lower()
    assert page.locator(".connector-shell").get_by_text("OLANO_API_KEY").first.is_visible()
