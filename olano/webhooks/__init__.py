"""Per-agent outbound webhooks.

When a subscribed agent event fires, the runtime POSTs a signed JSON payload
to each configured webhook URL. Deliveries are rate-limited, retried with
backoff, and recorded so the dashboard can show history and live test results.

Public surface:

    from olano.webhooks import WebhookDispatcher, WebhookDB, build_payload
"""

from __future__ import annotations

from olano.webhooks.db import WebhookDB
from olano.webhooks.dispatcher import (
    WebhookDispatcher,
    build_inbound_message,
    build_payload,
    parse_inbound_body,
    sign_payload,
)

__all__ = [
    "WebhookDB",
    "WebhookDispatcher",
    "build_inbound_message",
    "build_payload",
    "parse_inbound_body",
    "sign_payload",
]
