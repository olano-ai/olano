"""E2E: the setup and admin surfaces live in the profile menu, not the nav.

Providers, Connections and Tools (the **Setup** group) and Vault, Logs, Config
and Admin (the admin-only **Administration** group) are not places anyone visits
during the day, so they moved from the sidebar (Admin was pinned under More, the
rest sat inside it) into the menu behind the signed-in user's button at the
bottom-left, next to theme, language and the shell switch. Three DOM-shape facts
only a browser can see:

* the menu holds exactly one link to each, in two labelled groups (a third,
  **Help and docs**, holds the one link that leaves the app: the user guide,
  opened at the page for the current tab), and the nav groups hold none, even
  with **More** expanded, which is where they used to be;
* the rows are real anchors that navigate (so keyboard activation and
  open-in-new-tab work), and on a phone the tap also closes the nav drawer;
* while one of those pages is open the profile button carries the active state,
  since nothing in the nav groups is lit.

The harness signs in as the admin, so the section is present; a member would see
no section at all rather than rows that 403.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import HAMBURGER, MORE_HEADER, SIDEBAR, open_mobile_nav

pytestmark = pytest.mark.e2e

PROFILE_WRAP = ".profile-menu-wrap"
PROFILE_BTN = f"{PROFILE_WRAP} button.profile-btn"
MENU = f"{PROFILE_WRAP} .menu"
SETUP = ["/providers", "/connections", "/tools"]
ADMIN = ["/vault", "/logs", "/config", "/admin"]


def _open_nav_and_menu(page) -> None:
    wait_for_app_shell(page)
    if page.locator(HAMBURGER).is_visible():
        open_mobile_nav(page)
    page.locator(PROFILE_BTN).click()
    page.locator(MENU).wait_for(state="visible", timeout=10_000)


def test_setup_and_admin_surfaces_are_menu_rows_and_not_nav_items(page, dashboard) -> None:
    page.goto(dashboard.base_url.rstrip("/") + "/overview", wait_until="domcontentloaded")
    wait_for_app_shell(page)
    if page.locator(HAMBURGER).is_visible():
        open_mobile_nav(page)

    # Not in the nav groups — including under More, which ships collapsed and
    # renders its links only while open. Checked BEFORE the profile menu opens:
    # the menu opens upward over the nav and would intercept the click.
    nav = page.locator(f"{SIDEBAR} nav.sidebar-nav")
    more = page.locator(f"{SIDEBAR} .nav-group-more")
    # The collapse state is a server-side pref; a click that lands before the
    # prefs fetch settles can be overwritten by it, so expand with a retry.
    for _ in range(3):
        header = page.locator(f"{MORE_HEADER}.collapsed")
        if header.count() == 0:
            break
        header.first.click()
        try:
            more.wait_for(state="visible", timeout=4_000)
            break
        except Exception:
            continue
    more.wait_for(state="visible", timeout=10_000)
    for href in SETUP + ADMIN:
        assert nav.locator(f"a[href='{href}']").count() == 0, href

    page.locator(PROFILE_BTN).click()
    menu = page.locator(MENU)
    menu.wait_for(state="visible", timeout=10_000)
    for href in SETUP + ADMIN:
        assert menu.locator(f"a[href='{href}']").count() == 1, href
    # Three labelled groups; the labels are not .menu-item rows
    # (test_account_menu.py counts those). Setup and Administration hold the
    # surfaces; Help and docs holds the user guide link.
    labels = menu.locator(".menu-section-label")
    assert labels.count() == 3
    # Rendered uppercase by CSS; the words are what matter.
    assert [t.strip().lower() for t in labels.all_inner_texts()] == [
        "setup",
        "administration",
        "help and docs",
    ]
    # Setup rows come before the admin rows, in the order the table names, and
    # the docs link is the only row that leaves the app: it opens the guide's
    # page for the tab we are on (/overview), in a new tab.
    hrefs = menu.evaluate(
        "el => [...el.querySelectorAll('a.menu-link')].map(a => a.getAttribute('href'))"
    )
    docs_href = "https://docs.olano.ai/latest/dashboard/overview/"
    assert hrefs == SETUP + ADMIN + [docs_href], hrefs
    docs_row = menu.locator(f"a.menu-link[href='{docs_href}']")
    assert docs_row.get_attribute("target") == "_blank"
    assert docs_row.get_attribute("rel") == "noopener noreferrer"


def test_admin_row_navigates_closes_the_drawer_and_lights_the_profile_button(
    page, dashboard
) -> None:
    page.goto(dashboard.base_url.rstrip("/") + "/overview", wait_until="domcontentloaded")
    _open_nav_and_menu(page)
    mobile = page.locator(HAMBURGER).is_visible()

    page.locator(f"{MENU} a[href='/admin']").click()
    page.wait_for_url("**/admin", timeout=15_000)
    # The menu closed itself (its own open state, not just the drawer's).
    page.locator(MENU).wait_for(state="detached", timeout=5_000)
    if mobile:
        page.wait_for_selector(".app-shell.mobile-nav-open", state="detached", timeout=5_000)

    # Nothing in the nav groups is the current page; the profile button is.
    # Waited for, not sampled: the router commits a navigation to a lazily
    # loaded route as a transition, so the location — and with it this class —
    # lands only once the Admin chunk has resolved, a beat after the URL moved.
    if mobile:
        open_mobile_nav(page)
    btn = page.locator(PROFILE_BTN)
    page.locator(f"{PROFILE_BTN}.is-active").wait_for(state="attached", timeout=15_000)
    assert btn.get_attribute("aria-current") == "location"
    btn.click()
    row = page.locator(f"{MENU} a[href='/admin']")
    row.wait_for(state="visible", timeout=10_000)
    assert "active" in (row.get_attribute("class") or "")
