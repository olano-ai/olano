"""Public inbound route for the WhatsApp (Meta Cloud API) connector.

Registered from ``create_api`` (api.py), next to the Twilio route. Meta
delivers every customer message and every delivery status to a webhook, and
the connector points its number at this one, on the deployment's own public
origin (``WhatsAppMetaAPIAdapter._ensure_inbound_routing``)::

    GET  https://<dashboard host>/api/meta/whatsapp/<agent>   Meta checks the address
    POST https://<dashboard host>/api/meta/whatsapp/<agent>   Meta delivers events

**The check (GET).** When the connector sets the number's webhook address,
Meta calls it with ``hub.mode=subscribe``, ``hub.verify_token`` and
``hub.challenge``; the answer is the challenge, as plain text, only when the
token is this agent's (a derived value, see ``verify_token``).

**Deliveries (POST).** Self-authenticating ingress: the security middleware
skips the dashboard gates under ``/api/meta/whatsapp/`` and nothing is parsed
before ``X-Hub-Signature-256`` (HMAC-SHA256 of the raw body, keyed by the app
secret) checks out. The body is capped first, since the caller is anonymous
until then.

**Which agent.** The path names the agent whose number the address was set
for, and a number-level webhook address delivers only that number's events.
A delivery can still carry another number's events: an address set by hand as
the app's or the Business Account's callback receives every number on it. So
each event goes to the connector whose phone number ID it names, the path's
agent or another one, and only when that connector's app secret is the one
the signature was checked with (the same Meta app); an event for a number no
connector here owns is dropped with a log line. An unknown agent and an agent
without the connector answer the same 404, so the route never confirms which
agent names exist.

Meta expects a 200 quickly and retries anything else for days, so events are
handed to their connector in the background and the answer does not wait for
the agent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from fastapi import Request
from starlette.responses import PlainTextResponse, Response

logger = logging.getLogger(__name__)

#: The ``PlatformRegistry`` key the adapter is registered under.
PLATFORM_KEY = "whatsapp_meta_api"

#: Path prefix the security middleware exempts (GET and POST).
ROUTE_PREFIX = "/api/meta/whatsapp/"

#: The route itself. ``WhatsAppMetaAPIAdapter.webhook_path`` builds the same
#: path, and a test pins the two together.
WHATSAPP_ROUTE = "/api/meta/whatsapp/{agent}"

#: Meta batches events; a few MB bounds what an anonymous caller can make the
#: runtime hold before the signature check runs.
MAX_WEBHOOK_BODY_BYTES = 3 * 1024 * 1024

# Background deliveries, held so the loop does not collect them mid-run.
_TASKS: set[asyncio.Task] = set()


def _registry() -> Any:
    from olano.platforms import get_platform_registry

    return get_platform_registry()


def _resolve_adapter(agent: str) -> Any:
    """The agent's WhatsApp (Meta Cloud API) adapter, or ``None``."""
    try:
        return _registry().for_agent(agent).get(PLATFORM_KEY)
    except Exception as exc:  # registry not initialised (no agents booted yet)
        logger.debug("meta webhook: registry lookup failed for %r: %s", agent, exc)
        return None


def _adapter_for_number(phone_number_id: str, fingerprint: str, fallback: Any) -> Any:
    """The connector that owns *phone_number_id* on the same Meta app, or ``None``."""
    if not phone_number_id or phone_number_id == getattr(fallback, "phone_number_id", ""):
        return fallback
    try:
        peers = _registry().for_platform(PLATFORM_KEY)
    except Exception:
        return None
    for adapter in peers.values():
        if (
            getattr(adapter, "phone_number_id", "") == phone_number_id
            and getattr(adapter, "app_fingerprint", "") == fingerprint
        ):
            return adapter
    return None


async def _read_capped(request: Request, limit: int) -> Optional[bytes]:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return None
    return bytes(body)


def _deliver(adapter: Any, value: dict[str, Any]) -> None:
    async def _run() -> None:
        try:
            await adapter.handle_webhook_value(value)
        except Exception:
            logger.exception(
                "meta webhook: delivery to %s failed", getattr(adapter, "agent_name", "?")
            )

    task = asyncio.create_task(_run())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


def register_meta_webhook_routes(app: Any) -> None:
    """Mount ``GET``/``POST /api/meta/whatsapp/{agent}`` on the runtime app."""

    @app.get(WHATSAPP_ROUTE, include_in_schema=False)
    async def meta_whatsapp_verify(agent: str, request: Request) -> Response:
        adapter = _resolve_adapter(agent)
        if adapter is None:
            return Response(status_code=404)
        params = request.query_params
        challenge = adapter.verify_subscription(
            params.get("hub.mode", ""),
            params.get("hub.verify_token", ""),
            params.get("hub.challenge", ""),
        )
        if challenge is None:
            return Response(status_code=403)
        return PlainTextResponse(challenge)

    @app.post(WHATSAPP_ROUTE, include_in_schema=False)
    async def meta_whatsapp_webhook(agent: str, request: Request) -> Response:
        adapter = _resolve_adapter(agent)
        if adapter is None:
            return Response(status_code=404)
        raw = await _read_capped(request, MAX_WEBHOOK_BODY_BYTES)
        if raw is None:
            logger.warning("meta webhook: refused an oversized body for agent %r", agent)
            return Response(status_code=413)
        if not adapter.signature_valid(raw, request.headers.get("x-hub-signature-256", "")):
            logger.warning("meta webhook: rejected a delivery with a bad or missing signature")
            return Response(status_code=401)
        try:
            payload = json.loads(raw)
        except Exception:
            return Response(status_code=400)
        if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
            return Response(status_code=200)
        fingerprint = adapter.app_fingerprint
        for entry in payload.get("entry") or []:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes") or []:
                if not isinstance(change, dict) or change.get("field") != "messages":
                    continue
                value = change.get("value")
                if not isinstance(value, dict):
                    continue
                metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
                number_id = str((metadata or {}).get("phone_number_id") or "")
                target = _adapter_for_number(number_id, fingerprint, adapter)
                if target is None:
                    logger.info(
                        "meta webhook: no connector here owns phone number ID %s; event dropped",
                        number_id,
                    )
                    continue
                _deliver(target, value)
        return Response(status_code=200)
