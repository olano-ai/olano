"""Every dashboard route mounts its own content, at both viewports.

This is the automated half of the route checklist that the ``olano-tester``
skill walks by hand; both read :data:`tests.e2e.route_checks.ROUTE_CHECKS`, so
the list cannot drift between them.

It exists because a UI move can silently strand a selector. #228 relocated the
sign-in-links section from Connections to Config and left four checks broken
across both viewports — invisible until someone ran the skill manually. Running
the same list in CI turns that into a failing check on the PR that causes it.
"""

from __future__ import annotations

import pytest

from tests.e2e import harness
from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import ROUTE_CHECKS, SHELL_SELECTOR, check_id

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize(
    ("suffix", "selector"),
    ROUTE_CHECKS,
    ids=[check_id(s, sel) for s, sel in ROUTE_CHECKS],
)
def test_route_renders(
    page,
    request,
    dashboard: harness.RunningDashboard,
    suffix: str,
    selector: str,
) -> None:
    """Navigate to *suffix* and prove *selector* is on screen.

    ``page`` is parametrized over the viewports, so each check runs twice and a
    responsive regression fails on its own id.
    """
    # The viewport name is the `page` fixture's param — needed to resolve
    # "SHELL", which is a different element above and below the breakpoint.
    viewport = request.node.callspec.params["page"]
    resolved = SHELL_SELECTOR[viewport] if selector == "SHELL" else selector

    page.goto(dashboard.base_url.rstrip("/") + suffix, wait_until="domcontentloaded")
    # Routes render behind the auth gate; without this a slow first paint looks
    # like a missing section.
    wait_for_app_shell(page)

    # Deliberately NOT `.first`: Playwright's strict mode makes an *ambiguous*
    # selector fail here, and that is a signal worth keeping. A selector that
    # starts matching two cards no longer proves which one rendered — which is
    # how the duplicated "Sign-in links" heading was caught.
    try:
        page.locator(resolved).wait_for(state="visible", timeout=15_000)
    except Exception as exc:  # re-raised as a test failure with guidance
        pytest.fail(
            f"{suffix} did not render exactly one {resolved!r} at the {viewport} "
            f"viewport.\nIf the feature moved, was renamed, or its heading is now "
            f"shared with another card, update ROUTE_CHECKS in "
            f"tests/e2e/route_checks.py — don't drop the check without a replacement.\n"
            f"{exc}"
        )
