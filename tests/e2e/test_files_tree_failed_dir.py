"""Files-tree E2E: a directory that can't be listed must cost ONE request.

Regression for the Files view flickering and hammering the API. The tree's
re-hydration effect re-requested any expanded directory that had no children
and wasn't loading — and a *failed* listing is exactly that state, while the
failure itself writes `nodes` and so re-triggers the effect. One folder the
server refused (a 403 on a `shared/*` symlink projection, a 404 on a directory
deleted since it was expanded) therefore became an unbounded refetch: the
spinner flickered on and off, the runtime's 240-requests/minute budget went in
seconds, and the resulting 429s hit every other call — which surfaces as
"Connection lost", as though the server had restarted.

The backend half (shared projections are browsable now) is pinned by
``tests/test_runtime_api.py``. These drive the browser to pin the frontend
half, which is what turned one bad folder into a flood. The failure is stubbed
at the network layer rather than staged on disk, so the invariant holds for
*any* listing error, not only the one that prompted the fix.
"""

from __future__ import annotations

import pytest

from tests.e2e.route_checks import TREE_ROW, open_agent_workspace

pytestmark = pytest.mark.e2e

# Created by the test itself, never assumed to be in the workspace already: the
# only folder the seeded workspace reliably has is whatever another test made
# (``notes/`` exists only once something has visited /notes, which is why keying
# on it passed locally and failed in CI, purely on test order).
FOLDER = "e2e-failing-dir"
FOLDER_ROW = f"{TREE_ROW}:has-text('{FOLDER}')"
RETRY = "button:has-text('Retry')"
# Long enough for a runaway loop to be unmissable — measured on the unfixed
# build: **~1000 requests in 5s**, i.e. the runtime's 240/min budget gone in
# under two seconds.
#
# Waited with ``page.wait_for_timeout``, never ``time.sleep``: sleeping blocks
# the thread that services Playwright's route handlers, so the stubbed
# responses stop being delivered and the loop under test is throttled by the
# test itself. With ``time.sleep(3)`` this same assertion saw 1–2 requests on
# the *unfixed* build and passed — a test that proves nothing.
SETTLE_MS = 5_000


def _open_files(page, base_url: str) -> None:
    """Create the fixture folder through the app's own API, then open /files.

    Runs before any ``page.route`` stub is registered so the creation call can't
    be caught by it.
    """
    base = base_url.rstrip("/")
    resp = page.request.post(
        f"{base}/api/files/create",
        data={"path": FOLDER, "type": "directory"},
    )
    assert resp.status in (200, 409), f"could not seed {FOLDER}: {resp.status} {resp.text()}"
    page.goto(f"{base}/files", wait_until="domcontentloaded")
    # Every agent starts collapsed; open merli → workspace to reach the folder.
    open_agent_workspace(page)
    page.locator(FOLDER_ROW).first.wait_for(state="visible", timeout=15_000)


def test_failed_directory_listing_is_requested_once(page, dashboard) -> None:
    """Expanding a folder whose listing fails costs one request, not a flood."""
    calls: list[str] = []

    def _fail_that_directory(route):
        calls.append(route.request.url)
        route.fulfill(
            status=403,
            content_type="application/json",
            body='{"detail": "Access denied"}',
        )

    _open_files(page, dashboard.base_url)
    # Only the child listing is stubbed; the root listing still succeeds, so the
    # folder renders and is expandable exactly as in the real bug.
    page.route(f"**/api/files?*path={FOLDER}*", _fail_that_directory)

    page.locator(FOLDER_ROW).first.click()
    # Deliberately not gated on any UI appearing: this must catch a reintroduced
    # loop even if the error row is restyled or removed.
    page.wait_for_timeout(SETTLE_MS)

    assert len(calls) == 1, (
        f"expected one request for the failing directory, got {len(calls)} in "
        f"{SETTLE_MS}ms — the re-hydration effect is retrying on error again"
    )
    # The failure has to be visible: before, the row showed only a spinner, so a
    # permanently-stuck folder looked like it was merely loading.
    assert page.locator("text=Access denied").first.is_visible()
    assert page.locator(RETRY).first.is_visible()


def test_retry_reissues_the_request_and_recovers(page, dashboard) -> None:
    """Retry is the user gesture that re-requests — and a success clears the error."""
    calls: list[str] = []
    state = {"fail": True}

    def _maybe_fail(route):
        calls.append(route.request.url)
        if state["fail"]:
            route.fulfill(
                status=403,
                content_type="application/json",
                body='{"detail": "Access denied"}',
            )
        else:
            route.continue_()

    _open_files(page, dashboard.base_url)
    page.route(f"**/api/files?*path={FOLDER}*", _maybe_fail)

    page.locator(FOLDER_ROW).first.click()
    page.locator(RETRY).first.wait_for(state="visible", timeout=10_000)
    assert len(calls) == 1

    state["fail"] = False
    page.locator(RETRY).first.click()
    page.wait_for_function(
        "() => !document.body.innerText.includes('Access denied')",
        timeout=10_000,
    )
    assert len(calls) == 2, "Retry must reissue exactly one request"

    # Settled again: the recovered directory does not re-request either.
    page.wait_for_timeout(SETTLE_MS)
    assert len(calls) == 2
