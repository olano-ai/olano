"""E2E: after a sign-out, the next person starts at home, not on the previous
person's page.

The app renders at whatever URL the browser is on when a sign-in completes.
One person signing out from ``/chat/<agent>`` left that path in the address
bar, so the next person to sign in on the same browser landed on it and got
"Agent ... is not in your roster" and "Could not load the conversation list"
for a page that was never theirs. A deliberate sign-out (or a sign-in as a
different account) now starts at home; the same person whose session lapsed
still goes back to where they were.

Runs the shared dashboard multi-user by turning the flag on in this test's own
home (the root autouse fixture gives each test one), with two real accounts.
"""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

import pytest

from tests.e2e.route_checks import HAMBURGER, open_mobile_nav

pytestmark = pytest.mark.e2e

PASSWORD = "pw-e2e-123456"


def _accounts() -> tuple[str, str]:
    from olano.config.home import ensure_olano_home, update_home_config
    from olano.users.store import get_user_store

    ensure_olano_home()
    update_home_config({"multi_user_enabled": True})
    store = get_user_store()
    tag = uuid.uuid4().hex[:8]
    admin = store.create_user(f"lead-{tag}@example.com", PASSWORD, name=f"Lead {tag}")
    store.db.update_user(admin.id, is_admin=True)
    member = store.create_user(f"temp-{tag}@example.com", PASSWORD, name=f"Temp {tag}")
    return admin.email, member.email


def _sign_in(page, email: str) -> None:
    page.locator("input[type=email]").fill(email)
    page.locator("input[type=password]").fill(PASSWORD)
    page.locator("button.btn.primary").first.click()


def test_the_next_person_does_not_land_on_the_previous_persons_page(page, dashboard) -> None:
    admin_email, member_email = _accounts()
    base = dashboard.base_url.rstrip("/")
    page.goto(base + "/chat/merli", wait_until="domcontentloaded")
    page.locator("input[type=email]").wait_for(state="visible", timeout=20_000)
    _sign_in(page, admin_email)
    page.locator(".app-shell").wait_for(state="visible", timeout=20_000)
    # The first sign-in on this browser keeps the link it was opened with.
    assert urlparse(page.url).path == "/chat/merli", page.url

    if page.locator(HAMBURGER).is_visible():
        open_mobile_nav(page)
    wrap = page.locator(".profile-menu-wrap")
    wrap.locator("button").first.click()
    page.get_by_role("button", name="Sign out").or_(
        page.get_by_role("menuitem", name="Sign out")
    ).first.click()

    page.locator("input[type=email]").wait_for(state="visible", timeout=20_000)
    _sign_in(page, member_email)
    page.locator(".app-shell").wait_for(state="visible", timeout=20_000)
    page.wait_for_timeout(1500)  # long enough for a stale view's toasts to fire
    assert urlparse(page.url).path != "/chat/merli", page.url
    body = page.locator("body").inner_text()
    assert "not in your roster" not in body
    assert "Could not load the conversation list" not in body
