"""Shopify storefront/admin tools.

Auth env vars: SHOPIFY_STORE (mystore.myshopify.com), SHOPIFY_ACCESS_TOKEN
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_SHOP_CREDS_MSG = (
    "[missing-credentials] SHOPIFY_STORE and SHOPIFY_ACCESS_TOKEN must be set in vault."
)


def _shopify_config() -> tuple[str | None, dict | None, str | None]:
    """Return (base_url, headers, error_string)."""
    store = get_credential("SHOPIFY_STORE", "")
    token = get_credential("SHOPIFY_ACCESS_TOKEN", "")
    if not store or not token:
        return None, None, _SHOP_CREDS_MSG
    base = f"https://{store}/admin/api/2024-01"
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    return base, headers, None


@tool
def shopify_list_products(
    limit: Annotated[int, "Max products to return (1-250)"] = 20,
    status: Annotated[str, "active, draft, or archived"] = "active",
) -> str:
    """List Shopify products."""
    base, headers, err = _shopify_config()
    if err:
        return err
    try:
        r = httpx.get(
            f"{base}/products.json",
            headers=headers,
            params={"limit": limit, "status": status},
            timeout=10,
        )
        r.raise_for_status()
        products = r.json().get("products", [])
        lines = []
        for p in products:
            variants = p.get("variants", [])
            price = variants[0].get("price", "N/A") if variants else "N/A"
            lines.append(
                f"- [{p.get('id', '?')}] {p.get('title', '?')} | {price} | {p.get('status', '?')}"
            )
        return "\n".join(lines) if lines else "No products found."
    except Exception as exc:
        return f"[tool-error] shopify_list_products: {exc}"


@tool
def shopify_create_product(
    title: Annotated[str, "Product title"],
    description: Annotated[str, "Product description (HTML allowed)"],
    price: Annotated[str, "Price (e.g. '29.99')"],
    sku: Annotated[str, "SKU (optional)"] = "",
    vendor: Annotated[str, "Vendor name (optional)"] = "",
) -> str:
    """Create a new Shopify product."""
    base, headers, err = _shopify_config()
    if err:
        return err
    try:
        payload = {
            "product": {
                "title": title,
                "body_html": description,
                "vendor": vendor,
                "variants": [{"price": price, "sku": sku}],
            }
        }
        r = httpx.post(f"{base}/products.json", headers=headers, json=payload, timeout=15)
        r.raise_for_status()
        product = r.json().get("product", {})
        return f"Product created: [{product.get('id', '?')}] {product.get('title', '?')}"
    except Exception as exc:
        return f"[tool-error] shopify_create_product: {exc}"


@tool
def shopify_update_product(
    product_id: Annotated[str, "Shopify product ID"],
    title: Annotated[str, "New title (optional)"] = "",
    price: Annotated[str, "New price (optional)"] = "",
    status: Annotated[str, "active, draft, or archived (optional)"] = "",
) -> str:
    """Update a Shopify product."""
    base, headers, err = _shopify_config()
    if err:
        return err
    try:
        payload: dict = {"product": {"id": product_id}}
        if title:
            payload["product"]["title"] = title
        if status:
            payload["product"]["status"] = status
        if price:
            payload["product"]["variants"] = [{"price": price}]
        r = httpx.put(
            f"{base}/products/{product_id}.json", headers=headers, json=payload, timeout=15
        )
        r.raise_for_status()
        product = r.json().get("product", {})
        return f"Product updated: [{product.get('id', '?')}] {product.get('title', '?')}"
    except Exception as exc:
        return f"[tool-error] shopify_update_product: {exc}"


@tool
def shopify_list_orders(
    limit: Annotated[int, "Max orders to return"] = 20,
    status: Annotated[str, "open, closed, cancelled, or any"] = "open",
) -> str:
    """List Shopify orders."""
    base, headers, err = _shopify_config()
    if err:
        return err
    try:
        r = httpx.get(
            f"{base}/orders.json",
            headers=headers,
            params={"limit": limit, "status": status},
            timeout=10,
        )
        r.raise_for_status()
        orders = r.json().get("orders", [])
        lines = []
        for o in orders:
            lines.append(
                f"- [{o.get('id', '?')}] {(o.get('created_at') or '')[:10]} | "
                f"{o.get('email', 'N/A')} | ${o.get('total_price', '?')} | "
                f"{o.get('fulfillment_status') or 'unfulfilled'}"
            )
        return "\n".join(lines) if lines else f"No {status} orders found."
    except Exception as exc:
        return f"[tool-error] shopify_list_orders: {exc}"


@tool
def shopify_get_order(order_id: Annotated[str, "Shopify order ID"]) -> str:
    """Get details for a specific Shopify order."""
    base, headers, err = _shopify_config()
    if err:
        return err
    try:
        r = httpx.get(f"{base}/orders/{order_id}.json", headers=headers, timeout=10)
        r.raise_for_status()
        o = r.json().get("order", {})
        items = "\n".join(
            f"  - {li.get('name', '?')} x{li.get('quantity', '?')} @ ${li.get('price', '?')}"
            for li in o.get("line_items", [])
        )
        return (
            f"Order #{o.get('order_number', '?')} | {(o.get('created_at') or '')[:10]}\n"
            f"Customer: {o.get('email', 'N/A')}\n"
            f"Total: ${o.get('total_price', '?')} | Status: {o.get('financial_status', '?')}\n"
            f"Items:\n{items}"
        )
    except Exception as exc:
        return f"[tool-error] shopify_get_order: {exc}"


@tool
def shopify_get_analytics() -> str:
    """Get basic Shopify store analytics (orders count, revenue)."""
    base, headers, err = _shopify_config()
    if err:
        return err
    try:
        r = httpx.get(
            f"{base}/orders/count.json", headers=headers, params={"status": "any"}, timeout=10
        )
        r.raise_for_status()
        count = r.json().get("count", 0)
        return f"Total orders: {count}\n(Use shopify_list_orders for detailed revenue breakdown)"
    except Exception as exc:
        return f"[tool-error] shopify_get_analytics: {exc}"


@tool
def shopify_search_customers(
    query: Annotated[str, "Customer search query (e.g. email or name)"],
    limit: Annotated[int, "Max customers to return"] = 20,
) -> str:
    """Search Shopify customers."""
    base, headers, err = _shopify_config()
    if err:
        return err
    try:
        r = httpx.get(
            f"{base}/customers/search.json",
            headers=headers,
            params={"query": query, "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        customers = r.json().get("customers", [])
        if not customers:
            return f"No customers match '{query}'."
        lines = []
        for c in customers:
            lines.append(
                f"- [{c.get('id', '?')}] {c.get('first_name', '')} {c.get('last_name', '')} "
                f"<{c.get('email', '?')}> orders={c.get('orders_count', 0)} "
                f"total=${c.get('total_spent', '0')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] shopify_search_customers: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
