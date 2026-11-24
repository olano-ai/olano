"""The monetization config round-trips through PUT/GET /api/agents/{name}.

Regression guard for the ``AgentUpdateRequest`` allow-list: before wiring the
dashboard Monetization tab, a PUT carrying ``monetization`` was silently dropped
because the request model didn't declare the field.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.runtime.api import create_api


def _runner(model="anthropic:claude-haiku-4-5-20251001"):
    r = MagicMock()
    r.config = MagicMock(model=model)
    r.ainvoke = AsyncMock(return_value="ok")
    return r


@pytest.fixture
def app():
    runtime = MagicMock()
    runtime._runners = {"merli": _runner()}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    return create_api(runtime)


def _admin(app) -> TestClient:
    c = TestClient(app)
    c.post("/api/auth/bootstrap", json={"email": "admin@x.com", "password": "pw", "name": "Admin"})
    return c


def _write_agent(name: str) -> None:
    # The API resolves agent.json5 via ``Path.home()/.lionclaw/agents`` (the test
    # points HOME at a tmp dir), not LIONCLAW_HOME.
    agent_dir = Path.home() / ".lionclaw" / "agents" / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.json5").write_text(json.dumps({"name": name}), encoding="utf-8")


def test_monetization_round_trips_through_put(app, tmp_path, monkeypatch):
    # Align the two config-path resolvers: PUT uses Path.home()/.lionclaw/agents,
    # GET uses get_olano_home()/agents. Point both at the same tmp dir.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / ".lionclaw"))
    _write_agent("merli")
    admin = _admin(app)

    body = {
        "monetization": {
            "enabled": True,
            "providers": ["stripe", "telegram_stars"],
            "trial_days": 5,
            "plan": {"price_cents": 1500, "stripe_price_id": "price_1", "stars_amount": 750},
        }
    }
    r = admin.put("/api/agents/merli", json=body)
    assert r.status_code == 200, r.text

    got = admin.get("/api/agents/merli").json()
    mon = got["monetization"]
    assert mon["enabled"] is True
    assert mon["trial_days"] == 5
    assert set(mon["providers"]) == {"stripe", "telegram_stars"}
    assert mon["plan"]["price_cents"] == 1500
    assert mon["plan"]["stripe_price_id"] == "price_1"
    assert mon["plan"]["stars_amount"] == 750
