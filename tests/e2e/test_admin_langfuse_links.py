"""E2E: the Langfuse column in Admin -> Users.

What the in-process tests cannot see: that the column renders beside
Interface at both viewports with its help on screen, that an admin's row is a
switch that starts off and writes the account's ``langfuse_links`` flag, and
that a member's row offers no switch at all (trace data is admin-only).
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.e2e

USERS = "section.card:has-text('People who can sign in')"


def _seed() -> tuple[str, str]:
    from olano.users.store import get_user_store

    store = get_user_store()
    tag = uuid.uuid4().hex[:8]
    admin = store.create_user(f"ops-{tag}@example.com", "pw-e2e-123456", name=f"Ops {tag}")
    store.db.update_user(admin.id, is_admin=True)
    member = store.create_user(f"rep-{tag}@example.com", "pw-e2e-123456", name=f"Rep {tag}")
    return admin.id, member.id


def _flag(user_id: str) -> bool:
    from olano.users.store import get_user_store

    user = get_user_store().db.get_user_by_id(user_id)
    assert user is not None
    return user.langfuse_links


def test_the_langfuse_column_is_off_by_default_and_switches(page, dashboard) -> None:
    admin_id, member_id = _seed()
    page.goto(dashboard.base_url.rstrip("/") + "/admin", wait_until="domcontentloaded")
    users = page.locator(USERS)
    users.wait_for(state="visible", timeout=20_000)
    assert users.locator("th", has_text="Langfuse").count() == 1
    assert "off by default for every account" in users.inner_text().lower()

    from olano.users.store import get_user_store

    db = get_user_store().db
    admin_name = db.get_user_by_id(admin_id).name
    member_name = db.get_user_by_id(member_id).name
    admin_row = users.locator("tr", has_text=admin_name)
    member_row = users.locator("tr", has_text=member_name)
    admin_row.wait_for(state="visible", timeout=20_000)

    switch = admin_row.get_by_label("Show Langfuse links to this user")
    assert _flag(admin_id) is False
    switch.click()
    for _ in range(50):
        if _flag(admin_id):
            break
        page.wait_for_timeout(200)
    assert _flag(admin_id) is True

    assert member_row.get_by_test_id("user-langfuse-links").count() == 0
    assert "Admins only" in member_row.inner_text()
    assert _flag(member_id) is False
