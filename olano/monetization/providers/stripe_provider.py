"""Stripe rail: Checkout subscriptions + signed webhook → entitlement updates.

Each agent owner uses **their own** Stripe account: the secret key and webhook
secret are resolved per-agent from the credential vault (same path as
``tools/stripe_tools.py``), never a single platform key. There is no Stripe
Connect / application fee — money goes straight to the owner.

The end-user is linked to their Checkout session via ``client_reference_id =
"{agent}:{channel}:{user_id}"``; the webhook reads it back on
``checkout.session.completed`` and stores the ``subscription`` id so later
renewal/cancel events (which carry no chat identity) resolve by that id.
"""

from __future__ import annotations

import hmac
import json
import logging
from hashlib import sha256
from typing import Any

import httpx

from olano.monetization.providers.base import (
    EntitlementUpdate,
    PaymentProvider,
    SubscriptionOffer,
)
from olano.monetization.store import EntitlementStatus
from olano.tools._credentials import get_credential

logger = logging.getLogger(__name__)

_STRIPE_BASE = "https://api.stripe.com/v1"
# Reject a signed event whose timestamp is older than this (replay protection).
_SIG_TOLERANCE_S = 300


def encode_reference(agent: str, channel: str, user_id: str) -> str:
    """The ``client_reference_id`` that links a checkout back to a chat user."""
    return f"{agent}:{channel}:{user_id}"


def decode_reference(ref: str) -> tuple[str, str, str] | None:
    parts = (ref or "").split(":")
    if len(parts) < 3:
        return None
    # user_id may itself contain ':' — keep everything after the 2nd colon.
    return parts[0], parts[1], ":".join(parts[2:])


class StripeProvider(PaymentProvider):
    name = "stripe"

    def _api_key(self) -> str:
        return get_credential("STRIPE_API_KEY", "")

    def _webhook_secret(self) -> str:
        return get_credential("STRIPE_WEBHOOK_SECRET", "")

    async def create_offer(self, *, channel: str, user_id: str) -> SubscriptionOffer | None:
        key = self._api_key()
        price_id = getattr(self.plan, "stripe_price_id", "") or ""
        if not key or not price_id:
            # Fail soft: the caller falls back to Stars / a contact-owner note.
            logger.warning(
                "Stripe offer unavailable for agent=%s (key=%s price=%s)",
                self.agent,
                "set" if key else "missing",
                "set" if price_id else "missing",
            )
            return None

        success_url = getattr(self.plan, "success_url", "") or "https://t.me"
        cancel_url = getattr(self.plan, "cancel_url", "") or success_url
        data = {
            "mode": "subscription",
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": "1",
            "client_reference_id": encode_reference(self.agent, channel, user_id),
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{_STRIPE_BASE}/checkout/sessions", data=data, auth=(key, "")
                )
            resp.raise_for_status()
            url = resp.json().get("url")
        except Exception as exc:  # network / API error → fail soft
            logger.warning("Stripe checkout creation failed for agent=%s: %s", self.agent, exc)
            return None
        if not url:
            return None
        return SubscriptionOffer(text=getattr(self.plan, "offer_copy", ""), stripe_url=url)

    # ── webhook ───────────────────────────────────────────────────────────
    def verify_webhook(self, *, headers: dict[str, str], body: bytes) -> bool:
        secret = self._webhook_secret()
        if not secret:
            logger.warning("Stripe webhook secret missing for agent=%s; rejecting", self.agent)
            return False
        sig_header = headers.get("stripe-signature") or headers.get("Stripe-Signature") or ""
        timestamp, signatures = _parse_sig_header(sig_header)
        if not timestamp or not signatures:
            return False
        try:
            import time as _time

            if abs(_time.time() - int(timestamp)) > _SIG_TOLERANCE_S:
                logger.warning(
                    "Stripe webhook timestamp outside tolerance for agent=%s", self.agent
                )
                return False
        except ValueError:
            return False
        signed_payload = f"{timestamp}.".encode() + body
        expected = hmac.new(secret.encode(), signed_payload, sha256).hexdigest()
        return any(hmac.compare_digest(expected, sig) for sig in signatures)

    def parse_event(self, payload: dict[str, Any]) -> EntitlementUpdate | None:
        event_type = payload.get("type", "")
        event_id = payload.get("id", "")
        obj = (payload.get("data") or {}).get("object") or {}

        if event_type == "checkout.session.completed":
            ref = decode_reference(obj.get("client_reference_id") or "")
            if not ref:
                return None
            agent, channel, user_id = ref
            return EntitlementUpdate(
                status=EntitlementStatus.ACTIVE,
                provider=self.name,
                agent=agent,
                channel=channel,
                user_id=user_id,
                external_customer_id=str(obj.get("customer") or ""),
                external_sub_id=str(obj.get("subscription") or ""),
                current_period_end=_period_end(obj),
                event_id=event_id,
            )

        if event_type in ("invoice.paid", "invoice.payment_succeeded"):
            return EntitlementUpdate(
                status=EntitlementStatus.ACTIVE,
                provider=self.name,
                external_sub_id=str(obj.get("subscription") or ""),
                external_customer_id=str(obj.get("customer") or ""),
                current_period_end=_period_end(obj),
                event_id=event_id,
            )

        if event_type == "invoice.payment_failed":
            return EntitlementUpdate(
                status=EntitlementStatus.PAST_DUE,
                provider=self.name,
                external_sub_id=str(obj.get("subscription") or ""),
                event_id=event_id,
            )

        if event_type == "customer.subscription.updated":
            status = _map_sub_status(str(obj.get("status") or ""))
            return EntitlementUpdate(
                status=status,
                provider=self.name,
                external_sub_id=str(obj.get("id") or ""),
                external_customer_id=str(obj.get("customer") or ""),
                current_period_end=_period_end(obj),
                event_id=event_id,
            )

        if event_type == "customer.subscription.deleted":
            return EntitlementUpdate(
                status=EntitlementStatus.CANCELED,
                provider=self.name,
                external_sub_id=str(obj.get("id") or ""),
                current_period_end=_period_end(obj),
                event_id=event_id,
            )

        if event_type in ("charge.refunded", "charge.dispute.created"):
            # Revoke immediately on refund/chargeback.
            return EntitlementUpdate(
                status=EntitlementStatus.CANCELED,
                provider=self.name,
                external_customer_id=str(obj.get("customer") or ""),
                current_period_end=0.0,
                event_id=event_id,
            )

        return None


def _parse_sig_header(header: str) -> tuple[str, list[str]]:
    """Split Stripe's ``t=...,v1=...,v1=...`` header into (timestamp, [v1 sigs])."""
    timestamp = ""
    sigs: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            sigs.append(value)
    return timestamp, sigs


def _period_end(obj: dict[str, Any]) -> float | None:
    """Extract a Unix ``current_period_end`` from a subscription/invoice object."""
    val = obj.get("current_period_end")
    if val is None:
        lines = (obj.get("lines") or {}).get("data") or [{}]
        period = (lines[0].get("period") if lines else {}) or {}
        val = period.get("end")
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _map_sub_status(stripe_status: str) -> EntitlementStatus:
    return {
        "active": EntitlementStatus.ACTIVE,
        "trialing": EntitlementStatus.ACTIVE,
        "past_due": EntitlementStatus.PAST_DUE,
        "unpaid": EntitlementStatus.PAST_DUE,
        "canceled": EntitlementStatus.CANCELED,
        "incomplete_expired": EntitlementStatus.EXPIRED,
    }.get(stripe_status, EntitlementStatus.CANCELED)


def load_event(body: bytes) -> dict[str, Any] | None:
    """Parse a raw webhook body into a JSON event dict (``None`` on bad JSON)."""
    try:
        event = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return event if isinstance(event, dict) else None
