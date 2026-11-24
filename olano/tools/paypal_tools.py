"""PayPal REST API tools."""

from __future__ import annotations

from typing import Any

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

_PAYPAL_CREDS_MSG = (
    "[missing-credentials] No PayPal credentials found. Set PAYPAL_ACCESS_TOKEN or both "
    "PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET. PAYPAL_MODE controls sandbox vs live."
)


def _base_url() -> str:
    mode = get_credential("PAYPAL_MODE", "sandbox").lower()
    return "https://api-m.sandbox.paypal.com" if mode == "sandbox" else "https://api-m.paypal.com"


def _access_token() -> tuple[str | None, str | None]:
    """Return (token, error_string). One of the two is always None."""
    token = get_credential("PAYPAL_ACCESS_TOKEN")
    if token:
        return token, None
    client_id = get_credential("PAYPAL_CLIENT_ID")
    secret = get_credential("PAYPAL_CLIENT_SECRET")
    if not client_id or not secret:
        return None, _PAYPAL_CREDS_MSG
    try:
        r = httpx.post(
            f"{_base_url()}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(client_id, secret),
            timeout=20,
        )
        r.raise_for_status()
        return str(r.json().get("access_token", "")), None
    except Exception as exc:
        return None, f"[tool-error] PayPal auth: {exc}"


def _request(method: str, path: str, **kwargs: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Return (json_dict, error_string). One of the two is always None."""
    token, err = _access_token()
    if err:
        return None, err
    try:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        r = httpx.request(
            method,
            f"{_base_url()}/{path.lstrip('/')}",
            headers=headers,
            timeout=30,
            **kwargs,
        )
        r.raise_for_status()
        return (r.json() if r.content else {}), None
    except Exception as exc:
        return None, f"[tool-error] PayPal {method} {path}: {exc}"


@tool
def paypal_get_balance() -> str:
    """Get PayPal account balances."""
    data, err = _request("GET", "v1/reporting/balances")
    if err:
        return err
    balances = (data or {}).get("balances", [])
    if not balances:
        return "No balances returned."
    lines = []
    for b in balances:
        ab = b.get("available_balance", {})
        lines.append(f"- {ab.get('value', '?')} {ab.get('currency_code', '?')}")
    return "\n".join(lines)


@tool
def paypal_list_transactions(start_date: str, end_date: str, page_size: int = 20) -> str:
    """List PayPal transactions for an ISO-8601 date range."""
    data, err = _request(
        "GET",
        "v1/reporting/transactions",
        params={"start_date": start_date, "end_date": end_date, "page_size": page_size},
    )
    if err:
        return err
    rows = (data or {}).get("transaction_details", [])
    if not rows:
        return "No transactions found."
    lines = []
    for t in rows:
        info = t.get("transaction_info", {})
        amount = info.get("transaction_amount", {})
        lines.append(
            f"- {info.get('transaction_id', '?')} "
            f"{amount.get('value', '?')} {amount.get('currency_code', '?')}"
        )
    return "\n".join(lines)


@tool
def paypal_create_order(amount: str, currency: str = "USD", description: str = "") -> str:
    """Create a PayPal order for later approval/capture."""
    payload = {
        "intent": "CAPTURE",
        "purchase_units": [
            {"amount": {"currency_code": currency, "value": amount}, "description": description}
        ],
    }
    data, err = _request("POST", "v2/checkout/orders", json=payload)
    if err:
        return err
    return f"Created PayPal order {(data or {}).get('id', '?')} status={(data or {}).get('status', '?')}"


@tool
def paypal_get_order(order_id: str) -> str:
    """Get a PayPal order by ID."""
    data, err = _request("GET", f"v2/checkout/orders/{order_id}")
    if err:
        return err
    return str(data)


@tool
def paypal_capture_order(order_id: str) -> str:
    """Capture an approved PayPal order."""
    data, err = _request("POST", f"v2/checkout/orders/{order_id}/capture", json={})
    if err:
        return err
    return f"Captured PayPal order {(data or {}).get('id', '?')} status={(data or {}).get('status', '?')}"


@tool
def paypal_create_invoice(
    recipient_email: str, item_name: str, amount: str, currency: str = "USD", note: str = ""
) -> str:
    """Create a draft PayPal invoice."""
    payload = {
        "detail": {"currency_code": currency, "note": note},
        "invoicer": {},
        "primary_recipients": [{"billing_info": {"email_address": recipient_email}}],
        "items": [
            {
                "name": item_name,
                "quantity": "1",
                "unit_amount": {"currency_code": currency, "value": amount},
            }
        ],
    }
    data, err = _request("POST", "v2/invoicing/invoices", json=payload)
    if err:
        return err
    d = data or {}
    return f"Created PayPal invoice {d.get('id') or d.get('href') or '?'}"


@tool
def paypal_send_invoice(invoice_id: str) -> str:
    """Send a draft PayPal invoice."""
    _, err = _request(
        "POST",
        f"v2/invoicing/invoices/{invoice_id}/send",
        json={"send_to_recipient": True},
    )
    if err:
        return err
    return f"Sent PayPal invoice {invoice_id}"


@tool
def paypal_get_invoice(invoice_id: str) -> str:
    """Get a PayPal invoice by ID."""
    data, err = _request("GET", f"v2/invoicing/invoices/{invoice_id}")
    if err:
        return err
    return str(data)


@tool
def paypal_refund_capture(
    capture_id: str,
    amount: str = "",
    currency: str = "USD",
    note: str = "",
) -> str:
    """Refund a captured PayPal payment. Empty amount = full refund."""
    payload: dict[str, Any] = {}
    if amount:
        payload["amount"] = {"value": amount, "currency_code": currency}
    if note:
        payload["note_to_payer"] = note
    data, err = _request("POST", f"v2/payments/captures/{capture_id}/refund", json=payload)
    if err:
        return err
    d = data or {}
    return f"Refund {d.get('id', '?')} status={d.get('status', '?')}"


get_tools, get_tool = make_toolkit_api(globals())
