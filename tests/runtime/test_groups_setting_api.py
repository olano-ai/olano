"""Multi-bot group threads travel the Config page's GET/PUT.

The whole group UI — the rail's Group chats section, New group, the roster
panel that adds and removes members — is gated on the `groups_enabled`
capability, which reads the `groups:` block. Before this seam existed the block
had no writer at all: a fully built feature could only be reached by hand-
editing config.yaml, which on a managed deployment (no SSH) meant it could not
be reached at all.

So what these tests guard is the round trip, the write path's clamping, and the
capability the shell reads — plus the independence that matters: both switches
ship on, and a PUT that touches one must not move the other, in either
direction.
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


def test_defaults_are_both_switches_on(client):
    block = client.get("/api/system/config").json()["groups"]
    assert block == {
        "enabled": True,
        "min_members": 2,
        "max_members": 6,
        "autonomous": True,
        "max_hops": 3,
        "cascade_timeout_seconds": 180,
    }
    # Every group surface in the simple shell reads this capability.
    assert client.get("/api/status").json()["capabilities"]["groups_enabled"] is True


def test_config_put_turns_it_off_and_back_on(client):
    saved = client.put("/api/system/config", json={"groups": {"enabled": False}})
    assert saved.status_code == 200
    assert saved.json()["groups"]["enabled"] is False
    # /api/status caches capabilities; the save must drop that cache, or the
    # rail keeps drawing a Group chats section past the switch that removed it.
    assert client.get("/api/status").json()["capabilities"]["groups_enabled"] is False
    # And the routes really are gone, not just hidden.
    assert client.get("/api/groups").status_code == 404

    on = client.put("/api/system/config", json={"groups": {"enabled": True}})
    assert on.json()["groups"]["enabled"] is True
    assert client.get("/api/status").json()["capabilities"]["groups_enabled"] is True


def test_toggling_the_surface_never_moves_the_cascade(client):
    """The two switches are independent in both directions. The one that
    would hurt: an operator who turned the cascade off must not get it back
    because they bounced the surface."""
    client.put("/api/system/config", json={"groups": {"enabled": False}})
    client.put("/api/system/config", json={"groups": {"enabled": True}})
    assert client.get("/api/system/config").json()["groups"]["autonomous"] is True

    client.put("/api/system/config", json={"groups": {"autonomous": False}})
    client.put("/api/system/config", json={"groups": {"enabled": False}})
    client.put("/api/system/config", json={"groups": {"enabled": True}})
    assert client.get("/api/system/config").json()["groups"]["autonomous"] is False


def test_partial_put_leaves_the_other_fields_alone(client):
    client.put("/api/system/config", json={"groups": {"autonomous": True, "max_hops": 5}})
    client.put("/api/system/config", json={"groups": {"max_members": 4}})

    block = client.get("/api/system/config").json()["groups"]
    assert block["autonomous"] is True
    assert block["max_hops"] == 5
    assert block["max_members"] == 4


def test_bounds_are_clamped_on_write(client):
    """What lands on disk carries the loader's own guarantees — the dashboard
    cannot persist a cascade budget a hand-edit would have been corrected on
    read."""
    client.put(
        "/api/system/config",
        json={"groups": {"max_hops": 10_000, "cascade_timeout_seconds": 99_999}},
    )
    block = client.get("/api/system/config").json()["groups"]
    assert block["max_hops"] == 10
    assert block["cascade_timeout_seconds"] == 1800

    client.put(
        "/api/system/config",
        json={"groups": {"max_hops": 0, "cascade_timeout_seconds": 1, "max_members": 99}},
    )
    block = client.get("/api/system/config").json()["groups"]
    assert block["max_hops"] == 1
    assert block["cascade_timeout_seconds"] == 10
    assert block["max_members"] == 6


def test_inverted_roster_bounds_resolve_rather_than_persist(client):
    """A max below the min would describe a group nobody can build."""
    client.put("/api/system/config", json={"groups": {"min_members": 5, "max_members": 3}})
    block = client.get("/api/system/config").json()["groups"]
    assert block["min_members"] == 5
    assert block["max_members"] == 5


def test_junk_shapes_are_400s(client):
    assert client.put("/api/system/config", json={"groups": ["on"]}).status_code == 400
    assert (
        client.put("/api/system/config", json={"groups": {"max_hops": "lots"}}).status_code == 400
    )
    assert (
        client.put(
            "/api/system/config", json={"groups": {"cascade_timeout_seconds": "a while"}}
        ).status_code
        == 400
    )


def test_unknown_keys_are_dropped_not_persisted(client):
    """A stale dashboard build cannot invent a group setting."""
    resp = client.put("/api/system/config", json={"groups": {"enabled": False, "wat": 1}})
    assert resp.status_code == 200
    assert "wat" not in client.get("/api/system/config").json()["groups"]


def test_saving_other_fields_leaves_it_alone(client):
    client.put("/api/system/config", json={"groups": {"enabled": False}})

    client.put("/api/system/config", json={"highlights_all_agents": True})

    assert client.get("/api/system/config").json()["groups"]["enabled"] is False
