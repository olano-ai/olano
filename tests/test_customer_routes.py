"""The Multi-user tab's end-user list: list an agent's end users, block or
unblock one. The dashboard twin of ``olano customers list|block|unblock``."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from olano.config.agent_config import AgentConfig


@pytest.fixture
def agent():
    from olano.config.loader import save_agent_config
    from olano.memory.customer_memory import customers_base

    save_agent_config(AgentConfig(name="desk"))
    base = customers_base("desk")
    (base / "telegram-449281").mkdir(parents=True)
    (base / "telegram-449281" / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
    (base / "whatsapp-6591234567").mkdir(parents=True)
    (base / "whatsapp-6591234567" / ".customer.json").write_text(
        json.dumps({"platform": "whatsapp", "user_id": "+65 9123 4567"}), encoding="utf-8"
    )
    (base / ".hidden").mkdir()
    return "desk"


@pytest.fixture
def client():
    from olano.runtime.api import create_api

    return TestClient(create_api(MagicMock()))


def test_list_block_and_unblock(client, agent):
    rows = client.get(f"/api/agents/{agent}/customers").json()["customers"]
    assert [r["key"] for r in rows] == ["telegram-449281", "whatsapp-6591234567"]
    assert rows[0]["platform"] == "telegram" and rows[0]["user_id"] == "449281"
    assert rows[1]["user_id"] == "+65 9123 4567"
    assert not any(r["blocked"] or r["muted"] for r in rows)
    assert rows[0]["last_active"] is not None

    url = f"/api/agents/{agent}/customers/telegram-449281/blocked"
    blocked = client.put(url, json={"blocked": True})
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["customer"]["blocked"] is True
    assert blocked.json()["customer"]["blocked_at"]

    from olano.memory.customer_memory import customers_base
    from olano.memory.customer_prefs import delivery_suppressed

    assert delivery_suppressed(customers_base(agent) / "telegram-449281") == "blocked"

    unblocked = client.put(url, json={"blocked": False})
    assert unblocked.json()["customer"]["blocked"] is False
    assert delivery_suppressed(customers_base(agent) / "telegram-449281") is None


def test_bad_requests(client, agent):
    base = f"/api/agents/{agent}/customers"
    assert client.put(f"{base}/telegram-449281/blocked", json={}).status_code == 400
    assert client.put(f"{base}/telegram-449281/blocked", json={"blocked": "yes"}).status_code == 400
    assert client.put(f"{base}/nobody-1/blocked", json={"blocked": True}).status_code == 404
    assert client.put(f"{base}/.hidden/blocked", json={"blocked": True}).status_code == 404
    assert client.get("/api/agents/ghost/customers").status_code == 404


def test_set_blocked_refuses_keys_that_leave_the_store(agent):
    from olano.memory.customer_memory import customers_base
    from olano.memory.customer_prefs import PREFS_FILENAME, set_blocked

    for key in ("..", "../..", "a/../telegram-449281", ".hidden", ""):
        assert set_blocked(agent, key, True) is False, key
    assert not (customers_base(agent).parent / PREFS_FILENAME).exists()


def test_routes_are_for_admins_and_owners(client, agent, monkeypatch):
    from olano.runtime import auth

    def deny(name):
        raise HTTPException(status_code=403, detail="agent_admin_required")

    monkeypatch.setattr(auth, "assert_agent_admin", deny)
    assert client.get(f"/api/agents/{agent}/customers").status_code == 403
    put = client.put(
        f"/api/agents/{agent}/customers/telegram-449281/blocked", json={"blocked": True}
    )
    assert put.status_code == 403


def _request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "scheme": "http",
            "server": ("testserver", 80),
            "headers": [],
            "query_string": b"",
        }
    )


def test_member_middleware_leaves_the_owner_check_to_the_handler():
    from olano.runtime.auth import _member_forbidden

    assert _member_forbidden(_request("PUT", "/api/agents/desk/customers/tg-1/blocked")) is False
    # Matched by segment: an agent named "customers" gets no exemption.
    assert _member_forbidden(_request("PUT", "/api/agents/customers/config/raw")) is True
    assert _member_forbidden(_request("PUT", "/api/agents/desk/config/raw")) is True
