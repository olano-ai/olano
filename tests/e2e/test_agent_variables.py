"""E2E: the agent edit "Variables" tab (agent-scoped vault overrides).

Drives the real browser through ``/agents/<name>?tab=variables`` — the
owner-or-admin surface backed by ``/api/agents/{name}/vault/*``. The default
mock gateway has no agent config on disk and no vault, so this module launches
its own dashboard, registers an unlocked ``VaultStore`` with the in-process
resolver (one common secret + one merli override), and writes the agent config
where the runtime will look **during the test**: the root conftest's autouse
``lionclaw_home`` re-points ``LIONCLAW_HOME`` at a fresh per-test home (the
harness's own throwaway home is superseded), so ``_agent_config_path``
resolves against that — the ``_agent_on_disk`` fixture seeds it per test,
ordered before the page fixture navigates. Runs at both viewports via the
rebound ``page`` fixture; assertions are read-only so the two viewport runs
stay independent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

AGENT = "merli"


@pytest.fixture(scope="module")
def vars_dashboard():
    """A dashboard whose in-process vault is seeded with common + override."""
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")

    import tempfile

    from olano.vault.resolver import set_vault
    from olano.vault.store import VaultStore

    try:
        with harness.launch_dashboard(gateway=harness.build_mock_gateway()) as running:
            # The vault resolver is module-global in the runtime's process (the
            # server runs in a thread), so registering here is visible to every
            # request. The DB lives outside any per-test home on purpose — the
            # store handle keeps working while homes rotate between tests.
            with tempfile.TemporaryDirectory(prefix="lc-vars-vault-") as vault_dir:
                store = VaultStore.open(
                    passphrase="e2e-vars-pass",
                    db_path=Path(vault_dir) / "vault.db",
                    use_keyring=False,
                )
                store.set(
                    "OPENAI_API_KEY", "sk-common", description="Shared key", category="api-keys"
                )
                store.set("SLACK_BOT_TOKEN", "xoxb-common", description="Shared bot")
                store.set("OPENAI_API_KEY", "sk-merli", description="merli's key", agent=AGENT)
                set_vault(store)
                try:
                    yield running
                finally:
                    set_vault(None)
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


@pytest.fixture
def _agent_on_disk(lionclaw_home):
    """agents/<AGENT>/agent.json5 in the ACTIVE per-test home.

    The new endpoints 404 for agents with no config on disk, and the autouse
    ``lionclaw_home`` fixture makes a fresh home per test — so the config must
    be seeded per test, not once in the module fixture.
    """
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


# `vars_dashboard` (module-scoped) must drive the `page` fixture instead of the
# session `dashboard`. Rebind `page` locally so it consumes our dashboard; it
# also depends on `_agent_on_disk` so the config exists before the SPA's first
# API call.
@pytest.fixture(params=["desktop", "mobile"])
def page(request, vars_dashboard, _agent_on_disk):
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(
            vars_dashboard.base_url.rstrip("/") + f"/agents/{AGENT}?tab=variables",
            wait_until="domcontentloaded",
        )
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def test_variables_tab_lists_overrides_and_shared_keys(page) -> None:
    """The deep-linked tab paints both cards with the seeded vault state."""
    # Single-user mode → implicit admin → the tab button is present and active.
    # The detail tab rail is an ARIA tablist — its items are tabs, not buttons.
    tab_btn = page.get_by_role("tab", name="Variables")
    tab_btn.first.wait_for(state="visible", timeout=20_000)

    # This agent's override row (name + reveal/edit/delete actions).
    page.get_by_text("Agent variables", exact=True).wait_for(state="visible", timeout=15_000)
    row = page.locator("div.row", has_text="OPENAI_API_KEY").filter(
        has=page.locator("button[aria-label='Reveal value']")
    )
    row.first.wait_for(state="visible", timeout=10_000)

    # Shared keys card: the overridden key is badged, the other offers Override.
    page.get_by_text("Shared vault keys", exact=True).wait_for(state="visible", timeout=10_000)
    page.get_by_text("overridden", exact=True).wait_for(state="visible", timeout=10_000)
    shared_row = page.locator("div.row", has_text="SLACK_BOT_TOKEN")
    shared_row.locator("button", has_text="Override").first.wait_for(
        state="visible", timeout=10_000
    )
