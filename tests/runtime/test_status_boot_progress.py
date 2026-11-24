"""`/api/status` must describe a boot as progress, not as an outage.

The API binds before the agent-boot loop, so during a boot the dashboard, login
and every already-booted agent work normally. The dashboard used to render that
window as a blocking "Starting up" overlay because `booting: true` was folded
into its health state; it now needs `agents_total` alongside `agents` to say
"6 of 15 ready" instead, and to know when the wait ends.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def runtime() -> MagicMock:
    rt = MagicMock()
    rt._runners = {"marcus": MagicMock(), "andrew": MagicMock()}
    rt._channels = []
    rt._agents_booting = True
    rt._agents_expected = ["marcus", "andrew", "marisol", "peter"]
    rt.scheduler.list_jobs.return_value = []
    return rt


@pytest.fixture
def client(runtime, tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_path / "ws"))
    from olano.runtime.api import create_api

    return TestClient(create_api(runtime))


def test_status_reports_progress_while_booting(client):
    body = client.get("/api/status").json()

    assert body["booting"] is True
    assert body["status"] == "running"  # it IS serving — that is the whole point
    assert sorted(body["agents"]) == ["andrew", "marcus"]
    assert body["agents_total"] == 4


def test_total_counts_only_agents_the_runtime_will_actually_boot(client, runtime):
    """Paused / hidden / deployment-blocked agents are already excluded from
    `_agents_expected`, so the count can reach 100% instead of stalling."""
    runtime._agents_expected = ["marcus"]

    body = client.get("/api/status").json()

    assert body["agents_total"] == 1


def test_total_is_null_before_the_boot_set_is_resolved(client, runtime):
    """Never guess — an unknown total renders as an open-ended spinner."""
    runtime._agents_expected = []

    body = client.get("/api/status").json()

    assert body["agents_total"] is None


def test_finished_boot_reports_not_booting(client, runtime):
    runtime._agents_booting = False

    body = client.get("/api/status").json()

    assert body["booting"] is False
    assert body["agents_total"] == 4


def test_status_reports_data_custody(client):
    """`custody` rides on the public probe and is cheap: every box that was not
    provisioned with a sealed data volume (which includes every test home — the
    autouse fixture points the seal state at a file that does not exist) reports
    mode "none". See runtime/seal_guard.py."""
    body = client.get("/api/status").json()

    assert body["custody"] == {"mode": "none"}
