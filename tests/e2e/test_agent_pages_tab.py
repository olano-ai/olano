"""E2E: the agent Pages tab shows deployed pages, their state, errors and logs.

Seeds three pages for ``merli`` in the active per-test home (a plain page, a
static build that is live, and a server that settled in ``failed``), plus the
supervisor's state snapshot and a server log in the agent's workspace, then
drives ``/agents/merli?tab=pages`` in a real browser at both viewports. The
harness runs the API without a page supervisor, so the tab reads exactly the
seeded snapshot: deterministic, and the same path a CLI-only box takes.
"""

from __future__ import annotations

import json
import time

import pytest

from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e

AGENT = "merli"


@pytest.fixture
def seeded_pages(lionclaw_home):
    from olano.pages.paths import service_log_path, services_state_path
    from olano.pages.registry import deploy_page, start_page
    from olano.pages.spec import build_spec
    from olano.pages.workspaces import _WS_ROOT_CACHE, agent_workspace_root

    _WS_ROOT_CACHE.clear()
    root = agent_workspace_root(AGENT)
    assert root is not None, "the seeded agent must resolve a workspace"
    for folder in ("site", "web", "crm"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    (root / "site" / "index.html").write_text("<h1>report</h1>")

    start_page(AGENT, "weekly-report", "site", root)
    deploy_page(
        AGENT,
        "dashboard-app",
        "web",
        root,
        build_spec(build="npm ci && npm run build", output="dist"),
        spa=True,
    )
    deploy_page(AGENT, "crm-api", "crm", root, build_spec(start="node server.js"))

    now = time.time()
    state = {
        "version": 1,
        "pages": {
            "dashboard-app": {
                "slug": "dashboard-app",
                "agent": AGENT,
                "kind": "static",
                "state": "live",
                "release": "20261016T120000.000000001Z-abc123",
                "build": {"state": "succeeded", "seconds": 41.2, "finished_at": now - 60},
            },
            "crm-api": {
                "slug": "crm-api",
                "agent": AGENT,
                "kind": "service",
                "state": "failed",
                "detail": "stopped restarting after 5 crashes in a row",
                "restarts": 5,
                "last_exit": 1,
                "last_error": "the server exited with code 1\nError: Cannot find module 'express'",
            },
        },
    }
    path = services_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))
    log = service_log_path(root, "crm-api")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("starting: node server.js\nError: Cannot find module 'express'\n")
    return root


def test_pages_tab_shows_kinds_states_errors_and_logs(page, dashboard, seeded_pages) -> None:
    page.goto(dashboard.base_url.rstrip("/") + f"/agents/{AGENT}?tab=pages")
    wait_for_app_shell(page)

    page.get_by_test_id("page-card-crm-api").wait_for(state="visible", timeout=20_000)

    assert page.get_by_test_id("page-state-weekly-report").inner_text().strip() == "serving"
    assert page.get_by_test_id("page-state-dashboard-app").inner_text().strip() == "live"
    assert page.get_by_test_id("page-state-crm-api").inner_text().strip() == "failed"

    static = page.get_by_test_id("page-card-dashboard-app")
    assert "static build" in static.inner_text()
    assert "Last build succeeded in 41.2s" in static.inner_text()

    crm = page.get_by_test_id("page-card-crm-api")
    assert "server" in crm.inner_text()
    assert "node server.js" in crm.inner_text()
    assert "5 restart(s)" in crm.inner_text()

    crm.get_by_text("Last error").click()
    crm.get_by_text("Cannot find module 'express'", exact=False).first.wait_for(state="visible")

    crm.get_by_role("button", name="Logs").click()
    log = page.get_by_test_id("page-log-crm-api")
    log.wait_for(state="visible", timeout=10_000)
    page.wait_for_function(
        "el => el.textContent.includes('starting: node server.js')",
        arg=log.element_handle(),
        timeout=10_000,
    )

    # Plain pages have no logs to show.
    plain = page.get_by_test_id("page-card-weekly-report")
    assert plain.get_by_role("button", name="Logs").count() == 0

    # Nothing on the tab may push the page sideways on a phone.
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, f"page overflows horizontally by {overflow}px"


def test_pages_tab_stop_button_stops_a_page(page, dashboard, seeded_pages) -> None:
    from olano.pages.registry import get_page

    page.goto(dashboard.base_url.rstrip("/") + f"/agents/{AGENT}?tab=pages")
    wait_for_app_shell(page)
    card = page.get_by_test_id("page-card-weekly-report")
    card.wait_for(state="visible", timeout=20_000)
    card.get_by_role("button", name="Stop").click()
    page.wait_for_function(
        "() => document.querySelector('[data-testid=page-state-weekly-report]')"
        "?.textContent.trim() === 'stopped'",
        timeout=10_000,
    )
    assert get_page("weekly-report")["enabled"] is False
