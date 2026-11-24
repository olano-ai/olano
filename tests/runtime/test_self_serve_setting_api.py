"""Member bot creation travels the Config page's GET/PUT.

The `self_serve_agents:` block ships ON (each member gets a New-bot button out
of the box), and on a managed deployment there is no SSH — so the dashboard's
Config section is the only way an operator can turn it off, cap the per-member
quota, or pin the template menu. These tests guard that round trip, the write
path's clamping, and the /api/status capability the New-bot button reads.
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


def test_defaults_on(client):
    block = client.get("/api/system/config").json()["self_serve_agents"]
    assert block == {"enabled": True, "max_per_user": 3, "allowed_archetypes": []}
    # The simple shell's New-bot button (for members) reads this capability.
    assert client.get("/api/status").json()["capabilities"]["self_serve_agents"] is True


def test_config_put_turns_it_off_and_back_on(client):
    saved = client.put("/api/system/config", json={"self_serve_agents": {"enabled": False}})
    assert saved.status_code == 200
    assert saved.json()["self_serve_agents"]["enabled"] is False
    # /api/status caches capabilities; the save must drop that cache, or a
    # member's New-bot button outlives the switch that removed it.
    assert client.get("/api/status").json()["capabilities"]["self_serve_agents"] is False

    on = client.put("/api/system/config", json={"self_serve_agents": {"enabled": True}})
    assert on.json()["self_serve_agents"]["enabled"] is True
    assert client.get("/api/status").json()["capabilities"]["self_serve_agents"] is True


def test_partial_put_leaves_the_other_fields_alone(client):
    client.put(
        "/api/system/config",
        json={"self_serve_agents": {"max_per_user": 5, "allowed_archetypes": ["merli"]}},
    )
    client.put("/api/system/config", json={"self_serve_agents": {"enabled": False}})

    block = client.get("/api/system/config").json()["self_serve_agents"]
    assert block == {"enabled": False, "max_per_user": 5, "allowed_archetypes": ["merli"]}


def test_quota_is_clamped_on_write(client):
    """What lands on disk carries the loader's own guarantees — the dashboard
    cannot persist a value a hand-edit would have been corrected on read."""
    client.put("/api/system/config", json={"self_serve_agents": {"max_per_user": 10_000}})
    assert client.get("/api/system/config").json()["self_serve_agents"]["max_per_user"] == 100

    client.put("/api/system/config", json={"self_serve_agents": {"max_per_user": 0}})
    assert client.get("/api/system/config").json()["self_serve_agents"]["max_per_user"] == 1


def test_archetype_list_is_cleaned_on_write(client):
    client.put(
        "/api/system/config",
        json={"self_serve_agents": {"allowed_archetypes": [" merli ", "", "  "]}},
    )
    block = client.get("/api/system/config").json()["self_serve_agents"]
    assert block["allowed_archetypes"] == ["merli"]


def test_junk_shapes_are_400s(client):
    assert client.put("/api/system/config", json={"self_serve_agents": ["on"]}).status_code == 400
    assert (
        client.put(
            "/api/system/config", json={"self_serve_agents": {"max_per_user": "lots"}}
        ).status_code
        == 400
    )
    assert (
        client.put(
            "/api/system/config", json={"self_serve_agents": {"allowed_archetypes": "merli"}}
        ).status_code
        == 400
    )


def test_saving_other_fields_leaves_it_alone(client):
    client.put("/api/system/config", json={"self_serve_agents": {"enabled": False}})

    client.put("/api/system/config", json={"highlights_all_agents": True})

    assert client.get("/api/system/config").json()["self_serve_agents"]["enabled"] is False
