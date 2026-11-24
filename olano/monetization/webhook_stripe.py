"""Stripe webhook endpoint on the runtime FastAPI app.

Registered by :func:`create_api`. One route serves every agent in the single
runtime process; the ``{agent}`` path segment picks the tenant, so each owner
points *their* Stripe dashboard webhook at ``/api/monetization/stripe/<agent>``.
Per-agent secrets (the ``whsec_…`` signing secret, the API key) resolve through
``agent_runtime_context`` exactly like the agent's own tools.

The HTTP security middleware skips the bearer/session gate for this path (see
``runtime/api.py``); authenticity comes solely from Stripe's signed-payload
HMAC verified here.
"""

from __future__ import annotations

import logging
from typing import Any

# Imported at module level (not inside the function) so FastAPI can resolve the
# ``Request`` annotation from module globals when it introspects the route —
# a function-local import leaves the hint unresolvable and FastAPI then mistakes
# ``request`` for a query parameter (422).
from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def register_stripe_webhook(app: Any, runtime: Any) -> None:
    """Add ``POST /api/monetization/stripe/{agent}`` to *app*."""

    @app.post("/api/monetization/stripe/{agent}")
    async def stripe_webhook(agent: str, request: Request) -> Any:
        runner = runtime.get_runner(agent) if hasattr(runtime, "get_runner") else None
        if runner is None:
            return JSONResponse(status_code=404, content={"detail": "unknown agent"})

        mon = getattr(runner.config, "monetization", None)
        if mon is None or not getattr(mon, "enabled", False):
            return JSONResponse(status_code=404, content={"detail": "monetization disabled"})

        # Read the RAW body before any parsing — Stripe's signature is computed
        # over the exact bytes.
        body = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}

        from olano.monetization.providers.stripe_provider import StripeProvider, load_event
        from olano.monetization.service import apply_entitlement_update, send_confirmation
        from olano.tools._credentials import agent_runtime_context

        # Bind the agent so get_credential resolves THIS agent's webhook secret.
        with agent_runtime_context(agent, getattr(runner.config, "env", None)):
            provider = StripeProvider(agent=agent, plan=getattr(mon, "plan", None))
            if not provider.verify_webhook(headers=headers, body=body):
                logger.warning("Stripe webhook signature rejected for agent=%s", agent)
                return JSONResponse(status_code=401, content={"detail": "bad signature"})

        event = load_event(body)
        if event is None:
            return JSONResponse(status_code=400, content={"detail": "invalid payload"})

        update = provider.parse_event(event)
        if update is None:
            return JSONResponse(status_code=200, content={"received": True, "handled": False})
        # A webhook without a chat reference inherits the agent from the path.
        if not update.agent:
            update.agent = agent

        record = apply_entitlement_update(update)
        if record is not None and record.status.value == "active":
            await send_confirmation(
                record.agent,
                record.channel,
                record.user_id,
                "✅ Subscription active — thanks! You now have full access.",
            )
        return JSONResponse(status_code=200, content={"received": True, "handled": True})
