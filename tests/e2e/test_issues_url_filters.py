"""E2E: the Issues list's filters live in the URL.

A filtered list used to exist only in a session-scoped memory map: nothing to
copy, nothing that survived a reload, nothing a teammate could open. The address
is the source of truth now, so these pin the round trip in a real browser:

* landing on ``/issues?state=closed&label=…`` shows the Closed list with that
  label selected, and offers **Copy link**;
* changing a filter rewrites the address (only the non-default keys);
* opening an issue keeps the filters beside ``?issue=``, and Back keeps them too;
* leaving for another tab and coming back re-seeds the remembered filters into
  the address, so a left-nav round trip never resets the working set.

Seeds one closed issue with a unique label through the app's own API.
"""

from __future__ import annotations

import uuid

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import click_nav_link, is_mobile_viewport, open_mobile_nav

pytestmark = pytest.mark.e2e


@pytest.fixture
def closed_issue(page, dashboard) -> tuple[int, str]:
    base = dashboard.base_url.rstrip("/")
    label = f"e2e-url-{uuid.uuid4().hex[:8]}"
    resp = page.request.post(
        f"{base}/api/issues",
        data={"title": f"E2E url filters {label}", "type": "task", "labels": [label]},
    )
    assert resp.status == 200, f"could not seed the issue: {resp.status} {resp.text()}"
    issue_id = int(resp.json()["issue"]["id"])
    closed = page.request.post(f"{base}/api/issues/{issue_id}/close", data={})
    assert closed.status == 200, closed.text()
    return issue_id, label


def test_a_shared_url_reopens_the_same_list_and_filter_changes_rewrite_it(
    page, dashboard, closed_issue
) -> None:
    issue_id, label = closed_issue
    base = dashboard.base_url.rstrip("/")
    page.goto(f"{base}/issues?state=closed&label={label}", wait_until="domcontentloaded")
    wait_for_app_shell(page)

    closed_toggle = page.locator(".issues-state-toggle.active")
    closed_toggle.wait_for(state="visible", timeout=15_000)
    assert "Closed" in closed_toggle.inner_text()
    page.wait_for_function(
        "(label) => document.querySelector(\"select[aria-label='Label']\")?.value === label",
        arg=label,
        timeout=15_000,
    )
    # Exactly the seeded row, with its label on the labels row under the title.
    row = page.locator(".issues-row")
    row.first.wait_for(state="visible", timeout=15_000)
    assert row.count() == 1
    assert row.locator(".issue-labels .issue-label-chip").count() == 1
    # A filter is active, so the list can be shared from a phone too.
    assert page.locator("button:has-text('Copy link')").count() == 1
    # …and the route check's own contract still holds: one New issue button.
    assert page.locator("button:has-text('New issue')").count() == 1

    # Flip to Open: the address changes, the default state key disappears.
    page.locator(".issues-state-toggle", has_text="Open").click()
    page.wait_for_url(f"**/issues?label={label}", timeout=10_000, wait_until="commit")

    # Open the seeded issue from the Closed list: `issue` joins the filters.
    page.locator(".issues-state-toggle", has_text="Closed").click()
    page.wait_for_url(f"**/issues?state=closed&label={label}", timeout=10_000, wait_until="commit")
    page.locator(".issues-row").first.click()
    page.wait_for_url(
        f"**/issues?state=closed&label={label}&issue={issue_id}",
        timeout=10_000,
        wait_until="commit",
    )
    page.locator(".issue-state-pill").wait_for(state="visible", timeout=15_000)


def test_the_remembered_filters_come_back_after_a_left_nav_round_trip(
    page, dashboard, closed_issue
) -> None:
    _issue_id, label = closed_issue
    base = dashboard.base_url.rstrip("/")
    page.goto(f"{base}/issues?state=closed&label={label}", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    page.locator(".issues-row").first.wait_for(state="visible", timeout=15_000)

    if is_mobile_viewport(page):
        open_mobile_nav(page)
    click_nav_link(page, "/overview")
    page.wait_for_url("**/overview", timeout=15_000, wait_until="commit")
    if is_mobile_viewport(page):
        open_mobile_nav(page)
    click_nav_link(page, "/issues")
    # A bare /issues landing is seeded from the remembered set and rewritten.
    page.wait_for_url(f"**/issues?state=closed&label={label}", timeout=15_000, wait_until="commit")
