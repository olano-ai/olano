"""E2E: a person invited on the Admin page can be granted access at once.

The Users section and the Agent access section load their data separately,
and inviting someone used to refresh only the first: the Grant access dialog
kept the roster it loaded with the page, so the person just invited was not
in it until a reload.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.e2e


def test_the_grant_dialog_lists_a_person_invited_a_moment_ago(page, dashboard) -> None:
    tag = uuid.uuid4().hex[:6]
    name = f"Nadia {tag}"
    page.goto(dashboard.base_url.rstrip("/") + "/admin", wait_until="domcontentloaded")
    form = page.locator("form.admin-form-row")
    form.wait_for(state="visible", timeout=20_000)
    page.get_by_role("heading", name="Agent access").wait_for(timeout=20_000)

    form.get_by_placeholder("Name", exact=True).fill(name)
    form.get_by_placeholder("Email", exact=True).fill(f"nadia-{tag}@example.com")
    form.get_by_role("button", name="Send invite").click()
    # Inviting shows the link to share; close it to get back to the page.
    done = page.get_by_role("button", name="Done", exact=True)
    done.wait_for(state="visible", timeout=10_000)
    done.click()

    page.get_by_role("button", name="Grant access").first.click()
    grant = page.get_by_role("dialog", name="Grant access")
    grant.wait_for(state="visible", timeout=10_000)
    grant.get_by_text(name).first.wait_for(state="visible", timeout=10_000)
