"""Twilio webhook signature validation: one copy, two adapters.

``X-Twilio-Signature`` is HMAC-SHA1 over the full request URL with every POST
parameter appended (sorted by name, name and value concatenated with no
delimiter), keyed by the account auth token. Twilio documents it at
https://www.twilio.com/docs/usage/security.

Two adapters speak this protocol, ``sms.py`` (Programmable Messaging) and
``whatsapp_twilio.py`` (Conversations), and a second copy of a crypto check is
exactly the kind of drift that turns into one surface validating and the other
not. Both import from here.

**The default-port variant is load-bearing.** Twilio signs the URL it was
configured with, and a proxy in front of the runtime may present that URL with
or without the scheme's default port (``:443`` on https). Checking only the
literal configured string rejects a genuine Twilio request whenever the two
spellings disagree, which reads as "Twilio is sending bad signatures" rather
than as a URL mismatch. :func:`validate_signature` tries both.

**Only form-encoded bodies are handled here, deliberately.** Twilio signs a
JSON-bodied webhook differently (the body's SHA-256 rides a ``bodySHA256``
query parameter and the POST fields are *not* folded in). Both adapters
receive ``application/x-www-form-urlencoded`` (Conversations documents that
every webhook request carries that content type), so the JSON variant has no
call site. Adding one means adding the other algorithm, not loosening this one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.parse

__all__ = ["compute_signature", "default_port_variant", "validate_signature"]


def compute_signature(auth_token: str, url: str, post_params: dict[str, str]) -> str:
    """Return the base64 HMAC-SHA1 Twilio would send for this request."""
    data_to_sign = url
    for key in sorted(post_params.keys()):
        data_to_sign += key + post_params[key]
    mac = hmac.new(
        auth_token.encode("utf-8"),
        data_to_sign.encode("utf-8"),
        hashlib.sha1,
    )
    return base64.b64encode(mac.digest()).decode("utf-8")


def default_port_variant(url: str) -> str | None:
    """Return *url* with the scheme's default port toggled, or ``None``.

    Only default ports move (443 for https, 80 for http): a non-standard port
    is part of the URL Twilio was given and must never be rewritten, or the
    check would accept a signature computed for a different endpoint.
    """
    parsed = urllib.parse.urlparse(url)
    default_ports = {"https": 443, "http": 80}
    default_port = default_ports.get(parsed.scheme)
    if default_port is None:
        return None

    if parsed.port == default_port:
        # Explicit default port → strip it.
        return urllib.parse.urlunparse(
            (
                parsed.scheme,
                parsed.hostname or "",
                parsed.path,
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        )
    if parsed.port is None:
        # No port → add the default.
        netloc = f"{parsed.hostname}:{default_port}"
        return urllib.parse.urlunparse(
            (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
        )
    return None


def validate_signature(
    auth_token: str,
    url: str,
    post_params: dict[str, str],
    signature: str,
) -> bool:
    """True when *signature* matches either spelling of *url*.

    Constant-time comparison; an empty signature never validates.
    """
    if not signature or not auth_token:
        return False
    if hmac.compare_digest(compute_signature(auth_token, url, post_params), signature):
        return True
    variant = default_port_variant(url)
    return bool(
        variant
        and hmac.compare_digest(compute_signature(auth_token, variant, post_params), signature)
    )
