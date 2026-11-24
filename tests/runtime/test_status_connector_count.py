"""`/api/status` must count every connector, not just the legacy half.

Connectors live in two runtime registries: `_channels` (classic `BaseChannel`)
and `_platforms` (`platform:<x>` adapters). The dashboard's connector picker
suppresses every classic type, so a connector created there is *always* a
platform adapter — and the status probe counted only `_channels`. The Overview
"Channels" tile therefore read `0 · no connectors` on a box with live Telegram
and WhatsApp connectors.

The tile also needs the declared total, not just the attached one: a connector
whose token went stale attaches to nothing, and reporting only the live count
puts it back at the same wrong `0`.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.config.home import get_olano_paths
from olano.runtime.api import create_api


def _platform(value: str) -> MagicMock:
    """A booted platform adapter — identified by `adapter.platform.value`."""
    adapter = MagicMock()
    adapter.platform = SimpleNamespace(value=value)
    return adapter


def _channel(name: str) -> MagicMock:
    """A booted classic channel — identified by the `BaseChannel.name` attr."""
    channel = MagicMock()
    channel.platform = None
    channel.name = name
    return channel


def _runner(*, channels: list[str], hidden: bool = False) -> MagicMock:
    runner = MagicMock()
    runner.config = MagicMock(channels=channels, hidden=hidden)
    return runner


@pytest.fixture
def runtime() -> MagicMock:
    rt = MagicMock()
    rt._runners = {"merli": _runner(channels=["platform:telegram", "platform:whatsapp"])}
    rt._channels = []
    rt._platforms = [_platform("telegram"), _platform("whatsapp")]
    rt._agent_resources = {"merli": {"channels": [], "platforms": list(rt._platforms)}}
    rt._agents_booting = False
    rt._agents_expected = []
    rt.registry = MagicMock()
    rt.registry.get.return_value = None
    rt.scheduler = None  # len(list_jobs()) must not hit a Mock
    return rt


@pytest.fixture
def client(runtime, tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path / "ws"))
    return TestClient(create_api(runtime))


def test_platform_adapters_are_counted(client):
    """The regression: the modern connector path was invisible to the probe."""
    body = client.get("/api/status").json()

    assert sorted(body["channels"]) == ["platform:telegram", "platform:whatsapp"]
    assert body["channels_configured"] == 2


def test_classic_channels_still_counted_and_named_by_type(client, runtime):
    """Legacy configs keep working, and both kinds report the type the agent
    config spells — never the adapter's Python class name."""
    runtime._channels = [_channel("telegram")]
    runtime._platforms = [_platform("whatsapp")]
    runtime._runners = {"merli": _runner(channels=["telegram", "platform:whatsapp"])}
    runtime._agent_resources = {
        "merli": {"channels": list(runtime._channels), "platforms": list(runtime._platforms)}
    }

    body = client.get("/api/status").json()

    assert sorted(body["channels"]) == ["platform:whatsapp", "telegram"]
    assert body["channels_configured"] == 2


def test_configured_total_survives_a_connector_that_failed_to_attach(client, runtime):
    """A stale token boots to no adapter. The declared total still reports it,
    so the tile says "2 · 1 active" instead of hiding it behind the live count."""
    runtime._platforms = [_platform("telegram")]
    runtime._agent_resources = {"merli": {"channels": [], "platforms": list(runtime._platforms)}}

    body = client.get("/api/status").json()

    assert body["channels"] == ["platform:telegram"]
    assert body["channels_configured"] == 2


def test_hidden_agents_do_not_inflate_the_configured_total(client, runtime):
    """The boot path never binds a hidden agent's connectors, so counting them
    would leave the declared total permanently unreachable."""
    runtime._runners["probe"] = _runner(channels=["platform:slack"], hidden=True)

    body = client.get("/api/status").json()

    assert body["channels_configured"] == 2


def test_no_connectors_reads_as_zero(client, runtime):
    runtime._runners = {"merli": _runner(channels=[])}
    runtime._channels = []
    runtime._platforms = []
    runtime._agent_resources = {"merli": {"channels": [], "platforms": []}}

    body = client.get("/api/status").json()

    assert body["channels"] == []
    assert body["channels_configured"] == 0


# ── scoping: connectors follow the roster ────────────────────────────────────


@pytest.fixture
def scoped_runtime(runtime) -> MagicMock:
    runtime._runners = {
        "merli": _runner(channels=["platform:telegram"]),
        "scout": _runner(channels=["platform:discord"]),
    }
    runtime._platforms = [_platform("telegram"), _platform("discord")]
    runtime._agent_resources = {
        "merli": {"channels": [], "platforms": [runtime._platforms[0]]},
        "scout": {"channels": [], "platforms": [runtime._platforms[1]]},
    }
    return runtime


@pytest.fixture
def scoped_client(scoped_runtime, tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path / "ws"))
    for name in ("merli", "scout"):
        d = get_olano_paths().agents_dir / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "agent.json5").write_text(json.dumps({"name": name}), encoding="utf-8")
    return TestClient(create_api(scoped_runtime))


def test_connectors_are_scoped_like_the_roster(scoped_client):
    """A member sees the connectors of the agents they were granted, an
    anonymous caller (the pre-login readiness probe) sees none at all."""
    app = scoped_client.app
    admin = TestClient(app)
    admin.post(
        "/api/auth/bootstrap", json={"email": "admin@x.com", "password": "pw", "name": "Admin"}
    )
    uid = admin.post(
        "/api/admin/users", json={"email": "bob@x.com", "password": "pw", "name": "Bob"}
    ).json()["user"]["id"]
    assert admin.post("/api/agents/merli/access", json={"user_id": uid}).status_code == 200
    member = TestClient(app)
    member.post("/api/auth/login", json={"email": "bob@x.com", "password": "pw"})

    assert sorted(admin.get("/api/status").json()["channels"]) == [
        "platform:discord",
        "platform:telegram",
    ]

    member_body = member.get("/api/status").json()
    assert member_body["channels"] == ["platform:telegram"]
    assert member_body["channels_configured"] == 1

    anon_body = TestClient(app).get("/api/status").json()
    assert anon_body["status"] == "running"
    assert anon_body["channels"] == []
    assert anon_body["channels_configured"] == 0
