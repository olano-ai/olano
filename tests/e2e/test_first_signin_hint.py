"""First sign-in on a just-deployed managed instance: the sign-in screen says
where the temporary password is (the welcome email, or the deployment's
Security tab in the cloud console), and stops saying it once the owner has
replaced that password.

Real path end to end: the runtime's own first-boot provisioning from a
temporary password, a managed-cloud installation manifest, the real API and the
real sign-in page, at both viewports. It uses its own module-scoped dashboard,
like test_onboarding_wizard, because provisioning turns the home multi-user and
the shared session dashboard must stay single-user for every other test.
"""

from __future__ import annotations

import json

import httpx
import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness
from tests.e2e.conftest import VIEWPORTS

pytestmark = pytest.mark.e2e

DEPLOYMENT_ID = "0b6f7c1e-3a2d-4c8e-9f10-2d5b7e8a9c41"
CONSOLE_LINK = f"https://cloud.olano.ai/app/deployments/{DEPLOYMENT_ID}?tab=security"
OWNER_EMAIL = "owner@olano.test"
TEMP_PASSWORD = "temp-pass-e2e-7f2c"
NOTE = "[data-testid='first-signin-hint']"


@pytest.fixture(autouse=True)
def lionclaw_home():
    """Override the root autouse home swap (tests/conftest.py), as
    test_onboarding_wizard does: the dashboard below owns LIONCLAW_HOME for the
    whole module, and a per-test swap would point the live server at an empty
    users DB mid-test."""
    yield


@pytest.fixture(scope="module")
def provisioned_dashboard(tmp_path_factory):
    """A dashboard whose home the runtime provisioned itself, the way a managed
    box's first `olano serve` does: admin = the owner's email + the temporary
    password from the environment, then the cloud manifest is in place."""
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    from olano.deployment import resolver
    from olano.runtime.first_boot import first_boot_provisioning

    manifest = tmp_path_factory.mktemp("installation") / "installation.json"
    env = pytest.MonkeyPatch()
    try:
        with harness.launch_dashboard() as running:
            env.setenv("LIONCLAW_ADMIN_EMAIL", OWNER_EMAIL)
            env.setenv("LIONCLAW_ADMIN_PASSWORD", TEMP_PASSWORD)
            env.setenv("LIONCLAW_DEFAULT_AGENT", "merli")  # already seeded: no install
            env.setenv("LIONCLAW_INSTALLATION_FILE", str(manifest))
            result = first_boot_provisioning()
            assert result is not None and dict(result.steps)["admin"] == "created"
            # After provisioning, never before: a managed manifest with no
            # settings artifact yet is the quarantine posture, whose settle wait
            # provisioning would otherwise sit out.
            manifest.write_text(
                json.dumps({"managed": True, "mode": "cloud", "deployment_id": DEPLOYMENT_ID})
            )
            resolver.invalidate()
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")
    finally:
        env.undo()
        resolver.invalidate()


@pytest.fixture(params=sorted(VIEWPORTS))
def page(request, provisioned_dashboard):
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[request.param])
        pg = ctx.new_page()
        pg.goto(provisioned_dashboard.base_url, wait_until="domcontentloaded")
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def _assert_note_points_at_the_console(page) -> None:
    note = page.locator(NOTE)
    note.wait_for(state="visible", timeout=30_000)
    link = note.get_by_role("link")
    assert link.get_attribute("href") == CONSOLE_LINK
    # The visible text is the address itself, and it opens beside the form.
    assert link.inner_text().strip() == CONSOLE_LINK
    assert link.get_attribute("target") == "_blank"
    # A 36-character id is one unbreakable word; it must wrap inside the card,
    # never push the page wider than a phone.
    width = page.viewport_size["width"]
    box = note.bounding_box()
    assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= width


def test_a_new_owner_is_told_where_the_temporary_password_is(page) -> None:
    _assert_note_points_at_the_console(page)
    card = page.locator("form.auth-card")
    assert "Welcome to your new Olano instance." in card.inner_text()
    assert "Welcome back" not in card.inner_text()
    assert "the email we just sent you" in page.locator(NOTE).inner_text()

    # "Forgot password?" points at the same place instead of at an admin who
    # does not exist yet.
    page.get_by_role("button", name="Forgot password?").click()
    _assert_note_points_at_the_console(page)
    assert "handled by your admin" not in page.locator(".auth-card").inner_text()


def test_the_ordinary_form_returns_once_the_owner_chose_a_password(
    page, provisioned_dashboard
) -> None:
    api = provisioned_dashboard.runtime_url.rstrip("/")
    with httpx.Client(base_url=api, timeout=15.0) as owner:
        # Idempotent across the two viewport runs: the first one claims.
        if owner.get("/api/auth/sign-in-hint").json()["temporary_password"] is not None:
            login = owner.post(
                "/api/auth/login", json={"email": OWNER_EMAIL, "password": TEMP_PASSWORD}
            )
            assert login.status_code == 200, login.text
            claim = owner.post(
                "/api/auth/claim", json={"name": "Dana Owner", "password": "owner-own-pw-1"}
            )
            assert claim.status_code == 200, claim.text
        assert owner.get("/api/auth/sign-in-hint").json() == {"temporary_password": None}

    page.reload(wait_until="domcontentloaded")
    card = page.locator("form.auth-card")
    card.wait_for(state="visible", timeout=30_000)
    page.get_by_text("Welcome back to Olano.").wait_for(timeout=15_000)
    # The hint request has had time to answer; the note must still be absent.
    page.wait_for_timeout(1500)
    assert page.locator(NOTE).count() == 0
