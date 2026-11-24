"""Passkey and security-key requests are refused in an agent's browser.

A passkey lives on the person's own phone or computer, and a security key in
their hand; neither can reach a browser running on a server. The phone route
(scan a code, then Bluetooth) needs the phone next to the machine running the
browser, so no WebAuthn request an agent's browser makes can ever succeed.

What one does instead is freeze the page. A headed Chromium (the live browser)
answers ``navigator.credentials.get({publicKey})`` with a dialog of its own,
drawn by the browser rather than the page, so the screencast never shows it,
and while it is open the browser drops every click and key aimed at the page:
the person who took control sees GitHub's "Waiting for input from browser
interaction..." and a "More options" button that does nothing. Measured on a
headed Chromium: with the request pending, a click on a button in the page
reported success and the button never saw it.

So every page of an agent's context gets :data:`REFUSE_PASSKEYS_JS` before its
own scripts run: a WebAuthn request is rejected at once with
``NotAllowedError``, exactly what the page would see if a person pressed
Cancel in that dialog, which every site handles by offering its other ways to
sign in (an authenticator app, a text message, a password). Feature checks say
no platform authenticator and no passkey autofill, so a site that asks before
offering a passkey does not offer one. Requests that are not WebAuthn (saved
passwords, federated sign-in) pass through untouched.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

REFUSE_PASSKEYS_JS = """(() => {
  try {
    const refuse = () => Promise.reject(new DOMException(
      "Passkeys and security keys cannot be used in this browser.", "NotAllowedError"));
    const creds = typeof navigator !== "undefined" && navigator.credentials;
    if (creds) {
      const proto = Object.getPrototypeOf(creds);
      for (const name of ["get", "create"]) {
        const real = proto[name];
        if (typeof real !== "function") continue;
        Object.defineProperty(proto, name, {
          configurable: true,
          writable: true,
          value: function (options) {
            if (options && options.publicKey) return refuse();
            return real.call(this, options);
          },
        });
      }
    }
    const PKC = typeof window !== "undefined" && window.PublicKeyCredential;
    if (PKC) {
      const no = () => Promise.resolve(false);
      for (const name of [
        "isUserVerifyingPlatformAuthenticatorAvailable",
        "isConditionalMediationAvailable",
      ]) {
        if (typeof PKC[name] === "function") {
          Object.defineProperty(PKC, name, { configurable: true, writable: true, value: no });
        }
      }
    }
  } catch (e) {}
})();"""


async def refuse_passkeys(context: Any) -> None:
    """Install :data:`REFUSE_PASSKEYS_JS` on every page and frame of *context*.

    Never raises: a context that cannot take an init script browses as before.
    """
    try:
        await context.add_init_script(REFUSE_PASSKEYS_JS)
    except Exception as exc:
        logger.debug("browser: passkey refusal not installed: %s", exc)
