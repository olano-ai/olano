"""Passkey requests are refused in an agent's browser (olano/browser/passkeys.py).

In the live (headed) browser a WebAuthn request opens a dialog drawn by the
browser itself, which the screencast never shows and which swallows every click
aimed at the page, so a person who took control could not reach a site's
"More options". These run the init script on a real Chromium: a request is
refused at once with the error a person pressing Cancel produces, and nothing
that is not WebAuthn is touched. Skipped when playwright or its Chromium build
isn't installed.
"""

from __future__ import annotations

from functools import lru_cache

import pytest


@lru_cache(maxsize=1)
def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="playwright/chromium not installed"
)

_GET = """() => navigator.credentials.get({publicKey: {challenge: new Uint8Array(32), timeout: 60000}})
  .then(() => "resolved", (e) => e.name)"""
_CREATE = """() => navigator.credentials.create({publicKey: {
    challenge: new Uint8Array(32), rp: {name: "x"},
    user: {id: new Uint8Array(8), name: "u", displayName: "u"},
    pubKeyCredParams: [{type: "public-key", alg: -7}]}})
  .then(() => "resolved", (e) => e.name)"""


@pytest.fixture
async def page():
    from playwright.async_api import async_playwright

    from olano.browser.passkeys import refuse_passkeys

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        await refuse_passkeys(context)
        page = await context.new_page()
        # A secure context, which is the only place navigator.credentials exists.
        await page.route("https://example.test/", lambda r: r.fulfill(body="<p>hi</p>"))
        await page.goto("https://example.test/")
        try:
            yield page
        finally:
            await browser.close()


async def test_a_passkey_sign_in_is_refused_as_if_cancelled(page):
    assert await page.evaluate(_GET) == "NotAllowedError"


async def test_registering_a_passkey_is_refused_too(page):
    assert await page.evaluate(_CREATE) == "NotAllowedError"


async def test_sites_are_told_there_is_no_passkey_to_offer(page):
    platform = await page.evaluate(
        "() => PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable()"
    )
    autofill = await page.evaluate("() => PublicKeyCredential.isConditionalMediationAvailable()")
    assert platform is False
    assert autofill is False


async def test_requests_that_are_not_webauthn_pass_through(page):
    # A saved-password lookup reaches the real implementation (which answers
    # null here) instead of the refusal.
    result = await page.evaluate(
        "() => navigator.credentials.get({password: true}).then((c) => c === null ? 'null' : 'other', (e) => e.name)"
    )
    assert result != "NotAllowedError"


async def test_an_agent_context_gets_the_refusal():
    """``sessions._open`` installs it on every context it builds."""
    import inspect

    from olano.browser import sessions

    assert "await refuse_passkeys(context)" in inspect.getsource(sessions._open)
