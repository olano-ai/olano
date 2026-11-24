"""E2E: a notification that names something opens it.

"Issue #10 assigned to jamie-rivera" told the operator exactly what happened
and gave them no way to act on it — the row was inert markup, so reaching the
issue meant opening Issues and hunting for #10 by hand. The row is a real
anchor now, which is a DOM-shape fact an in-process API test can't see: that it
carries an ``href`` (so keyboard activation and open-in-new-tab work, not just
a synthetic onClick) and that the href lands on the issue's own detail page.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests.e2e.conftest import wait_for_app_shell

pytestmark = pytest.mark.e2e

DEDUPE_KEY = "e2e-notification-link"
ISSUE_TITLE = "E2E notification deep link"


@pytest.fixture
def seeded_issue(page, dashboard) -> Iterator[int]:
    """A real issue plus the notification that points at it.

    The issue goes in through the app's own API so the link has somewhere real
    to land; the notification is raised in-process because the runtime shares
    this one (the bell has no create endpoint — only producers inside the
    runtime raise rows).
    """
    from olano.issues.delivery import issue_action
    from olano.runtime import notifications

    base = dashboard.base_url.rstrip("/")
    resp = page.request.post(f"{base}/api/issues", data={"title": ISSUE_TITLE, "type": "task"})
    assert resp.status == 200, f"could not seed the issue: {resp.status} {resp.text()}"
    issue_id = int(resp.json()["issue"]["id"])

    notifications.notify(
        kind="issue_assigned",
        level="info",
        title=f"Issue #{issue_id} assigned to e2e",
        body="ai-scout assigned: something to look at",
        dedupe_key=DEDUPE_KEY,
        action=issue_action(issue_id),
    )
    try:
        yield issue_id
    finally:
        # The store is a process-wide singleton shared with the session-scoped
        # dashboard, so leaving the row behind would leak into later tests.
        notifications.resolve(DEDUPE_KEY)


def test_a_notification_opens_the_issue_it_names(page, dashboard, seeded_issue) -> None:
    base = dashboard.base_url.rstrip("/")
    page.goto(f"{base}/overview", wait_until="domcontentloaded")
    wait_for_app_shell(page)

    row = page.locator(f"a.notification-row-main[href='/issues?issue={seeded_issue}']")
    row.wait_for(state="visible", timeout=15_000)
    # An anchor, not a div with a handler — that is what keeps keyboard
    # activation and open-in-new-tab working.
    assert row.evaluate("el => el.tagName") == "A"

    row.click()

    page.wait_for_url(f"**/issues?issue={seeded_issue}", timeout=15_000)
    # …and the issue's own detail page opened, not just the Issues list.
    page.locator(f"h1:has-text('{ISSUE_TITLE}')").wait_for(state="visible", timeout=15_000)
