"""E2E: the New-agent catalog picker distinguishes loading / error / empty.

Drives the ``/agents`` → **New agent** modal. The picker used to render an
in-flight *or failed* catalog fetch as "Nothing available." — identical to a box
that genuinely has no archetypes. Combined with a module cache that stored the
failed result permanently, one 401/429/timeout left every picker in the tab
blank until a full page reload, and a box missing the ``olano-agents`` pack
looked like a healthy picker with the wrong contents.

So this asserts the three states are actually distinguishable in the DOM:
populated (bundled archetypes are enough), a failure that shows an error line
plus a working Retry, and — critically — that a failed fetch does not poison the
cache: retrying after the backend recovers repopulates the list without a reload.

Follows ``test_agent_tools_internal.py``: module-scoped dashboard over the mock
gateway, a locally rebound ``page`` fixture running at both viewports.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def picker_dashboard():
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    try:
        with harness.launch_dashboard(gateway=harness.build_mock_gateway()) as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


@pytest.fixture(params=["desktop", "mobile"])
def page(request, picker_dashboard):
    from tests.e2e.conftest import VIEWPORTS

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(
            picker_dashboard.base_url.rstrip("/") + "/agents",
            wait_until="domcontentloaded",
        )
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def _open_new_agent(page) -> None:
    # Scoped to the content area: the sidebar's bot list carries its own
    # "New agent" (the one-question dialog), and on desktop it is the first
    # match in DOM order.
    btn = page.locator("main").get_by_role("button", name=re.compile(r"^New agent")).first
    btn.wait_for(state="visible", timeout=30_000)
    btn.click()
    page.get_by_role("button", name="Expert", exact=True).wait_for(timeout=15_000)


def _select_tab(page, label: str) -> None:
    page.get_by_role("button", name=label, exact=True).click()


def test_expert_and_team_tabs_populate(page) -> None:
    """Both archetype tabs list real, selectable cards.

    The bundled templates alone are enough — this failing means the catalog
    never reached the picker, which is what the production symptom looked like.
    """
    _open_new_agent(page)
    for tab in ("Expert", "Team"):
        _select_tab(page, tab)
        cards = page.locator(".catalog-card")
        cards.first.wait_for(state="visible", timeout=30_000)
        assert cards.count() >= 1, f"{tab} tab rendered no cards"
        # A card the user can actually pick, not a skeleton row.
        assert page.locator(".catalog-card .catalog-name").first.inner_text().strip()


def test_integration_tab_populates(page) -> None:
    _open_new_agent(page)
    _select_tab(page, "Integration")
    cards = page.locator(".catalog-card")
    cards.first.wait_for(state="visible", timeout=30_000)
    assert cards.count() >= 1


def test_empty_text_is_not_shown_while_the_catalog_loads(page) -> None:
    """A slow fetch must render skeletons, never "Nothing available.".

    The route is installed and the page reloaded first, so the app shell's idle
    prefetch is intercepted too — otherwise the catalog is already warm by the
    time the modal opens and there is no in-flight state left to observe.
    """

    # `page.wait_for_timeout`, not `time.sleep`: a sync route handler shares the
    # test's thread, so sleeping blocks Playwright's own loop and the request
    # completes before the modal can even open.
    def slow(route):
        page.wait_for_timeout(4000)
        route.continue_()

    page.route("**/api/archetypes*", slow)
    page.reload(wait_until="domcontentloaded")

    _open_new_agent(page)
    _select_tab(page, "Expert")
    # In flight: the busy list is up and the empty copy is absent.
    page.locator(".catalog-list[aria-busy='true']").wait_for(timeout=15_000)
    assert page.locator(".skeleton-block").count() > 0
    assert page.get_by_text("Nothing available.").count() == 0
    # And it still resolves to real cards once the response lands.
    page.locator(".catalog-card .catalog-name").first.wait_for(timeout=30_000)


def test_failed_fetch_shows_an_error_and_recovers_via_retry(page) -> None:
    """A failure is legible AND recoverable without a page reload.

    The permanent-empty-cache bug lived exactly here: the old loader stored the
    failed read as data, so no later attempt — and no Retry — could repopulate
    the list.

    Failures continue until the test clears the flag, so a background auto-retry
    can't accidentally satisfy the recovery step before Retry is clicked.
    """
    state = {"failing": True}

    def handler(route):
        if state["failing"]:
            route.fulfill(status=503, content_type="application/json", body='{"detail":"boom"}')
        else:
            route.continue_()

    page.route("**/api/archetypes*", handler)
    page.reload(wait_until="domcontentloaded")

    _open_new_agent(page)
    _select_tab(page, "Expert")

    retry = page.get_by_role("button", name="Retry")
    retry.wait_for(state="visible", timeout=30_000)
    # The failure is reported as a failure, not as an empty catalog.
    assert page.get_by_text("Nothing available.").count() == 0
    assert page.locator(".error-text").first.inner_text().strip()

    state["failing"] = False
    retry.click()
    page.locator(".catalog-card .catalog-name").first.wait_for(timeout=30_000)
    assert page.get_by_role("button", name="Retry").count() == 0
