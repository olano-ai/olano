"""E2E: the agent Overview tab's "Fleet file access" switch.

``agent.json5 → fleet_access`` opens every agent's folder, the shared skills and
the logs to this agent's shell and file tools, so the switch asks before turning
on (and never before turning off), explains itself, and writes the field the
runtime reads. Both viewports run (the ``page`` fixture is parametrized), per
the dashboard rule in CLAUDE.md.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e

AGENT = "father"
LABEL = "Fleet file access"


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
def page(request, dashboard, _agent_on_disk):
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(
            dashboard.base_url.rstrip("/") + f"/agents/{AGENT}?tab=overview",
            wait_until="domcontentloaded",
        )
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def _switch(page):
    sw = page.get_by_role("switch", name=LABEL)
    sw.wait_for(state="visible", timeout=30_000)
    sw.scroll_into_view_if_needed()
    return sw


def _on_disk(cfg_dir) -> dict:
    return json.loads((cfg_dir / "agent.json5").read_text(encoding="utf-8"))


def _wait_saved(page, cfg_dir, expected: bool) -> None:
    for _ in range(50):
        if bool(_on_disk(cfg_dir).get("fleet_access")) is expected:
            return
        page.wait_for_timeout(200)
    raise AssertionError(f"fleet_access never became {expected}: {_on_disk(cfg_dir)}")


def test_switch_explains_itself_and_starts_off(page) -> None:
    sw = _switch(page)
    assert sw.get_attribute("aria-checked") == "false"
    body = page.locator("body").inner_text()
    for phrase in (
        "every agent's folder",
        "shared skills folder (read and write)",
        "deployment logs (read only)",
        "Off by default",
        "Only an administrator can change it",
        "Takes effect when the agent restarts",
    ):
        assert phrase in body, phrase


def test_cancel_leaves_it_off(page, _agent_on_disk) -> None:
    _switch(page).click()
    dialog = page.get_by_role("alertdialog")
    dialog.wait_for(state="visible", timeout=10_000)
    assert "access to every agent's files" in dialog.inner_text()
    dialog.get_by_role("button", name="Cancel").click()
    dialog.wait_for(state="hidden", timeout=10_000)
    assert _switch(page).get_attribute("aria-checked") == "false"
    page.wait_for_timeout(500)
    assert not _on_disk(_agent_on_disk).get("fleet_access")


def test_confirm_turns_it_on_and_off_needs_no_confirmation(page, _agent_on_disk) -> None:
    _switch(page).click()
    dialog = page.get_by_role("alertdialog")
    dialog.wait_for(state="visible", timeout=10_000)
    # The dialog fits the window at both breakpoints.
    box = dialog.bounding_box()
    width = page.evaluate("() => window.innerWidth")
    assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= width + 1, box
    page.screenshot(
        path=str(_agent_on_disk / f"fleet-access-confirm-{page.viewport_size['width']}.png")
    )
    dialog.get_by_role("button", name="Turn on").click()
    _wait_saved(page, _agent_on_disk, True)
    assert _switch(page).get_attribute("aria-checked") == "true"

    _switch(page).click()
    assert page.get_by_role("alertdialog").count() == 0
    _wait_saved(page, _agent_on_disk, False)
    assert _switch(page).get_attribute("aria-checked") == "false"
