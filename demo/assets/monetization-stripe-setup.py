"""One-off: create the demo's test-mode product and price in the Stripe sandbox (sk_test_ only)."""

import json
import os

import httpx

key = os.environ["OLANO_STRIPE_SANDBOX_SECRET_KEY"]
assert key.startswith("sk_test_"), "not a test-mode key"
api = httpx.Client(base_url="https://api.stripe.com/v1", auth=(key, ""), timeout=30)
name = "Olano demo: Lumi Spanish tutor monthly"
found = api.get("/products/search", params={"query": f"name:'{name}' AND active:'true'"}).json()
if found.get("data"):
    prod = found["data"][0]
else:
    prod = api.post(
        "/products",
        data={
            "name": name,
            "description": "Demo product for an Olano product video (test mode only).",
        },
    ).json()
prices = api.get("/prices", params={"product": prod["id"], "active": "true"}).json()["data"]
price = (
    prices[0]
    if prices
    else api.post(
        "/prices",
        data={
            "product": prod["id"],
            "unit_amount": 900,
            "currency": "usd",
            "recurring[interval]": "month",
            "nickname": "Olano demo monthly",
        },
    ).json()
)
print(
    json.dumps(
        {
            "product": prod["id"],
            "product_livemode": prod["livemode"],
            "price": price["id"],
            "price_livemode": price["livemode"],
            "amount": price["unit_amount"],
            "currency": price["currency"],
            "interval": price["recurring"]["interval"],
        }
    )
)
