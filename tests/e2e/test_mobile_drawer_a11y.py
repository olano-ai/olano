"""E2E: a closed mobile nav drawer is really closed.

Below the 900px breakpoint the sidebar becomes an off-canvas drawer. It used to
be pushed away with ``transform: translateX(-110%)`` alone — which moves it out
of *sight* but leaves every control inside it in the accessibility tree, in the
tab order, and matching Playwright's ``:visible``. The dashboard renders the
notification bell twice on purpose (sidebar on desktop, topbar on mobile), so
the symptom was two live "Notifications" buttons on a phone, one of them
permanently off-screen: a screen-reader user heard it twice and a keyboard user
tabbed into a drawer they could not see.

The drawer now also carries ``visibility: hidden`` while closed, which removes
it from all three, and flips to ``visible`` when opened — delayed by one
transition on the way out so the slide-out animation still plays.

Desktop is the control: there the sidebar is the real navigation and must stay
visible and focusable at all times.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import SIDEBAR, close_mobile_nav, is_mobile_viewport, open_mobile_nav

pytestmark = pytest.mark.e2e

BELL = "button.notification-bell"


def test_exactly_one_notification_bell_is_reachable(page) -> None:
    """The bell is rendered twice by design; only one may ever be live."""
    wait_for_app_shell(page)

    assert page.locator(f"{BELL}:visible").count() == 1


def test_closed_drawer_is_out_of_the_tab_order(page) -> None:
    """`visibility: hidden` is what makes this true — a transform alone leaves
    the drawer's controls focusable off-screen."""
    wait_for_app_shell(page)

    if not is_mobile_viewport(page):
        pytest.skip("desktop: the sidebar is the navigation, not a drawer")

    # Nothing inside the closed drawer may be focusable.
    focusable = page.locator(f"{SIDEBAR} a, {SIDEBAR} button")
    assert focusable.count() > 0, "sidebar should still be in the DOM, just hidden"
    assert page.locator(f"{SIDEBAR} a:visible, {SIDEBAR} button:visible").count() == 0


def test_opening_the_drawer_makes_its_controls_live_again(page) -> None:
    """Hiding it must not break it: the drawer is the only navigation on mobile."""
    wait_for_app_shell(page)

    if not is_mobile_viewport(page):
        pytest.skip("desktop: no drawer to open")

    open_mobile_nav(page)

    assert page.locator(f"{SIDEBAR} a:visible").count() > 0
    # Both bells are legitimately on screen while the drawer is open — the
    # duplicate this test guards against is the *closed* one.
    close_mobile_nav(page)

    assert page.locator(f"{BELL}:visible").count() == 1


def test_desktop_sidebar_stays_visible(page) -> None:
    """The control case — the fix is scoped to the mobile breakpoint."""
    wait_for_app_shell(page)

    if is_mobile_viewport(page):
        pytest.skip("mobile: covered by the drawer tests above")

    assert page.locator(SIDEBAR).is_visible()
    assert page.locator(f"{SIDEBAR} a:visible").count() > 0
