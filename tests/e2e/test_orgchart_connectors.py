"""E2E: the org chart shows each agent's messaging connectors on its card.

Every agent card carries one small chip per configured connector (WhatsApp,
Telegram, Discord, Slack…), so a reader can see at a glance which agent answers
where and which connection is broken. The chips are fed by ``/api/org-chart``'s
per-agent ``channels`` rows, which the runtime builds from in-memory adapter
state only (``runtime/channel_status.py``). These tests put real connector
configs on fake agents and fake adapters in every state into the runtime's
bookkeeping, so the whole backend → React path is what renders.

Both viewports: the chip row makes the card taller, and the drawer's connector
list is part of the bottom sheet on a phone.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import httpx
import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

SLACK_REFUSAL = "Slack rejected the bot token (invalid_auth)."
IMAP_MISSING = "Set EMAIL_PASSWORD in the vault to start this connector."


def _platform(slug: str, *, connected: bool = True, fatal: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        platform=SimpleNamespace(value=slug),
        is_connected=connected,
        fatal_error_message=fatal,
        name=slug.title(),
    )


def _classic(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, _rebuilding=False)


def _build_gateway():
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    def cfg(name: str, org: dict, channels: list) -> AgentConfig:
        return AgentConfig(
            name=name,
            model="anthropic:claude-haiku-4-5-20251001",
            backend=FilesystemBackendConfig(root_dir="workspace"),
            org=org,
            channels=channels,
        )

    configs = {
        "boss": cfg(
            "boss", {"reports": ["merli", "scout", "ledger"]}, ["platform:telegram", "slack"]
        ),
        "merli": cfg(
            "merli",
            {"manager": "boss", "reports": ["concierge"]},
            ["platform:whatsapp", "platform:telegram", "imap"],
        ),
        "scout": cfg("scout", {"manager": "boss"}, ["discord", "platform:slack"]),
        "ledger": cfg("ledger", {"manager": "boss"}, []),
        # Eight connectors: more than a default-width card fits (seven), so the
        # row ends in a "+N" chip.
        "concierge": cfg(
            "concierge",
            {"manager": "merli"},
            [
                "platform:whatsapp",
                "platform:telegram",
                "platform:signal",
                "platform:sms",
                "platform:discord",
                "platform:matrix",
                "platform:webhook",
                "platform:weixin",
            ],
        ),
    }
    gw = harness._gateway_for({n: harness._FakeRunner(c, "ok") for n, c in configs.items()})
    gw._agent_resources = {
        "boss": {"channels": [_classic("slack")], "platforms": [_platform("telegram")]},
        "merli": {"channels": [], "platforms": [_platform("whatsapp"), _platform("telegram")]},
        "scout": {
            "channels": [_classic("discord")],
            "platforms": [_platform("slack", connected=False, fatal=SLACK_REFUSAL)],
        },
        "ledger": {"channels": [], "platforms": []},
        "concierge": {
            "channels": [],
            "platforms": [
                _platform("whatsapp", connected=False),  # waiting for its QR scan
                *(
                    _platform(s)
                    for s in ("telegram", "signal", "sms", "discord", "matrix", "webhook", "weixin")
                ),
            ],
        },
    }
    gw._skipped_channels = {"merli": {"imap": IMAP_MISSING}}
    return gw


@pytest.fixture(scope="module")
def connectors_dashboard():
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    # launch_dashboard replaces the module-global seed list with these agents,
    # and every later test on this worker reseeds its home from it: restore the
    # session dashboard's list on the way out, or five agents it never ran would
    # land on disk in every test after this module.
    previous_seed = dict(harness._SEED_ENTRIES)
    try:
        with harness.launch_dashboard(gateway=_build_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")
    finally:
        harness._SEED_ENTRIES.clear()
        harness._SEED_ENTRIES.update(previous_seed)


@pytest.fixture(params=["desktop", "mobile"])
def chart(request, connectors_dashboard):
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        page = ctx.new_page()
        page.goto(f"{connectors_dashboard.base_url}/orgchart", wait_until="domcontentloaded")
        page.locator(".org-node[aria-label=merli]").wait_for(state="visible", timeout=30_000)
        try:
            yield page
        finally:
            ctx.close()
            browser.close()


def _chip_labels(page, agent: str) -> list[str]:
    chips = page.locator(f".org-node[aria-label={agent}] .org-ch[role=img]")
    return [chips.nth(i).get_attribute("aria-label") or "" for i in range(chips.count())]


def test_each_card_shows_its_connectors_with_their_status(chart):
    labels = _chip_labels(chart, "merli")
    assert labels[:2] == ["WhatsApp · connected", "Telegram · connected"]
    assert labels[2].startswith("Email (IMAP) · needs attention")
    assert IMAP_MISSING in labels[2]
    # No connectors: no chip row, and the card keeps its original size.
    assert chart.locator(".org-node[aria-label=ledger] .org-chips").count() == 0


def test_a_failing_connector_is_flagged_and_explains_itself(chart):
    slack = chart.locator(".org-node[aria-label=scout] .org-ch.is-error")
    assert slack.count() == 1
    assert SLACK_REFUSAL in (slack.get_attribute("aria-label") or "")
    pairing = chart.locator(".org-node[aria-label=concierge] .org-ch.is-connecting")
    assert (pairing.first.get_attribute("aria-label") or "").startswith("WhatsApp · connecting")


def test_connectors_that_do_not_fit_fold_into_a_plus_chip(chart):
    labels = _chip_labels(chart, "concierge")
    assert len(labels) == 7  # six chips + the "+N" slot on a 176px card
    assert labels[-1].startswith("2 more: Webhook")
    assert (
        chart.locator(".org-node[aria-label=concierge] .org-ch.is-more").inner_text().strip()
        == "+2"
    )


def test_a_live_turn_pulses_the_chip_it_arrived_on(chart, connectors_dashboard):
    chart.locator(".a2a-live.is-live").wait_for(state="visible", timeout=15_000)
    connectors_dashboard.push_event(
        "thread_activity",
        {"agent": "merli", "thread_id": "wa-1", "status": "thinking", "channel": "whatsapp"},
    )
    active = chart.locator(".org-node[aria-label=merli] .org-ch.is-active")
    active.wait_for(state="visible", timeout=5_000)
    assert (active.get_attribute("aria-label") or "").startswith("WhatsApp · answering a message")
    # Another agent on the same messenger is not the one answering.
    assert chart.locator(".org-node[aria-label=concierge] .org-ch.is-active").count() == 0

    connectors_dashboard.push_event(
        "thread_activity",
        {"agent": "merli", "thread_id": "wa-1", "status": "idle", "channel": "whatsapp"},
    )
    active.wait_for(state="detached", timeout=5_000)


def test_search_finds_agents_by_connector(chart):
    chart.locator(".org-search input").fill("whatsapp")
    matched = chart.locator(".org-node.is-match")
    matched.first.wait_for(state="visible", timeout=5_000)
    assert sorted(matched.nth(i).get_attribute("aria-label") for i in range(matched.count())) == [
        "concierge",
        "merli",
    ]


def test_drawer_lists_connectors_and_opens_the_connectors_tab(chart):
    chart.locator(".org-node[aria-label=merli]").click()
    rows = chart.locator(".org-drawer .org-conn-row")
    rows.first.wait_for(state="visible", timeout=5_000)
    assert rows.count() == 3
    assert IMAP_MISSING in rows.nth(2).inner_text()
    chart.locator(".org-drawer").get_by_role("button", name="Manage connectors").click()
    chart.wait_for_url(re.compile(r"/agents/merli/connectors$"), timeout=10_000)


def test_the_connectors_toggle_hides_and_restores_the_chips(chart):
    toggle = chart.locator("button.chat-chip", has_text="Connectors")
    toggle.click()
    chart.locator(".org-node .org-ch").first.wait_for(state="detached", timeout=5_000)
    assert chart.locator(".org-node .org-chips").count() == 0
    toggle.click()
    chart.locator(".org-node[aria-label=merli] .org-ch").first.wait_for(
        state="visible", timeout=5_000
    )


def test_a_hand_placed_card_still_fits_its_chip_row(chart, connectors_dashboard):
    """A card dragged before it had connectors was saved one chip row short, and
    the saved box wins over the auto layout: it must still show every chip."""
    httpx.put(
        f"{connectors_dashboard.runtime_url}/api/auth/me/prefs",
        json={"prefs": {"orgchart_layout": {"a:merli": {"x": 32, "y": 160, "w": 176, "h": 62}}}},
        timeout=10,
    ).raise_for_status()
    chart.reload(wait_until="domcontentloaded")
    card = chart.locator(".org-node[aria-label=merli]")
    card.wait_for(state="visible", timeout=30_000)
    row = card.locator(".org-chips")
    row.wait_for(state="visible", timeout=5_000)
    card_box, row_box = card.bounding_box(), row.bounding_box()
    assert card_box and row_box
    assert row_box["y"] + row_box["height"] <= card_box["y"] + card_box["height"] + 0.5
