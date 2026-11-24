"""E2E: the chat's browser window when a site asks for a person.

There is no live browser behind the harness, so the agent's browser state is
answered in the page (Playwright request interception on
``/api/agents/<name>/browser``): an open live session whose agent is waiting
for a person to pass a check (``session.human_check``, the shape
``olano/browser/sessions.Session.snapshot`` returns). What this proves is the
dashboard's half: the banner names the site and how long the agent waits, it
offers Verify now and Skip at both viewports (a pill on a phone), and Skip
tells the server.
"""

from __future__ import annotations

import json
import time

import pytest

from tests.e2e import harness
from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e


def _state(waiting: bool) -> dict:
    now = time.time()
    return {
        "agent": "merli",
        "availability": {"mode": "live", "provider": "local", "enabled": True},
        "live_view": True,
        "signed_in_state": False,
        "session": {
            "agent": "merli",
            "kind": "live",
            "opened_at": now - 60,
            "last_used": now,
            "controller": "agent",
            "attention": "propertyguru.com.sg wants a person to confirm they are human (Cloudflare)."
            if waiting
            else "",
            "human_check": {
                "site": "propertyguru.com.sg",
                "who": "Cloudflare",
                "mode": "wait",
                "started_at": now,
                "deadline": now + 290,
                "reason": "",
            }
            if waiting
            else None,
            "viewers": 0,
            "url": "https://www.propertyguru.com.sg/",
            "title": "",
            "blocked": [],
            "recording": None,
        },
    }


def test_the_chat_offers_verify_now_and_skip(page, dashboard: harness.RunningDashboard) -> None:
    skipped: list[str] = []
    waiting = {"on": True}

    def browser_state(route) -> None:
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_state(waiting["on"]))
        )

    def skip(route) -> None:
        skipped.append(route.request.method)
        waiting["on"] = False
        route.fulfill(status=200, content_type="application/json", body='{"skipped": true}')

    page.route("**/api/agents/*/browser", browser_state)
    page.route("**/api/agents/*/browser/human-check/skip", skip)
    page.goto(dashboard.base_url.rstrip("/") + "/chat", wait_until="domcontentloaded")
    wait_for_app_shell(page)

    dock = page.locator("[data-testid='browser-dock']")
    dock.wait_for(state="visible", timeout=15_000)
    verify = dock.locator("[data-testid='dock-verify']")
    verify.wait_for(state="visible")
    mobile = page.viewport_size["width"] < 900
    if mobile:
        # A phone shows the pill; Verify now opens the full-screen view, where
        # the banner says what is asked and offers Skip.
        assert "Verify you're human" in dock.inner_text()
        verify.click()
        banner = page.locator(
            "[data-testid='browser-dock-expanded'] [data-testid='dock-human-check']"
        )
    else:
        banner = dock.locator("[data-testid='dock-human-check']")
    banner.wait_for(state="visible")
    text = banner.inner_text()
    assert "propertyguru.com.sg wants a person to confirm they're human." in text
    assert "The agent waits 4:" in text
    if mobile:
        # The large view has Take control already; the banner does not repeat it.
        assert banner.locator("[data-testid='dock-verify']").count() == 0

    banner.locator("[data-testid='dock-skip-check']").click()
    deadline = time.monotonic() + 10
    while not skipped and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert skipped == ["POST"]


def test_the_browser_tab_shows_a_debug_trace_and_its_reading(
    page, dashboard: harness.RunningDashboard
) -> None:
    trace = {
        "agent": "merli",
        "reason": "human check",
        "started_at": time.time() - 120,
        "stopped_at": time.time() - 10,
        "events": [{"t": 0, "kind": "check_started"}] * 12,
        "exit_samples": [],
        "analysis": {
            "exit_addresses": ["203.0.113.7", "203.0.113.8"],
            "findings": [
                {
                    "code": "exit_changes",
                    "text": "The exit address changed while the check was running.",
                },
                {
                    "code": "clearance_rejected",
                    "text": "The browser sent Cloudflare's pass and Cloudflare challenged again anyway.",
                },
            ],
        },
    }

    def debug_trace(route) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(trace))

    page.route("**/api/agents/*/browser/debug-trace", debug_trace)
    page.goto(
        dashboard.base_url.rstrip("/") + "/agents/merli?tab=browser", wait_until="domcontentloaded"
    )
    wait_for_app_shell(page)
    card = page.locator("[data-testid='browser-debug-trace']")
    card.wait_for(state="visible", timeout=15_000)
    findings = card.locator("[data-testid='trace-findings']")
    findings.wait_for(state="visible")
    assert "exit address changed" in findings.inner_text()
    assert "challenged again anyway" in findings.inner_text()
    assert "12 events" in card.inner_text()
    # No browser open in the harness: recording needs one, downloading does not.
    assert card.locator("[data-testid='trace-start']").is_disabled()
    with page.expect_download() as download:
        card.locator("[data-testid='trace-download']").click()
    assert download.value.suggested_filename.startswith("browser-trace-merli-")
    box = card.bounding_box()
    assert (
        box is not None
        and box["x"] >= 0
        and box["x"] + box["width"] <= page.viewport_size["width"] + 1
    )
