"""Public inbound route for the WhatsApp over Twilio connector.

Registered from ``create_api`` (api.py). Twilio delivers every customer message
as an HTTP POST, so the connector needs a public HTTPS address, and it uses the
one the deployment already has::

    https://<dashboard host>/api/twilio/whatsapp/<agent>

**Why the runtime's own origin, not a port of the adapter's.** The adapter used
to listen on a port of its own (the ``sms.py`` pattern), which no deployment
publishes: the all-in-one Caddyfile forwards ``/api/*`` and nothing else, so on
a managed box Twilio could never reach it, and the operator was asked to paste a
URL into Twilio that could not work anyway. Every deployment shape already
routes ``/api/*`` here (the same reasoning as ``openai_proxy``), and the adapter
registers this URL with Twilio itself at connect
(``WhatsAppTwilioAdapter._ensure_inbound_routing``), so nobody copies anything.

**Why the agent is in the path.** One Twilio account can serve several agents,
each with its own WhatsApp number, and a Conversations webhook names a
conversation, not a number. The path segment routes deterministically.

**Security.** Self-authenticating ingress: the security middleware skips the
dashboard gates for POSTs under ``/api/twilio/`` and the adapter checks
``X-Twilio-Signature`` (HMAC-SHA1 over the URL and the form, keyed by the
account's auth token) before anything else runs. An unknown agent and an agent
without this connector answer the same 404, so the surface never confirms which
agent names exist. The body is capped before it is buffered, since the caller
is unauthenticated until the signature is checked. Rate limiting still applies.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

#: The ``PlatformRegistry`` key the adapter is registered under.
PLATFORM_KEY = "whatsapp_twilio"

#: Path prefix the security middleware exempts (POST only).
ROUTE_PREFIX = "/api/twilio/"

#: The route itself. ``WhatsAppTwilioAdapter.webhook_path`` builds the same
#: path, and a test pins the two together.
WHATSAPP_ROUTE = "/api/twilio/whatsapp/{agent}"

#: A Conversations ``onMessageAdded`` payload is a few KB: a body of at most
#: 1600 characters, media metadata as JSON, attributes capped at 16 KB. This
#: is generous and still bounds what an unauthenticated caller can make the
#: runtime hold before the signature check runs.
MAX_WEBHOOK_BODY_BYTES = 256 * 1024

# An empty TwiML document. Conversations does not act on the reply (the agent
# answers through the REST API), but a well-formed body is what Twilio's
# webhook logs and retry logic expect to see.
_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def _twiml(status: int) -> Response:
    return Response(content=_EMPTY_TWIML, media_type="application/xml", status_code=status)


def _resolve_adapter(agent: str) -> Any:
    """Return the agent's live WhatsApp over Twilio adapter, or ``None``."""
    try:
        from olano.platforms import get_platform_registry

        # ``for_agent`` rather than ``get``: a miss is an ordinary 404 here,
        # not the operational warning ``get`` logs for a missing adapter.
        return get_platform_registry().for_agent(agent).get(PLATFORM_KEY)
    except Exception as exc:  # registry not initialised (no agents booted yet)
        logger.debug("twilio webhook: registry lookup failed for %r: %s", agent, exc)
        return None


def request_urls(request: Request) -> list[str]:
    """The public URLs this request may have been signed for.

    Twilio signs the exact URL it was given, and behind the reverse proxy the
    runtime sees ``http://olano:8765/...`` instead. The adapter validates
    against the URL it registered first; these are the fallback for a webhook
    registered under an origin the deployment has since moved away from but
    still answers on (the proxy keeps the original ``Host``). Adding
    candidates cannot weaken the check: every one of them still needs an HMAC
    keyed by the auth token.
    """
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    host = host.split(",")[0].strip()
    if not host:
        return []
    query = request.url.query
    target = f"{request.url.path}?{query}" if query else request.url.path
    return [f"https://{host}{target}", f"http://{host}{target}"]


async def _read_capped(request: Request, limit: int) -> Optional[bytes]:
    """Read the body, or ``None`` once it exceeds *limit* bytes."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


def register_twilio_webhook_routes(app: Any) -> None:
    """Mount ``POST /api/twilio/whatsapp/{agent}`` on the runtime app."""

    @app.post(WHATSAPP_ROUTE, include_in_schema=False)
    async def twilio_whatsapp_webhook(agent: str, request: Request) -> Response:
        """Hand one Twilio Conversations webhook to the agent's adapter."""
        adapter = _resolve_adapter(agent)
        if adapter is None:
            return _twiml(404)
        if not getattr(adapter, "is_connected", False):
            # Registered but not (yet) connected: starting up, or it refused to
            # start. Nothing can validate the signature without the adapter's
            # credentials, so nothing is accepted.
            return _twiml(503)
        raw = await _read_capped(request, MAX_WEBHOOK_BODY_BYTES)
        if raw is None:
            logger.warning("twilio webhook: refused an oversized body for agent %r", agent)
            return _twiml(413)
        status = await adapter.receive_webhook(
            raw,
            request.headers.get("x-twilio-signature", ""),
            candidate_urls=request_urls(request),
        )
        return _twiml(int(status))
