"""A chat call to an agent with no live runner says why, instead of "not found".

A stopped agent used to answer ``404 Agent '<name>' not found``: the phone
took the message and then claimed the agent did not exist.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.config.agent_state import set_agent_paused
from olano.config.home import get_olano_paths


def _seed(name: str, display: str | None = None) -> None:
    agent_dir = get_olano_paths().agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    body = {"name": name, "model": "fake:instant"}
    if display:
        body["display_name"] = display
    (agent_dir / "agent.json5").write_text(json.dumps(body))


@pytest.fixture
def runtime():
    rt = MagicMock()
    rt._runners = {}
    rt._blocked_agents = {}
    rt.registry.get.return_value = None
    return rt


@pytest.fixture
def client(tmp_path, monkeypatch, runtime):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    from olano.runtime.api import create_api

    return TestClient(create_api(runtime))


def _send(client: TestClient, agent: str):
    return client.post("/api/chat/stream", json={"agent": agent, "message": "hi"})


def test_unknown_agent_is_still_not_found(client) -> None:
    resp = _send(client, "ghost")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_stopped_agent_says_it_is_stopped(client) -> None:
    _seed("sebas", display="Sebas")
    set_agent_paused("sebas", True)
    resp = _send(client, "sebas")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "agent_not_running"
    assert detail["state"] == "paused"
    assert detail["message"].startswith("Sebas is stopped")
    assert detail["can_start"] is True  # single-user: the owner is the admin


def test_failed_boot_and_plan_limit_are_told_apart(client, runtime) -> None:
    _seed("ana")
    runtime.registry.get.return_value = SimpleNamespace(status="error", error="no API key")
    detail = _send(client, "ana").json()["detail"]
    assert detail["state"] == "error"
    assert detail["reason"] == "no API key"

    runtime.registry.get.return_value = None
    runtime._blocked_agents = {"ana": "max_agents cap"}
    detail = _send(client, "ana").json()["detail"]
    assert detail["state"] == "blocked"
    assert detail["can_start"] is False  # starting it would hit the same cap


def test_booting_agent_asks_for_a_retry(client, runtime) -> None:
    _seed("navi")
    runtime.registry.get.return_value = SimpleNamespace(status="starting", error="")
    resp = _send(client, "navi")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "agent_starting"
