"""The OAuth consent page (``/oauth/consent``) renders outside the app shell.

Only the "unknown or expired request" branch can be driven without an OAuth
client mid-flow, so that is what this pins: the route is reachable behind the
login, mounts without the sidebar/shell, and says what happened instead of
falling back to the overview.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_consent_page_reports_unknown_request(page, dashboard):
    page.goto(
        dashboard.base_url.rstrip("/") + "/oauth/consent?request=not-a-real-request",
        wait_until="domcontentloaded",
    )
    card = page.locator("[data-testid='oauth-consent-invalid']")
    card.wait_for(state="visible", timeout=20_000)
    assert "unknown or has expired" in card.inner_text()
    # No shell: the page is a single decision, not a dashboard view.
    assert page.locator(".app-shell").count() == 0
