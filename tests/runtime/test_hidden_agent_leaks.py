"""Endpoints that enumerate agents must never surface hidden ones.

Regression suite for the leaks that put the seven repo test fixtures in front
of customers three separate times: the Admin → Agent access table, the bulk
grant allow-list, "Start all", /api/status, the Tools Explorer usage map, and
the first-admin ownership bootstrap. All of these now inherit the safe default
from ``loader.list_agents()`` (or filter booted runners explicitly), so a
hidden agent — a shipped fixture or one an operator deliberately hid — stays
out even when it is booted via LIONCLAW_BOOT_HIDDEN_AGENTS.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.config.agent_state import is_agent_paused, set_agent_paused
from olano.config.home import get_olano_paths
from olano.runtime.api import create_api


def _write_agent(name: str, *, hidden: bool = False, tools: list[str] | None = None) -> None:
    agent_dir = get_olano_paths().agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    data: dict = {"name": name, "model": "fake:instant", "hidden": hidden}
    if tools is not None:
        data["tools"] = tools
    (agent_dir / "agent.json5").write_text(json.dumps(data), encoding="utf-8")


def _runtime(runners: dict | None = None) -> MagicMock:
    rt = MagicMock()
    rt._runners = runners or {}
    rt._channels = []
    rt._platforms = []
    rt._agents_booting = False
    rt._agents_expected = []
    rt.registry = MagicMock()
    rt.registry.get.return_value = None
    rt.scheduler = None
    return rt


def _hidden_runner() -> MagicMock:
    runner = MagicMock()
    runner.config = MagicMock(hidden=True, channels=[])
    return runner


def _visible_runner() -> MagicMock:
    runner = MagicMock()
    runner.config = MagicMock(hidden=False, channels=[])
    return runner


def _admin(app) -> TestClient:
    client = TestClient(app)
    client.post("/api/auth/bootstrap", json={"email": "admin@x.com", "password": "pw", "name": "A"})
    return client


@pytest.fixture(autouse=True)
def _empty_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the repo's own agents/ fixtures out of every roster in this module.
    monkeypatch.chdir(tmp_path)


def test_access_overview_and_bulk_exclude_hidden_agents():
    """The Admin → Agent access screen regression: hidden agents were listed
    because the disk half of its roster bypassed the hidden filter."""
    _write_agent("merli")
    _write_agent("test-agent-openai", hidden=True)
    runtime = _runtime({"test-agent-fake": _hidden_runner()})
    app = create_api(runtime)
    admin = _admin(app)

    res = admin.get("/api/admin/access-overview")
    assert res.status_code == 200
    names = {a["name"] for a in res.json()["agents"]}
    assert "merli" in names
    assert "test-agent-openai" not in names  # hidden on disk
    assert "test-agent-fake" not in names  # hidden and booted

    # The bulk grant allow-list is scoped the same way.
    uid = admin.get("/api/auth/me").json()["user"]["id"]
    res = admin.post(
        "/api/admin/access/bulk",
        json={"user_ids": [uid], "agent_names": ["test-agent-openai"], "role": "member"},
    )
    assert res.status_code == 200
    assert res.json()["changed"] == 0


def test_bootstrap_admin_grants_no_ownership_of_hidden_agents():
    _write_agent("merli")
    _write_agent("test-agent-openai", hidden=True)
    app = create_api(_runtime())
    admin = _admin(app)

    from olano.runtime.auth import get_user_store

    uid = admin.get("/api/auth/me").json()["user"]["id"]
    granted = {a["agent_name"] for a in get_user_store().db.list_agents_for_user(uid)}
    assert granted == {"merli"}


def test_system_start_skips_hidden_agents():
    """ "Start all" used to unpause and boot every agent on disk, hidden
    included — bypassing the boot loop's own filter."""
    _write_agent("merli")
    _write_agent("test-agent-fake", hidden=True)
    set_agent_paused("merli", True)
    set_agent_paused("test-agent-fake", True)

    runtime = _runtime()
    runtime._boot_agent = AsyncMock()
    app = create_api(runtime)
    admin = _admin(app)

    res = admin.post("/api/system/start")
    assert res.status_code == 200
    booted = {call.args[0] for call in runtime._boot_agent.await_args_list}
    assert booted == {"merli"}
    assert not is_agent_paused("merli")
    assert is_agent_paused("test-agent-fake")  # left exactly as it was


def test_status_excludes_hidden_runners():
    """A hidden agent booted anyway (LIONCLAW_BOOT_HIDDEN_AGENTS, explicit
    --agent) must still stay out of the public status roster."""
    runtime = _runtime({"merli": _visible_runner(), "test-agent-fake": _hidden_runner()})
    app = create_api(runtime)
    admin = _admin(app)

    res = admin.get("/api/status")
    assert res.status_code == 200
    assert "merli" in res.json()["agents"]
    assert "test-agent-fake" not in res.json()["agents"]


def test_tools_usage_excludes_hidden_agents():
    _write_agent("merli", tools=["web_search"])
    _write_agent("test-agent-fake", hidden=True, tools=["web_search"])
    app = create_api(_runtime())
    admin = _admin(app)

    res = admin.get("/api/catalog/tools/usage")
    assert res.status_code == 200
    usage = res.json()["usage"]
    referenced = {entry["agent"] for entries in usage.values() for entry in entries}
    assert referenced == {"merli"}
