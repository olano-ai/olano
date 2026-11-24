"""Stripe webhook route: signature gate, event application, idempotency.

Exercises the real per-agent secret resolution: the fake runner carries the
webhook secret in ``config.env``, which ``agent_runtime_context`` binds so
``get_credential`` inside the provider resolves it.
"""

from __future__ import annotations

import hmac
import json
import time
from hashlib import sha256
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from olano.config.agent_config import AgentConfig
from olano.monetization.store import EntitlementStatus, get_entitlement_store
from olano.monetization.webhook_stripe import register_stripe_webhook

SECRET = "whsec_test"


def _runtime() -> SimpleNamespace:
    config = AgentConfig(
        name="sicop",
        env={"STRIPE_WEBHOOK_SECRET": SECRET},
        monetization={
            "enabled": True,
            "providers": ["stripe"],
            "plan": {"stripe_price_id": "price_1"},
        },
    )
    runner = SimpleNamespace(config=config)
    return SimpleNamespace(get_runner=lambda name: runner if name == "sicop" else None)


def _client() -> TestClient:
    app = FastAPI()
    register_stripe_webhook(app, _runtime())
    return TestClient(app)


def _body(event_id: str, ref: str = "sicop:telegram:42") -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "client_reference_id": ref,
                    "subscription": "sub_1",
                    "customer": "cus_1",
                    "current_period_end": 2_000_000_000,
                }
            },
        }
    ).encode()


def _sig(body: bytes) -> str:
    ts = str(int(time.time()))
    return f"t={ts},v1={hmac.new(SECRET.encode(), f'{ts}.'.encode() + body, sha256).hexdigest()}"


def test_good_event_applies_and_returns_200() -> None:
    client = _client()
    body = _body("evt_1")
    resp = client.post(
        "/api/monetization/stripe/sicop",
        content=body,
        headers={"stripe-signature": _sig(body)},
    )
    assert resp.status_code == 200 and resp.json()["handled"] is True
    rec = get_entitlement_store().get("sicop", "telegram", "42")
    assert rec is not None and rec.status is EntitlementStatus.ACTIVE


def test_bad_signature_rejected() -> None:
    client = _client()
    body = _body("evt_2")
    resp = client.post(
        "/api/monetization/stripe/sicop",
        content=body,
        headers={"stripe-signature": "t=1,v1=deadbeef"},
    )
    assert resp.status_code == 401
    assert get_entitlement_store().get("sicop", "telegram", "42") is None


def test_unknown_agent_404() -> None:
    client = _client()
    body = _body("evt_3")
    resp = client.post(
        "/api/monetization/stripe/ghost",
        content=body,
        headers={"stripe-signature": _sig(body)},
    )
    assert resp.status_code == 404


def test_duplicate_event_is_idempotent() -> None:
    client = _client()
    body = _body("evt_dup")
    headers = {"stripe-signature": _sig(body)}
    client.post("/api/monetization/stripe/sicop", content=body, headers=headers)
    # Re-deliver the SAME event id with a canceled status — must be ignored, so
    # the stored record stays ACTIVE from the first delivery.
    cancel_body = json.dumps(
        {
            "id": "evt_dup",
            "type": "customer.subscription.deleted",
            "data": {"object": {"id": "sub_1"}},
        }
    ).encode()
    ts = str(int(time.time()))
    cancel_sig = f"t={ts},v1={hmac.new(SECRET.encode(), f'{ts}.'.encode() + cancel_body, sha256).hexdigest()}"
    client.post(
        "/api/monetization/stripe/sicop",
        content=cancel_body,
        headers={"stripe-signature": cancel_sig},
    )
    rec = get_entitlement_store().get("sicop", "telegram", "42")
    assert rec is not None and rec.status is EntitlementStatus.ACTIVE
