"""E2E: the agent's Browser tab, "Actions that don't ask".

Drives ``/agents/<name>?tab=browser`` in a real browser the way a first-time
owner would (single-user mode is the implicit admin): starts from a one-click
example, adds a site by pasting a link, reads each rule back as a sentence,
saves, and the rules are in ``agent.json5`` (read back through the API after a
reload); and every mistake is explained where it is made and blocks the save. Runs at both
viewports. The agent config is seeded per test because the autouse
``lionclaw_home`` fixture gives every test a fresh home.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

AGENT = "merli"


@pytest.fixture(scope="module")
def aa_dashboard():
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
            }
        ),
        encoding="utf-8",
    )
    return cfg_dir


@pytest.fixture(params=["desktop", "mobile"])
def page(request, aa_dashboard, _agent_on_disk):
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(
            aa_dashboard.base_url.rstrip("/") + f"/agents/{AGENT}?tab=browser",
            wait_until="domcontentloaded",
        )
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def test_an_owner_adds_rules_from_an_example_and_a_pasted_link(page) -> None:
    section = page.get_by_test_id("browser-auto-approvals")
    section.wait_for(state="visible", timeout=20_000)
    section.get_by_text("No rules yet, so the agent asks you every time.").wait_for(state="visible")
    # The hard limit is stated where the rules are edited.
    section.get_by_text("Always asks, whatever you set here").wait_for(state="visible")

    # One click on an example gives a complete rule that reads back as a sentence.
    section.get_by_test_id("browser-aa-example-linkedin").click()
    first = section.get_by_test_id("browser-aa-rule").nth(0)
    first.get_by_text(
        "On linkedin.com, the agent may send messages and posts without asking you, "
        "only while you chat with it."
    ).wait_for(state="visible")

    # "Add a site" focuses the new rule's website box; a pasted link becomes its site.
    section.get_by_test_id("browser-aa-add").click()
    second = section.get_by_test_id("browser-aa-rule").nth(1)
    site = second.get_by_label("Website")
    assert site.evaluate("el => el === document.activeElement")
    site.fill("https://www.Example.com/contact?x=1")
    second.get_by_label("Submit forms").check()
    second.get_by_label("Also when it works on its own").check()
    second.get_by_label("Submit forms").focus()  # leave the box: it shows what is saved
    assert site.input_value() == "example.com"
    second.get_by_text(
        "On example.com, the agent may send messages and posts and submit forms without "
        "asking you, including when it works on its own."
    ).wait_for(state="visible")
    second.get_by_text("Covers example.com and all of its pages and sub-domains.").wait_for(
        state="visible"
    )

    section.get_by_text("Unsaved changes").first.wait_for(state="visible")
    page.get_by_test_id("browser-aa-save").click()
    page.get_by_text("Browser rules saved.").first.wait_for(state="visible", timeout=10_000)

    page.reload(wait_until="domcontentloaded")
    section = page.get_by_test_id("browser-auto-approvals")
    section.get_by_test_id("browser-aa-rule").nth(1).wait_for(state="visible", timeout=20_000)
    # Saved rules fold into their sentence; Edit opens one again.
    assert section.get_by_label("Website").count() == 0
    section.get_by_role("button", name="Edit the rule for example.com").click()
    assert section.get_by_label("Website").input_value() == "example.com"
    body = page.evaluate(
        f"() => fetch('/api/agents/{AGENT}/browser/auto-approvals').then(r => r.json())"
    )
    assert body["rules"] == [
        {"site": "linkedin.com", "actions": ["send"], "in_background": False},
        {"site": "example.com", "actions": ["send", "submit"], "in_background": True},
    ]


def test_mistakes_are_explained_and_block_the_save(page) -> None:
    section = page.get_by_test_id("browser-auto-approvals")
    section.wait_for(state="visible", timeout=20_000)
    section.get_by_test_id("browser-aa-add").click()
    rule = section.get_by_test_id("browser-aa-rule").first
    rule.get_by_label("Website").fill("linkedin")
    rule.get_by_text('"linkedin" doesn\'t look like a website').wait_for(state="visible")
    assert page.get_by_test_id("browser-aa-save").is_disabled()

    rule.get_by_label("Website").fill("example.com")
    rule.get_by_label("Send messages and posts").uncheck()
    rule.get_by_text("Choose at least one action for example.com").first.wait_for(state="visible")
    assert page.get_by_test_id("browser-aa-save").is_disabled()

    rule.get_by_label("Sign up and book").check()
    section.get_by_test_id("browser-aa-add").click()
    dup = section.get_by_test_id("browser-aa-rule").nth(1)
    dup.get_by_label("Website").fill("www.example.com")
    dup.get_by_text("example.com already has a rule").wait_for(state="visible")
    assert page.get_by_test_id("browser-aa-save").is_disabled()

    dup.get_by_role("button", name="Remove the rule for example.com").click()
    assert page.get_by_test_id("browser-aa-save").is_enabled()
