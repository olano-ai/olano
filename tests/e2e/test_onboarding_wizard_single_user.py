"""Single-user onboarding wizard E2E: the wizard gates EVERY fresh admin,
including the implicit owner of a box with no accounts (manual installs, or a
managed box mid-provisioning). There the profile step's account half does not
*claim* an account — it *creates* the admin sign-in via /api/auth/bootstrap
(email + name + password), flipping multi-user mode on and signing the browser
in, after which the vault half and the rest of the tour continue as the new
admin.

The dashboard fixture is module-scoped **per viewport**: completing the
bootstrap step flips the throwaway LIONCLAW_HOME into multi-user mode, so the
mobile run needs a fresh single-user home rather than the one the desktop run
just secured (and neither may leak into the shared session fixture).
"""

from __future__ import annotations

import json
import urllib.request

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness
from tests.e2e.conftest import VIEWPORTS

pytestmark = pytest.mark.e2e

ADMIN = {"email": "owner@olano.test", "password": "olano-e2e-pw"}


@pytest.fixture(autouse=True)
def lionclaw_home():
    """Override the root autouse home-swap (tests/conftest.py) — the in-process
    runtime booted by `single_user_dashboard` owns LIONCLAW_HOME here."""
    yield


@pytest.fixture(autouse=True)
def setup_code_not_enforced(monkeypatch):
    """Override the suite-wide off switch (tests/conftest.py): this box is an
    unmanaged one with no accounts, so creating the admin needs the one-time
    setup code, exactly as a real self-hosted install does. No code in the
    environment, so the runtime generates one under the harness home."""
    from olano.runtime import setup_code

    monkeypatch.setattr(setup_code, "ENFORCED", True)
    monkeypatch.delenv("OLANO_SETUP_CODE", raising=False)
    monkeypatch.delenv("LIONCLAW_SETUP_CODE", raising=False)
    setup_code.reset_attempts_for_tests()


# Module-scoped param: every test in this file runs once per viewport, against
# a dashboard (and home) launched fresh for that viewport.
@pytest.fixture(scope="module", params=sorted(VIEWPORTS))
def viewport(request) -> str:
    return request.param


@pytest.fixture(scope="module")
def single_user_dashboard(viewport):
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    try:
        with harness.launch_dashboard() as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


def _rearm_onboarding(runtime_url: str) -> None:
    """DELETE /api/onboarding — in single-user mode the implicit owner is an
    admin, so no session cookie is needed."""
    req = urllib.request.Request(runtime_url.rstrip("/") + "/api/onboarding", method="DELETE")
    with urllib.request.urlopen(req, timeout=10) as res:
        assert res.status == 200


@pytest.fixture
def page(viewport, single_user_dashboard):
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[viewport])
        pg = ctx.new_page()
        pg.goto(single_user_dashboard.base_url, wait_until="domcontentloaded")
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def test_wizard_gates_the_implicit_owner_and_creates_the_admin(page, single_user_dashboard) -> None:
    """No sign-in, no accounts: the wizard appears immediately, its profile
    step's account half is the create-admin (bootstrap) form, and submitting
    it creates the account without losing the tour's place. The vault half is
    stubbed (the harness home has no vault to rekey; the real route is covered
    by tests/runtime/test_credential_rotation_api.py). Skipping the rest of
    the tour then lands on the app, signed in as that admin."""
    _rearm_onboarding(single_user_dashboard.runtime_url)
    page.route(
        "**/api/vault/status",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"unlocked": True, "has_vault": True}),
        ),
    )
    page.route(
        "**/api/vault/rekey",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"ok": True})
        ),
    )
    page.reload(wait_until="domcontentloaded")

    # No login form — the implicit owner lands straight in the wizard.
    page.locator(".ob-root").wait_for(state="visible", timeout=15_000)
    page.locator(".ob-welcome .btn.primary").click()

    # No accounts ⇒ the account half creates the admin sign-in: a name and an
    # email field, never a "current password" one.
    email = page.locator(".ob-form input[type='email']")
    email.wait_for(state="visible", timeout=10_000)
    assert page.locator(".ob-form input[autocomplete='current-password']").count() == 0

    page.locator(".ob-form input[name='name']").fill("Owner One")
    assert "owner-one" in page.locator("[data-testid='handle-preview']").inner_text()
    email.fill(ADMIN["email"])
    pw_fields = page.locator(".ob-card input[type='password']")
    assert pw_fields.count() == 4  # password + confirm, passphrase + confirm
    pw_fields.nth(0).fill(ADMIN["password"])
    pw_fields.nth(1).fill(ADMIN["password"])
    pw_fields.nth(2).fill("my-own-vault-pass")
    pw_fields.nth(3).fill("my-own-vault-pass")

    # A box with no accounts asks for the one-time setup code (printed by
    # setup.sh and in the runtime's log). A wrong one is refused with an
    # explanation of where the right one is, and nothing is created.
    from olano.runtime import setup_code

    code_field = page.locator("[data-testid='setup-code']")
    code_field.wait_for(state="visible", timeout=10_000)
    assert "docker compose logs olano" in page.get_by_test_id("profile-account-form").inner_text()
    code_field.fill("AAAA-AAAA-AAAA")
    page.get_by_role("button", name="Save and continue").click()
    page.get_by_text("That setup code is not right").wait_for(state="visible", timeout=10_000)
    assert page.locator(".ob-ok").count() == 0

    code_field.fill(setup_code.expected_code(announce=False).lower())
    page.get_by_role("button", name="Save and continue").click()

    # Bootstrap succeeded and the vault half followed: both halves settle on
    # their success lines and the wizard keeps its place (the auth context
    # refreshed to the real admin).
    oks = page.locator(".ob-ok")
    oks.nth(1).wait_for(state="visible", timeout=20_000)
    assert ADMIN["email"] in oks.nth(0).inner_text()
    assert page.locator(".ob-root").is_visible()

    # Continue → the AI provider step (the self-hosted key form): we are
    # signed in, tour intact.
    page.locator(".ob-actions .btn.primary").click()
    page.get_by_text("Connect an AI provider").wait_for(state="visible", timeout=15_000)

    # Back to the welcome panel (its skip button is visible at BOTH viewports,
    # unlike the rail's) and skip the rest of the tour → app shell renders.
    page.locator(".ob-actions .btn.ghost").first.click()  # provider → profile
    page.locator(".ob-ok").first.wait_for(state="visible", timeout=10_000)
    page.locator(".ob-actions .btn.ghost").first.click()  # profile → welcome
    page.locator(".ob-welcome").wait_for(state="visible", timeout=10_000)
    page.locator(".ob-welcome .btn.ghost").click()

    page.locator(".ob-root").wait_for(state="detached", timeout=15_000)
    page.wait_for_url("**/overview", timeout=15_000)


def test_completed_wizard_stays_gone_and_login_gate_is_armed(page, single_user_dashboard) -> None:
    """After the previous test bootstrapped the admin and completed the tour,
    a NEW browser context must hit the login gate (multi-user is on now —
    anonymous visitors no longer get the implicit owner), and signing in with
    the created account lands on the app with no wizard."""
    login_email = page.locator("form.auth-card input[type='email']")
    login_email.wait_for(state="visible", timeout=15_000)
    login_email.fill(ADMIN["email"])
    page.locator("form.auth-card input[type='password']").fill(ADMIN["password"])
    page.locator("form.auth-card button.btn.primary").click()

    page.wait_for_url("**/overview", timeout=15_000)
    assert page.locator(".ob-root").count() == 0
