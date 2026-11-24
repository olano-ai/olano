"""Stripe payments tools.

Auth env var: STRIPE_API_KEY
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)


_STRIPE_BASE = "https://api.stripe.com/v1"
_STRIPE_CREDS_MSG = (
    "[missing-credentials] No Stripe API key found. Set STRIPE_API_KEY in the secrets vault."
)


def _stripe_auth() -> tuple[str, str] | None:
    key = get_credential("STRIPE_API_KEY", "")
    if not key:
        return None
    return (key, "")


@tool
def stripe_get_balance() -> str:
    """Get Stripe account balance (available and pending)."""
    auth = _stripe_auth()
    if auth is None:
        return _STRIPE_CREDS_MSG
    try:
        r = httpx.get(f"{_STRIPE_BASE}/balance", auth=auth, timeout=10)
        r.raise_for_status()
        bal = r.json()
        available = bal.get("available", [])
        pending = bal.get("pending", [])
        lines = []
        for item in available:
            amt = item.get("amount", 0) / 100
            cur = item.get("currency", "").upper()
            lines.append(f"Available: {amt:.2f} {cur}")
        for item in pending:
            amt = item.get("amount", 0) / 100
            cur = item.get("currency", "").upper()
            lines.append(f"Pending: {amt:.2f} {cur}")
        return "\n".join(lines) if lines else "No balance data."
    except Exception as exc:
        return f"[tool-error] stripe_get_balance: {exc}"


@tool
def stripe_list_payments(
    limit: Annotated[int, "Max payments to return"] = 20,
) -> str:
    """List recent Stripe payment intents."""
    auth = _stripe_auth()
    if auth is None:
        return _STRIPE_CREDS_MSG
    try:
        r = httpx.get(
            f"{_STRIPE_BASE}/payment_intents",
            auth=auth,
            params={"limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        payments = r.json().get("data", [])
        lines = [
            f"- [{p.get('id', '?')}] {p.get('amount', 0) / 100:.2f} "
            f"{p.get('currency', '').upper()} | {p.get('status', '?')} | "
            f"{p.get('description', '') or ''}"
            for p in payments
        ]
        return "\n".join(lines) if lines else "No payments found."
    except Exception as exc:
        return f"[tool-error] stripe_list_payments: {exc}"


@tool
def stripe_list_customers(
    limit: Annotated[int, "Max customers to return"] = 20,
    email: Annotated[str, "Filter by email (optional)"] = "",
) -> str:
    """List Stripe customers."""
    auth = _stripe_auth()
    if auth is None:
        return _STRIPE_CREDS_MSG
    try:
        params: dict = {"limit": limit}
        if email:
            params["email"] = email
        r = httpx.get(f"{_STRIPE_BASE}/customers", auth=auth, params=params, timeout=10)
        r.raise_for_status()
        customers = r.json().get("data", [])
        lines = [
            f"- [{c.get('id', '?')}] {c.get('email', 'N/A')} | "
            f"{c.get('name', '') or ''} | created {c.get('created', '?')}"
            for c in customers
        ]
        return "\n".join(lines) if lines else "No customers found."
    except Exception as exc:
        return f"[tool-error] stripe_list_customers: {exc}"


@tool
def stripe_create_invoice(
    customer_id: Annotated[str, "Stripe customer ID"],
    amount: Annotated[int, "Amount in cents (e.g. 2999 = $29.99)"],
    currency: Annotated[str, "Currency code (e.g. usd)"] = "usd",
    description: Annotated[str, "Invoice description"] = "",
) -> str:
    """Create a Stripe invoice for a customer."""
    auth = _stripe_auth()
    if auth is None:
        return _STRIPE_CREDS_MSG
    try:
        item_r = httpx.post(
            f"{_STRIPE_BASE}/invoiceitems",
            auth=auth,
            data={
                "customer": customer_id,
                "amount": amount,
                "currency": currency,
                "description": description,
            },
            timeout=10,
        )
        item_r.raise_for_status()

        inv_r = httpx.post(
            f"{_STRIPE_BASE}/invoices",
            auth=auth,
            data={"customer": customer_id, "auto_advance": "false"},
            timeout=10,
        )
        inv_r.raise_for_status()
        inv = inv_r.json()
        return (
            f"Invoice created: [{inv.get('id', '?')}] ${amount / 100:.2f} "
            f"{currency.upper()} | status: {inv.get('status', '?')}"
        )
    except Exception as exc:
        return f"[tool-error] stripe_create_invoice: {exc}"


@tool
def stripe_create_payment_link(
    price_id: Annotated[str, "Stripe Price ID"],
    quantity: Annotated[int, "Quantity"] = 1,
) -> str:
    """Create a Stripe payment link for a product/price."""
    auth = _stripe_auth()
    if auth is None:
        return _STRIPE_CREDS_MSG
    try:
        r = httpx.post(
            f"{_STRIPE_BASE}/payment_links",
            auth=auth,
            data={"line_items[0][price]": price_id, "line_items[0][quantity]": quantity},
            timeout=10,
        )
        r.raise_for_status()
        link = r.json()
        return f"Payment link: {link.get('url', '?')} | id={link.get('id', '?')}"
    except Exception as exc:
        return f"[tool-error] stripe_create_payment_link: {exc}"


@tool
def stripe_get_revenue(
    period: Annotated[str, "Period: today, week, month, year"] = "month",
) -> str:
    """Get Stripe revenue summary for a period using charges."""
    auth = _stripe_auth()
    if auth is None:
        return _STRIPE_CREDS_MSG
    try:
        from datetime import datetime, timedelta

        now = datetime.utcnow()
        period_map = {
            "today": now.replace(hour=0, minute=0, second=0),
            "week": now - timedelta(days=7),
            "month": now - timedelta(days=30),
            "year": now - timedelta(days=365),
        }
        since = int(period_map.get(period, period_map["month"]).timestamp())
        r = httpx.get(
            f"{_STRIPE_BASE}/charges",
            auth=auth,
            params={"created[gte]": since, "limit": 100},
            timeout=10,
        )
        r.raise_for_status()
        charges = r.json().get("data", [])
        succeeded = [c for c in charges if c.get("status") == "succeeded"]
        total = sum(c.get("amount", 0) for c in succeeded) / 100
        if not succeeded:
            return f"Revenue ({period}): $0.00"
        return (
            f"Revenue ({period}): ${total:.2f}\n"
            f"Transactions: {len(succeeded)}\n"
            f"Avg: ${total / len(succeeded):.2f}"
        )
    except Exception as exc:
        return f"[tool-error] stripe_get_revenue: {exc}"


@tool
def stripe_list_products(
    limit: Annotated[int, "Max products to return"] = 20,
    active_only: Annotated[bool, "If True, only active products"] = True,
) -> str:
    """List Stripe products with their default prices."""
    auth = _stripe_auth()
    if auth is None:
        return _STRIPE_CREDS_MSG
    try:
        params: dict = {"limit": limit, "expand[]": "data.default_price"}
        if active_only:
            params["active"] = "true"
        r = httpx.get(f"{_STRIPE_BASE}/products", auth=auth, params=params, timeout=10)
        r.raise_for_status()
        products = r.json().get("data", [])
        if not products:
            return "No products found."
        lines = []
        for p in products:
            price = p.get("default_price") or {}
            if isinstance(price, dict):
                amt = price.get("unit_amount")
                cur = price.get("currency", "").upper()
                price_str = f"${amt / 100:.2f} {cur}" if amt is not None else "—"
                price_id = price.get("id", "")
            else:
                price_str = "—"
                price_id = str(price)
            lines.append(
                f"- [{p.get('id', '?')}] {p.get('name', '?')} | {price_str} "
                f"({price_id}) | active={p.get('active', '?')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] stripe_list_products: {exc}"


@tool
def stripe_refund_payment(
    payment_intent_id: Annotated[str, "Stripe PaymentIntent ID (pi_...)"],
    amount: Annotated[int, "Amount in cents to refund (0 = full)"] = 0,
    reason: Annotated[
        str,
        "Refund reason: duplicate, fraudulent, requested_by_customer",
    ] = "requested_by_customer",
) -> str:
    """Refund a Stripe payment. amount=0 refunds the full amount."""
    auth = _stripe_auth()
    if auth is None:
        return _STRIPE_CREDS_MSG
    try:
        data: dict = {"payment_intent": payment_intent_id, "reason": reason}
        if amount > 0:
            data["amount"] = amount
        r = httpx.post(f"{_STRIPE_BASE}/refunds", auth=auth, data=data, timeout=15)
        r.raise_for_status()
        rf = r.json()
        amt = rf.get("amount", 0) / 100
        cur = rf.get("currency", "").upper()
        return (
            f"Refund created: [{rf.get('id', '?')}] ${amt:.2f} {cur} | "
            f"status: {rf.get('status', '?')}"
        )
    except Exception as exc:
        return f"[tool-error] stripe_refund_payment: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
