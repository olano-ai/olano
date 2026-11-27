"""Look up a Stripe Checkout session in the demo's Stripe sandbox (test mode only).

    python stripe-session.py <checkout session id>

Uses OLANO_STRIPE_SANDBOX_SECRET_KEY and refuses any key that is not a test-mode key.
Used by ``flow-monetization.mjs``.
"""

import os
import sys

import httpx

key = os.environ.get("OLANO_STRIPE_SANDBOX_SECRET_KEY", "")
if not key.startswith("sk_test_"):
    sys.exit("refusing: not a test-mode key")
sid = sys.argv[1]
s = httpx.get(
    f"https://api.stripe.com/v1/checkout/sessions/{sid}",
    params={"expand[]": "line_items.data.price.product"},
    auth=(key, ""),
    timeout=30,
).json()
item = (s.get("line_items") or {}).get("data", [{}])[0]
price = item.get("price") or {}
product = price.get("product") or {}
rows = [
    ("id", s["id"][:30] + "…"),
    ("livemode", str(s["livemode"]).lower()),
    ("mode", s["mode"]),
    ("status", s["status"]),
    (
        "amount",
        f"{s['amount_total'] / 100:.2f} {s['currency'].upper()} / {price.get('recurring', {}).get('interval', '')}",
    ),
    ("price", price.get("id", "")),
    ("product", product.get("name", "")),
    ("client_reference_id", s.get("client_reference_id") or ""),
]
for k, v in rows:
    print(f"{k:<20} {v}")
