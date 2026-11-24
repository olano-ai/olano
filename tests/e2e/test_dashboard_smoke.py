"""Smoke tests proving the dashboard renders in a real browser.

Two example checks that exercise what no in-process API test can:

  * the SPA actually boots and React mounts into ``#root``;
  * single-user auth resolves to the ``ready`` phase and the app shell
    (sidebar + /overview route) renders.

Use these as templates for feature-level tests (chat, scheduler, files, …):
take the ``page`` fixture, navigate, interact, and assert on the rendered DOM.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_dashboard_loads_and_react_mounts(page) -> None:
    """The SPA serves, sets its title, and React mounts content into #root."""
    assert page.title() == "Olano"

    root = page.locator("#root")
    root.wait_for(state="attached", timeout=15_000)
    # React rendered *something* — an empty #root means the bundle never booted.
    page.wait_for_function("document.getElementById('root').children.length > 0")
    assert root.inner_html().strip() != ""


def test_single_user_lands_on_overview_shell(page) -> None:
    """Implicit-owner auth → ready phase → app shell + /overview route."""
    # The shell only renders once the auth gate passes (phase=ready). Which
    # element *is* the shell depends on the viewport: the sidebar on desktop,
    # the topbar on mobile, where the sidebar is a closed (hidden) drawer.
    from tests.e2e.conftest import APP_SHELL_SELECTOR, wait_for_app_shell

    wait_for_app_shell(page)
    assert page.locator(APP_SHELL_SELECTOR).first.is_visible()

    # Index route redirects to /overview.
    page.wait_for_url("**/overview", timeout=15_000)
    assert page.url.rstrip("/").endswith("/overview")


# Records the pathname on the first animation frame where the shell is on
# screen — the same moment Playwright's waiters (and a user's thumb) can first
# reach it.
FIRST_SHELL_FRAME = """
window.__firstShellFrame = null;
(function poll() {
  if (window.__firstShellFrame === null && document.querySelector('.app-shell')) {
    window.__firstShellFrame = location.pathname;
  }
  requestAnimationFrame(poll);
})();
"""


def test_the_shell_never_paints_before_the_landing_redirect(page, dashboard) -> None:
    """The root path must resolve to /overview *before* the shell mounts.

    While the shell is on screen at "/" the landing redirect is still in flight:
    React writes the history entry synchronously but delivers the re-render and
    the effects that follow it several scheduler hops later, and input can
    preempt those hops on a busy renderer. Anything the user does in that window
    races them — a tap on the mobile hamburger opened the drawer only for
    Layout's "navigation closes the drawer" effect to shut it again in the same
    task, before the browser ever painted it open (E2E, main, 2026-08-25).

    So the invariant is about the *first painted frame*, not about the URL
    eventually settling: the shell must never exist at "/" at all.
    """
    page.add_init_script(FIRST_SHELL_FRAME)  # applies from the next navigation
    page.goto(dashboard.base_url, wait_until="domcontentloaded")

    from tests.e2e.conftest import wait_for_app_shell

    wait_for_app_shell(page)
    assert page.evaluate("() => window.__firstShellFrame") == "/overview"
