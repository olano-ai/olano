"""The Playground is the deployment's own choice, not the plan's.

It ships off: the tab is hidden, its route never mounts, and character
generation skips the slow sprite-sheet step. Turning it on is a switch in
Config, so it has to travel the same GET/PUT the rest of that page uses.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.runtime.api import create_api


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    runtime = MagicMock()
    runtime._runners = {}
    return TestClient(create_api(runtime))


def test_defaults_off(client):
    assert client.get("/api/system/config").json()["playground_enabled"] is False
    # The dashboard nav and the route guard read it from here.
    assert client.get("/api/status").json()["capabilities"]["playground_enabled"] is False


def test_config_put_turns_it_on_and_back_off(client):
    saved = client.put("/api/system/config", json={"playground_enabled": True})
    assert saved.status_code == 200
    assert saved.json()["playground_enabled"] is True
    assert client.get("/api/system/config").json()["playground_enabled"] is True
    assert client.get("/api/status").json()["capabilities"]["playground_enabled"] is True

    off = client.put("/api/system/config", json={"playground_enabled": False})
    assert off.json()["playground_enabled"] is False
    # /api/status caches capabilities; a save must drop that cache, or the nav
    # keeps the old answer for seconds after the switch that changed it.
    assert client.get("/api/status").json()["capabilities"]["playground_enabled"] is False


def test_saving_other_fields_leaves_it_alone(client):
    client.put("/api/system/config", json={"playground_enabled": True})

    client.put("/api/system/config", json={"highlights_all_agents": True})

    assert client.get("/api/system/config").json()["playground_enabled"] is True
