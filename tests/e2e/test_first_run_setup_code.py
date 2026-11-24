"""First-run setup code E2E, the self-hosted shape: the installer (setup.sh) put
OLANO_SETUP_CODE into the environment, so the box requires sign-in from the
first request and its create-account screen (BootstrapView) asks for the code.

A stranger who reaches the URL first sees the create-account form and cannot
use it without the code; the owner, who has it from setup.sh, creates the
admin and lands in the app.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from tests.e2e import harness
from tests.e2e.conftest import VIEWPORTS

pytestmark = pytest.mark.e2e

CODE = "WXYZ-2345-6789"
ADMIN = {"email": "owner@olano.test", "password": "olano-e2e-pw"}


@pytest.fixture(autouse=True)
def lionclaw_home():
    """The in-process runtime booted by `coded_dashboard` owns LIONCLAW_HOME."""
    yield


@pytest.fixture(scope="module")
def _code_in_environment():
    mp = pytest.MonkeyPatch()
    mp.setenv("OLANO_SETUP_CODE", CODE)
    mp.setenv("LIONCLAW_SETUP_CODE", CODE)
    try:
        yield
    finally:
        mp.undo()


@pytest.fixture(autouse=True)
def setup_code_not_enforced(monkeypatch, _code_in_environment):
    """Enforcement on, and the installer's code in the environment for every
    test (the root fixture of the same name strips it)."""
    from olano.runtime import setup_code

    monkeypatch.setattr(setup_code, "ENFORCED", True)
    monkeypatch.setenv("OLANO_SETUP_CODE", CODE)
    monkeypatch.setenv("LIONCLAW_SETUP_CODE", CODE)
    setup_code.reset_attempts_for_tests()


@pytest.fixture(scope="module", params=sorted(VIEWPORTS))
def viewport(request) -> str:
    return request.param


@pytest.fixture(scope="module")
def coded_dashboard(viewport, _code_in_environment):
    if not harness.npm_available() or not harness.node_modules_present():
        pytest.skip("dashboard UI tooling missing (npm / node_modules)")
    try:
        with harness.launch_dashboard() as running:
            yield running
    except TimeoutError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"dashboard did not come up: {exc}")


@pytest.fixture
def page(viewport, coded_dashboard):
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium unavailable ({exc})")
        ctx = browser.new_context(**VIEWPORTS[viewport])
        pg = ctx.new_page()
        pg.goto(coded_dashboard.base_url, wait_until="domcontentloaded")
        try:
            yield pg
        finally:
            ctx.close()
            browser.close()


def test_create_account_needs_the_setup_code(page) -> None:
    form = page.locator("form.auth-card")
    form.wait_for(state="visible", timeout=15_000)
    code_field = page.locator("[data-testid='setup-code']")
    code_field.wait_for(state="visible", timeout=10_000)
    assert "setup.sh" in form.inner_text()

    page.locator("form.auth-card input[name='name']").fill("Owner One")
    page.locator("form.auth-card input[type='email']").fill(ADMIN["email"])
    page.locator("form.auth-card input[type='password']").fill(ADMIN["password"])
    submit = page.locator("form.auth-card button.btn.primary")
    # No code typed yet: the button stays disabled.
    assert submit.is_disabled()

    code_field.fill("AAAA-AAAA-AAAA")
    submit.click()
    page.get_by_text("That setup code is not right").wait_for(state="visible", timeout=10_000)
    assert form.is_visible()

    code_field.fill(CODE.lower().replace("-", " "))
    submit.click()
    form.wait_for(state="detached", timeout=15_000)
    page.wait_for_url("**/overview", timeout=15_000)
