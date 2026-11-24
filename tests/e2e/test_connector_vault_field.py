"""E2E: a connector's credential is edited in the vault, not in agent.json5.

The field used to be a text input bound to the channel config, so after the
first save it displayed the literal string ``${vault:TELEGRAM_BOT_TOKEN}`` and
the eye toggle revealed *the reference*. There was no way to see or confirm the
real token from the connector panel.

It is now a vault field: labelled with the exact key the runtime resolves,
showing that agent's per-agent override masked with a Show button, and saying
which value is actually in play. This drives the real browser through
``/agents/<name>?tab=connectors`` against a seeded in-process vault.

Same shape as ``test_agent_variables``: a module dashboard with an unlocked
``VaultStore`` registered on the in-process resolver, the agent config seeded
into the *active* per-test home, and the ``page`` fixture rebound so both
viewports run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

AGENT = "merli"
KEY = "TELEGRAM_BOT_TOKEN"
OVERRIDE = "8182517777:AAHrealbuttestonly"


@pytest.fixture(scope="module")
def connector_dashboard():
    """A dashboard whose vault holds this agent's own Telegram token."""
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")

    import tempfile

    from olano.vault.resolver import set_vault
    from olano.vault.store import VaultStore

    try:
        with harness.launch_dashboard(gateway=harness.build_mock_gateway()) as running:
            with tempfile.TemporaryDirectory(prefix="lc-conn-vault-") as vault_dir:
                store = VaultStore.open(
                    passphrase="e2e-conn-pass",
                    db_path=Path(vault_dir) / "vault.db",
                    use_keyring=False,
                )
                # Per-agent override — the whole point: one obvious key name,
                # a different value per agent.
                store.set(KEY, OVERRIDE, description="merli's bot", agent=AGENT)
                # A shared value for a second connector, to prove the field
                # distinguishes "set for this agent" from "using the shared one".
                store.set("DISCORD_BOT_TOKEN", "shared-discord")
                set_vault(store)
                try:
                    yield running
                finally:
                    set_vault(None)
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


@pytest.fixture
def _agent_on_disk(lionclaw_home):
    """An agent with a Telegram connector and **no credential in its config**."""
    cfg_dir = lionclaw_home / "agents" / AGENT
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": AGENT,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "backend": {"type": "filesystem", "root_dir": "workspace"},
                "channels": [{"type": "telegram", "allowed_users": ["5550001234"]}],
            }
        ),
        encoding="utf-8",
    )
    return cfg_dir


@pytest.fixture(params=["desktop", "mobile"])
def page(request, connector_dashboard, _agent_on_disk):
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(
            connector_dashboard.base_url.rstrip("/") + f"/agents/{AGENT}?tab=connectors",
            wait_until="domcontentloaded",
        )
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def _open_connector_editor(page) -> None:
    """Open the configured Telegram connector's edit modal.

    The row's edit affordance is an icon button — it carries an aria-label
    ("Edit connector"), not visible text. The modal is what mounts
    ``ConnectorForm``, so everything below depends on getting it open.
    """
    # The detail tab rail is an ARIA tablist — its items are tabs, not buttons.
    page.get_by_role("tab", name="Connectors").first.wait_for(state="visible", timeout=20_000)
    edit = page.get_by_role("button", name="Edit connector").first
    edit.wait_for(state="visible", timeout=20_000)
    edit.click()
    page.locator(".connector-shell").first.wait_for(state="visible", timeout=15_000)


def test_the_credential_field_names_the_vault_key_it_resolves(page) -> None:
    """The key IS the contract — the runtime resolves exactly this name."""
    _open_connector_editor(page)

    body = page.locator("body").inner_text()
    assert KEY in body, "the connector field must show the vault key it resolves"
    assert "Per-agent override" in body


def test_the_token_is_masked_until_revealed(page) -> None:
    """A credential is never on screen by default, even with the vault open."""
    _open_connector_editor(page)

    field = page.locator(".vault-secret-field input").first
    field.wait_for(state="visible", timeout=15_000)
    assert field.get_attribute("type") == "password"
    assert OVERRIDE not in page.locator("body").inner_text()

    page.locator(".vault-secret-field button[aria-label='Show value']").first.click()
    page.wait_for_timeout(1200)

    assert page.locator(".vault-secret-field input").first.input_value() == OVERRIDE


def test_the_field_says_which_value_is_actually_in_play(page) -> None:
    """'Set for this agent' vs the shared value is the difference between two
    bots and one bot answering for both."""
    _open_connector_editor(page)

    field = page.locator(".vault-secret-field").first
    field.wait_for(state="visible", timeout=15_000)
    # Wait for the badge, don't snapshot inner_text() the moment the field is
    # visible: the field mounts with state "unknown" and only says which value
    # is in play once the two /api/vault/secrets reads land, so the immediate
    # assertion raced that request and lost it on a loaded CI runner.
    field.get_by_text("Set for this agent").first.wait_for(state="visible", timeout=15_000)


def test_saving_the_connector_never_writes_the_credential_to_disk(page, _agent_on_disk) -> None:
    """The payload must not carry the credential — it lives in the vault."""
    _open_connector_editor(page)
    page.locator(".vault-secret-field").first.wait_for(state="visible", timeout=15_000)

    page.get_by_role("button", name="Save").first.click()
    page.wait_for_timeout(2000)

    written = json.loads((_agent_on_disk / "agent.json5").read_text(encoding="utf-8"))
    channel = next(c for c in written["channels"] if c.get("type") == "telegram")
    assert "token" not in channel
    assert OVERRIDE not in (_agent_on_disk / "agent.json5").read_text(encoding="utf-8")


# ── the typed credential is saved by the connector's own Save ────────────────

TWILIO_AGENT = "marcus"
TWILIO_KEY = "TWILIO_AUTH_TOKEN"


@pytest.fixture
def _twilio_agent_on_disk(lionclaw_home):
    """An agent whose WhatsApp (Twilio) connector has no token anywhere yet."""
    cfg_dir = lionclaw_home / "agents" / TWILIO_AGENT
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "agent.json5").write_text(
        json.dumps(
            {
                "name": TWILIO_AGENT,
                "model": "anthropic:claude-haiku-4-5-20251001",
                "backend": {"type": "filesystem", "root_dir": "workspace"},
                "channels": [
                    {
                        "type": "platform:whatsapp_twilio",
                        "extra": {"account_sid": "AC" + "0" * 32, "from_number": "+14155238886"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return cfg_dir


@pytest.mark.parametrize("viewport", ["desktop", "mobile"])
def test_a_token_typed_into_the_field_is_stored_by_the_connectors_save(
    connector_dashboard, _twilio_agent_on_disk, viewport
) -> None:
    """The reported failure, end to end.

    The field kept what was typed as its own draft, stored only by a separate
    "Save to vault" button beside it. Typing the Auth Token and pressing the
    connector's Save saved the connector without it, and after a restart the
    connector refused to start: no credential for TWILIO_AUTH_TOKEN.
    """
    import time

    from olano.vault.resolver import get_vault
    from tests.e2e.conftest import VIEWPORTS

    vault = get_vault()
    assert vault is not None
    vault.delete(TWILIO_KEY, agent=TWILIO_AGENT)  # each viewport starts from "not set"
    token = f"twilio-e2e-token-{viewport}"

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[viewport])
        page = ctx.new_page()
        try:
            page.goto(
                connector_dashboard.base_url.rstrip("/") + f"/agents/{TWILIO_AGENT}?tab=connectors",
                wait_until="domcontentloaded",
            )
            _open_connector_editor(page)
            field = page.locator(".vault-secret-field").first
            field.get_by_text("Not set").first.wait_for(state="visible", timeout=15_000)

            field.locator("input").first.fill(token)
            # Says where the value goes, and there is one Save, not two.
            field.get_by_text("when you save the connector").first.wait_for(
                state="visible", timeout=5_000
            )
            assert field.get_by_role("button", name="Save to vault").count() == 0

            # Exactly the modal's Save: a substring match would also find the
            # old field's own "Save to vault" button, which is the one path
            # that did work, and so could never catch this bug.
            page.get_by_role("button", name="Save", exact=True).click()
            deadline = time.monotonic() + 15
            stored = ""
            while time.monotonic() < deadline:
                try:
                    stored = vault.get(TWILIO_KEY, agent=TWILIO_AGENT, touch=False)
                except Exception:
                    stored = ""
                if stored:
                    break
                page.wait_for_timeout(250)
        finally:
            ctx.close()
            browser.close()

    assert stored == token, "the connector's Save must store the typed token in the vault"
    assert vault.list_overrides(name=TWILIO_KEY, agent=TWILIO_AGENT), "as this agent's override"
    written = (_twilio_agent_on_disk / "agent.json5").read_text(encoding="utf-8")
    assert token not in written, "and never in agent.json5"
    channel = json.loads(written)["channels"][0]
    assert channel["type"] == "platform:whatsapp_twilio"
    assert "auth_token" not in channel
