"""E2E: the simple shell replaces the dashboard, and only when asked.

``ui_mode`` picks which front end a human gets. Two properties matter enough to
drive a real browser at both breakpoints:

**Nothing changes unless you change it.** The default is ``advanced`` for every
account, so an upgraded deployment must render exactly the dashboard it always
did. The first test here is that control: with the key unset, the operator
console mounts and the simple shell is nowhere on the page.

**The two shells are disjoint.** Simple mode must not leave a stray piece of
the dashboard behind — no sidebar, no route nav — and must not borrow the
dashboard's own selectors, which three other suites drive. Playwright runs in
strict mode, so a shared selector would surface as an ambiguity failure in an
unrelated module rather than here.

Single-user is the harness's posture, so the switch under test is the
``ui_mode`` root key in ``config.yaml`` — the fallback ``resolve_ui_mode()``
uses when there is no users table to read a column from.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import wait_for_app_shell
from tests.e2e.route_checks import (
    SIDEBAR,
    SIMPLE_BOT_RAIL,
    SIMPLE_COMPOSER,
    SIMPLE_ROOT,
)

pytestmark = pytest.mark.e2e


def _set_ui_mode(value: str | None) -> None:
    """Write (or clear) the home-config shell selector for this test's home."""
    from olano.config.home import ensure_olano_home, update_home_config

    ensure_olano_home()
    update_home_config({"ui_mode": value or "advanced"})


def test_default_still_renders_the_dashboard(page) -> None:
    """The control. An install that never sets ui_mode is untouched."""
    _set_ui_mode(None)
    page.reload()
    wait_for_app_shell(page)
    assert page.locator(SIMPLE_ROOT).count() == 0
    # The advanced shell's own markers are present exactly as before.
    assert page.locator(".app-shell").count() == 1


def test_simple_mode_mounts_its_own_shell(page) -> None:
    _set_ui_mode("simple")
    page.reload()
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)
    page.wait_for_selector(SIMPLE_BOT_RAIL, timeout=20_000)
    page.wait_for_selector(SIMPLE_COMPOSER, timeout=20_000)


def test_simple_mode_leaves_no_dashboard_behind(page) -> None:
    """No sidebar, no route nav — the dashboard tree must not mount at all."""
    _set_ui_mode("simple")
    page.reload()
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)
    # The exact selectors the advanced-shell suites drive. Reusing either here
    # would make those suites ambiguous under Playwright's strict mode.
    assert page.locator(SIDEBAR).count() == 0
    assert page.locator(".app-shell").count() == 0


def test_simple_mode_hides_the_operator_chrome(page) -> None:
    """The point of the mode: none of the developer-facing surfaces render."""
    _set_ui_mode("simple")
    page.reload()
    page.wait_for_selector(SIMPLE_ROOT, timeout=20_000)
    for selector in (".tool-card", ".tool-activity", ".context-card", ".mcp-indicator"):
        assert page.locator(selector).count() == 0, selector
