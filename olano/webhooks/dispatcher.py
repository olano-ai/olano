"""Outbound webhook dispatcher.

A single :class:`WebhookDispatcher` lives on the runtime. Each agent registers
its webhook list at boot (and re-registers on edit / restart). The dispatcher
subscribes once to the global event bus; when a subscribed event fires it
fans out to every matching webhook — signing, rate-limiting, retrying, and
logging each delivery.

Because registration is driven by ``agent.json5`` (re-read on every boot) the
webhooks come back automatically after a server restart; the delivery history
lives in :class:`~olano.webhooks.db.WebhookDB` so it survives too.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from olano.config.agent_config import WebhookConfig
from olano.orchestration.event_bus import Event, EventBus, get_event_bus
from olano.platforms.url_safety import is_safe_url
from olano.security.rate_limit import InMemoryRateLimiter
from olano.webhooks.db import WebhookDB, get_webhook_db

logger = logging.getLogger(__name__)

USER_AGENT = "olano-webhooks/1"
# Status codes worth retrying (transient). Everything else is terminal.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def build_payload(
    *, event_type: str, agent_name: str, data: dict[str, Any], delivery_id: str, is_test: bool
) -> dict[str, Any]:
    """Construct the JSON body POSTed to a webhook receiver."""
    return {
        "id": delivery_id,
        "event": event_type,
        "agent": agent_name,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "test": is_test,
        "data": data,
    }


def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256 over ``"{timestamp}.{body}"`` → ``sha256=<hex>``.

    The receiver recomputes this over the raw body + timestamp header to verify
    authenticity and guard against replay.
    """
    mac = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode() + body,
        hashlib.sha256,
    )
    return f"sha256={mac.hexdigest()}"


class WebhookDispatcher:
    """Routes agent events to per-agent outbound webhooks."""

    def __init__(
        self,
        event_bus: EventBus | None = None,
        db: WebhookDB | None = None,
    ) -> None:
        self._bus = event_bus or get_event_bus()
        self._db = db or get_webhook_db()
        self._limiter = InMemoryRateLimiter()
        # agent_name -> {webhook_id: WebhookConfig}
        self._registry: dict[str, dict[str, WebhookConfig]] = {}
        self._subscribed = False
        self._inflight: set[asyncio.Task] = set()

    # ----------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Subscribe to the event bus (idempotent)."""
        if not self._subscribed:
            self._bus.subscribe("*", self._on_event)
            self._subscribed = True
            logger.info("WebhookDispatcher subscribed to event bus")

    def stop(self) -> None:
        if self._subscribed:
            self._bus.unsubscribe("*", self._on_event)
            self._subscribed = False
        for task in list(self._inflight):
            task.cancel()
        self._inflight.clear()

    def register_agent(self, agent_name: str, webhooks: list[WebhookConfig]) -> None:
        """Replace the outbound webhook set for an agent (boot + edits).

        Only outbound webhooks subscribe to the event bus; inbound ones are
        looked up on demand by the ingress endpoint, so they're skipped here.
        """
        enabled = {wh.id: wh for wh in webhooks if wh.enabled and wh.direction == "outbound"}
        if enabled:
            self._registry[agent_name] = enabled
        else:
            self._registry.pop(agent_name, None)

    def unregister_agent(self, agent_name: str) -> None:
        self._registry.pop(agent_name, None)

    def webhooks_for(self, agent_name: str) -> list[WebhookConfig]:
        return list(self._registry.get(agent_name, {}).values())

    # ------------------------------------------------------------- routing

    def _resolve_agent(self, event: Event) -> str | None:
        """Pull the owning agent name out of an event's data/source."""
        for key in ("agent", "agent_name"):
            val = event.data.get(key)
            if isinstance(val, str) and val:
                return val
        # Some emitters put the agent in ``source`` (e.g. message.sent).
        if event.source in self._registry:
            return event.source
        return None

    async def _on_event(self, event: Event) -> None:
        """Event-bus handler: fan out to matching webhooks (never raises)."""
        try:
            agent_name = self._resolve_agent(event)
            if not agent_name:
                return
            hooks = self._registry.get(agent_name)
            if not hooks:
                return
            for wh in list(hooks.values()):
                if wh.subscribes_to(event.type):
                    self._spawn(self._deliver(wh, agent_name, event.type, dict(event.data)))
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("WebhookDispatcher routing error: %s", exc)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    # ------------------------------------------------------------ delivery

    async def _deliver(
        self,
        wh: WebhookConfig,
        agent_name: str,
        event_type: str,
        data: dict[str, Any],
    ) -> None:
        """Rate-limit + send + record a single live (non-test) delivery."""
        if wh.rate_limit_per_minute:
            decision = self._limiter.check(
                f"{agent_name}:{wh.id}",
                max_calls=wh.rate_limit_per_minute,
                window_seconds=60,
            )
            if not decision.allowed:
                logger.info(
                    "Webhook '%s' rate-limited (retry in %ss)", wh.name, decision.retry_after
                )
                self._db.record(
                    agent_name=agent_name,
                    webhook_id=wh.id,
                    webhook_name=wh.name,
                    event_type=event_type,
                    url=wh.url,
                    ok=False,
                    error=f"rate-limited (>{wh.rate_limit_per_minute}/min)",
                )
                return
        result = await self.send(wh, event_type, agent_name, data, is_test=False)
        self._db.record(
            agent_name=agent_name,
            webhook_id=wh.id,
            webhook_name=wh.name,
            event_type=event_type,
            url=wh.url,
            ok=result["ok"],
            status_code=result.get("status_code"),
            duration_ms=result.get("duration_ms"),
            attempts=result.get("attempts", 1),
            error=result.get("error"),
            request_body=result.get("request_body"),
            response_body=result.get("response_body"),
        )

    async def send(
        self,
        wh: WebhookConfig,
        event_type: str,
        agent_name: str,
        data: dict[str, Any],
        *,
        is_test: bool,
    ) -> dict[str, Any]:
        """POST the payload with retry/backoff. Returns a result dict.

        Never raises — transport/SSRF/validation failures come back as
        ``{"ok": False, "error": ...}`` so callers (live delivery and the
        dashboard Test button) get a uniform shape.
        """
        import httpx

        delivery_id = _new_delivery_id()
        payload = build_payload(
            event_type=event_type,
            agent_name=agent_name,
            data=data,
            delivery_id=delivery_id,
            is_test=is_test,
        )
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        timestamp = str(int(time.time()))

        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "X-Olano-Event": event_type,
            "X-Olano-Delivery": delivery_id,
            "X-Olano-Timestamp": timestamp,
            **{str(k): str(v) for k, v in wh.headers.items()},
        }
        if is_test:
            headers["X-Olano-Test"] = "true"
        if wh.secret:
            headers["X-Olano-Signature"] = sign_payload(wh.secret, timestamp, body)

        # SSRF guard — fail closed before any network call.
        if wh.block_private_addresses and not is_safe_url(wh.url):
            return {
                "ok": False,
                "error": "blocked: url resolves to a private/internal address",
                "attempts": 0,
                "request_body": body.decode("utf-8", "replace"),
            }

        attempts = 0
        last_error: str | None = None
        max_attempts = max(1, wh.max_retries + 1)
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(verify=wh.verify_tls) as client:
                while attempts < max_attempts:
                    attempts += 1
                    try:
                        resp = await client.post(
                            wh.url,
                            content=body,
                            headers=headers,
                            timeout=wh.timeout_seconds,
                        )
                    except httpx.HTTPError as exc:
                        last_error = f"{type(exc).__name__}: {exc}"
                        if attempts < max_attempts:
                            await asyncio.sleep(min(2 ** (attempts - 1), 8))
                            continue
                        break

                    duration_ms = round((time.monotonic() - start) * 1000, 1)
                    if resp.status_code in _RETRYABLE_STATUS and attempts < max_attempts:
                        last_error = f"HTTP {resp.status_code}"
                        await asyncio.sleep(min(2 ** (attempts - 1), 8))
                        continue
                    return {
                        "ok": 200 <= resp.status_code < 300,
                        "status_code": resp.status_code,
                        "duration_ms": duration_ms,
                        "attempts": attempts,
                        "error": None if resp.is_success else f"HTTP {resp.status_code}",
                        "request_body": body.decode("utf-8", "replace"),
                        "response_body": resp.text[:8000],
                        "delivery_id": delivery_id,
                    }
        except Exception as exc:  # pragma: no cover - defensive
            last_error = f"{type(exc).__name__}: {exc}"

        return {
            "ok": False,
            "status_code": None,
            "duration_ms": round((time.monotonic() - start) * 1000, 1),
            "attempts": attempts,
            "error": last_error or "delivery failed",
            "request_body": body.decode("utf-8", "replace"),
            "delivery_id": delivery_id,
        }

    async def test(
        self, wh: WebhookConfig, agent_name: str, *, event_type: str = "test.ping"
    ) -> dict[str, Any]:
        """Fire a one-off test delivery (logged with ``is_test=True``)."""
        sample = {
            "message": "This is a test delivery from Olano.",
            "agent": agent_name,
            "webhook": wh.name,
        }
        result = await self.send(wh, event_type, agent_name, sample, is_test=True)
        self._db.record(
            agent_name=agent_name,
            webhook_id=wh.id,
            webhook_name=wh.name,
            event_type=event_type,
            url=wh.url,
            ok=result["ok"],
            status_code=result.get("status_code"),
            duration_ms=result.get("duration_ms"),
            attempts=result.get("attempts", 1),
            is_test=True,
            error=result.get("error"),
            request_body=result.get("request_body"),
            response_body=result.get("response_body"),
        )
        return result

    # ----------------------------------------------------------- inbound

    async def ingest(
        self,
        wh: WebhookConfig,
        agent_name: str,
        *,
        raw_body: bytes,
        content_type: str,
        headers: dict[str, str],
        dispatch: InboundDispatch | None,
        is_test: bool = False,
    ) -> dict[str, Any]:
        """Process one inbound payload: build a prompt, run the agent, log it.

        ``dispatch(message, thread_id) -> str`` runs the agent and returns its
        reply. When ``None`` (or the webhook isn't set to ``respond``) the
        message is still built/logged but no reply is produced — the caller may
        background the agent run instead. Never raises.
        """
        delivery_id = _new_delivery_id()
        if wh.rate_limit_per_minute:
            decision = self._limiter.check(
                f"in:{agent_name}:{wh.id}",
                max_calls=wh.rate_limit_per_minute,
                window_seconds=60,
            )
            if not decision.allowed:
                self._db.record(
                    agent_name=agent_name,
                    webhook_id=wh.id,
                    webhook_name=wh.name,
                    event_type="inbound",
                    url="",
                    ok=False,
                    direction="inbound",
                    is_test=is_test,
                    error=f"rate-limited (>{wh.rate_limit_per_minute}/min)",
                )
                return {"ok": False, "error": "rate limit exceeded", "status_code": 429}

        parsed, raw_text = parse_inbound_body(raw_body, content_type)
        message = build_inbound_message(wh, parsed=parsed, raw_text=raw_text, headers=headers)
        thread_id = (
            f"webhook-{wh.id}" if wh.accumulate_thread else f"webhook-{wh.id}-{delivery_id[:8]}"
        )

        start = time.monotonic()
        reply: str | None = None
        ok = True
        error: str | None = None
        try:
            if dispatch is not None:
                reply = await dispatch(message, thread_id)
        except Exception as exc:
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("Inbound webhook '%s' agent run failed: %s", wh.name, error)

        self._db.record(
            agent_name=agent_name,
            webhook_id=wh.id,
            webhook_name=wh.name,
            event_type="inbound",
            url="",
            ok=ok,
            duration_ms=round((time.monotonic() - start) * 1000, 1),
            direction="inbound",
            is_test=is_test,
            error=error,
            request_body=raw_text,
            response_body=reply,
        )
        return {
            "ok": ok,
            "thread_id": thread_id,
            "message": message,
            "response": reply,
            "error": error,
            "delivery_id": delivery_id,
        }


# dispatch callable: (message, thread_id) -> agent reply text
InboundDispatch = Any


def parse_inbound_body(raw_body: bytes, content_type: str) -> tuple[Any, str]:
    """Best-effort parse of an arbitrary inbound payload.

    Returns ``(parsed, raw_text)`` where ``parsed`` is a dict/list (JSON or
    form-encoded) or ``None`` when the body is opaque text.
    """
    raw_text = raw_body.decode("utf-8", "replace")
    ct = (content_type or "").lower()
    stripped = raw_text.lstrip()
    if "application/json" in ct or stripped[:1] in ("{", "["):
        try:
            return json.loads(raw_text), raw_text
        except (json.JSONDecodeError, ValueError):
            pass
    if "application/x-www-form-urlencoded" in ct or ("=" in raw_text and "&" in raw_text):
        from urllib.parse import parse_qsl

        try:
            pairs = parse_qsl(raw_text, strict_parsing=False)
            if pairs:
                return dict(pairs), raw_text
        except ValueError:
            pass
    return None, raw_text


# Inbound request headers worth surfacing to the agent — many senders carry the
# event type / signature there (e.g. GitHub's X-GitHub-Event).
_NOTABLE_HEADER_PREFIXES = ("x-", "user-agent", "content-type")
_NOISE_HEADERS = frozenset(
    {"x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-real-ip"}
)


def build_inbound_message(
    wh: WebhookConfig,
    *,
    parsed: Any,
    raw_text: str,
    headers: dict[str, str],
) -> str:
    """Compose the agent prompt: user guidelines + the full incoming payload.

    Every field the sender posted is appended verbatim so the agent can process
    payloads whose schema the user never had to specify.
    """
    parts: list[str] = []
    if wh.prompt.strip():
        parts.append(wh.prompt.strip())
    parts.append(f"--- Incoming webhook payload: {wh.name} ---")

    if isinstance(parsed, dict | list):
        parts.append("```json")
        parts.append(json.dumps(parsed, indent=2, ensure_ascii=False, default=str)[:20000])
        parts.append("```")
    elif raw_text.strip():
        parts.append(raw_text[:20000])
    else:
        parts.append("(empty body)")

    notable = {
        k: v
        for k, v in headers.items()
        if k.lower() not in _NOISE_HEADERS and k.lower().startswith(_NOTABLE_HEADER_PREFIXES)
    }
    if notable:
        parts.append("--- Request headers ---")
        parts.extend(f"{k}: {v}" for k, v in sorted(notable.items()))

    return "\n".join(parts)


def _new_delivery_id() -> str:
    import uuid

    return uuid.uuid4().hex


_DISPATCHER: WebhookDispatcher | None = None


def get_dispatcher() -> WebhookDispatcher:
    """Process-wide dispatcher singleton (lazy)."""
    global _DISPATCHER
    if _DISPATCHER is None:
        _DISPATCHER = WebhookDispatcher()
    return _DISPATCHER
