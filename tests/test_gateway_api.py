"""Tests for Gateway REST API (FastAPI endpoints)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_gateway(tmp_workspace):
    """A minimal mock Gateway instance."""
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig

    config = AgentConfig(
        name="test_agent",
        model="anthropic:claude-haiku-4-5-20251001",
        backend=FilesystemBackendConfig(root_dir="workspace"),
    )
    config.emoji = "🤖"
    config.description = "Test agent"
    config.channels = []

    mock_runner = MagicMock()
    mock_runner.config = config
    mock_runner.ainvoke = AsyncMock(return_value="Hello from agent!")

    mock_scheduler = MagicMock()
    mock_scheduler.list_jobs.return_value = [
        {"id": "job1", "name": "test_job", "next_run": "2026-04-01"}
    ]

    gw = MagicMock()
    gw._runners = {"test_agent": mock_runner}
    gw._channels = []
    gw._agent_resources = {}  # real RuntimeServer has this dict; keep it concrete
    gw.scheduler = mock_scheduler
    gw.get_runner.return_value = mock_runner

    return gw


@pytest.fixture
def api_client(mock_gateway, tmp_workspace, monkeypatch):
    """Create a FastAPI TestClient bound to a mock gateway."""
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))

    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    app = create_api(mock_gateway)
    return TestClient(app)  # type: ignore[arg-type]


def test_status_endpoint(api_client):
    resp = api_client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "status" in data
    assert data["status"] == "running"
    assert "agents" in data


def test_agents_endpoint(api_client):
    # Patch list_agents so the test is isolated from any on-disk agents/ folder
    # in the cwd — we only want to assert the booted (mocked) agent shows up.
    with patch("olano.config.loader.list_agents", return_value=[]):
        resp = api_client.get("/api/agents")
    assert resp.status_code == 200
    agents = resp.json()
    booted = [a for a in agents if a.get("running")]
    assert len(booted) == 1
    assert booted[0]["name"] == "test_agent"
    assert booted[0]["emoji"] == "🤖"


def test_chat_endpoint(api_client):
    resp = api_client.post(
        "/api/chat",
        json={
            "message": "Hello",
            "agent": "test_agent",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "response" in data
    assert data["response"] == "Hello from agent!"
    assert "thread_id" in data


def test_chat_unknown_agent(api_client):
    resp = api_client.post(
        "/api/chat",
        json={
            "message": "Hello",
            "agent": "nonexistent_agent",
        },
    )
    assert resp.status_code == 404


def test_scheduler_tasks_endpoint(api_client, tmp_path, monkeypatch):
    from olano.scheduler.db import SchedulerDB

    db = SchedulerDB(db_path=tmp_path / "test.db")
    db.create_task("Test task", "Check something", schedule="0 9 * * *")

    with patch("olano.runtime.api.SchedulerDB", return_value=db):
        resp = api_client.get("/api/scheduler/tasks")

    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1
    assert tasks[0]["name"] == "Test task"


def test_create_scheduler_task(api_client):
    with patch("olano.runtime.api.SchedulerDB") as mock_db_class:
        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.create_task.return_value = "abc123"
        resp = api_client.post(
            "/api/scheduler/tasks",
            json={
                "name": "New task",
                "prompt": "Do something",
                "schedule": "0 9 * * *",
            },
        )

    assert resp.status_code == 200


def test_delete_scheduler_task(api_client, tmp_path):
    from olano.scheduler.db import SchedulerDB

    db = SchedulerDB(db_path=tmp_path / "test.db")
    task_id = db.create_task("Delete me", "Prompt", schedule="0 9 * * *")

    with patch("olano.runtime.api.SchedulerDB", return_value=db):
        resp = api_client.delete(f"/api/scheduler/tasks/{task_id}")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_delete_nonexistent_task(api_client, tmp_path):
    from olano.scheduler.db import SchedulerDB

    db = SchedulerDB(db_path=tmp_path / "test.db")

    with patch("olano.runtime.api.SchedulerDB", return_value=db):
        resp = api_client.delete("/api/scheduler/tasks/nonexistent-id")

    assert resp.status_code == 404


def test_list_files_endpoint(api_client, tmp_workspace):
    (tmp_workspace / "notes.md").write_text("# Notes", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files")

    assert resp.status_code == 200
    files = resp.json()
    names = {f["name"] for f in files}
    assert "notes.md" in names or "MEMORY.md" in names


def test_read_file_endpoint(api_client, tmp_workspace):
    (tmp_workspace / "notes.md").write_text("# Hello\nTest content.", encoding="utf-8")

    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files/notes.md")

    assert resp.status_code == 200
    data = resp.json()
    assert "Hello" in data["content"]


def test_read_nonexistent_file(api_client, tmp_workspace):
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/files/missing.txt")

    assert resp.status_code == 404


def test_memory_read_endpoint(api_client, tmp_workspace):
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.get("/api/memory")

    assert resp.status_code == 200
    data = resp.json()
    assert "content" in data


def test_memory_append_endpoint(api_client, tmp_workspace):
    with patch("olano.runtime.api._workspace_root", return_value=tmp_workspace):
        resp = api_client.post(
            "/api/memory",
            json={
                "content": "User prefers dark mode",
                "section": "Preferences",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_logs_endpoint(api_client):
    resp = api_client.get("/api/logs")
    assert resp.status_code == 200
    data = resp.json()
    assert "logs" in data


# ── Signal guided-setup endpoints ────────────────────────────────────────────


def _fake_proc(returncode: int, out: bytes = b"", err: bytes = b""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(out, err))
    proc.kill = MagicMock()
    return proc


def test_signal_detect_not_installed(api_client):
    with patch("shutil.which", return_value=None):
        resp = api_client.post("/api/connectors/signal/detect", json={})
    assert resp.status_code == 200
    assert resp.json() == {"installed": False, "version": None, "path": None}


def test_signal_detect_installed(api_client):
    proc = _fake_proc(0, out=b"signal-cli 0.14.4.1\n")
    with (
        patch("shutil.which", return_value="/opt/homebrew/bin/signal-cli"),
        patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)),
    ):
        resp = api_client.post("/api/connectors/signal/detect", json={})
    data = resp.json()
    assert data["installed"] is True
    assert data["version"] == "signal-cli 0.14.4.1"


def test_signal_register_reports_captcha_required(api_client):
    proc = _fake_proc(1, err=b"Captcha required for verification, use --captcha CAPTCHA")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        resp = api_client.post("/api/connectors/signal/register", json={"account": "+15551230000"})
    data = resp.json()
    assert data["ok"] is False
    assert data["captcha_required"] is True


def test_signal_register_short_circuits_when_daemon_live(api_client, mock_gateway):
    """A running daemon holds the account lock — register must NOT shell out
    (which would hang); it short-circuits to already_registered."""
    from olano.platforms.config import Platform

    adapter = MagicMock()
    adapter.platform = Platform.SIGNAL
    adapter.account = "+15551230000"
    adapter.client = object()  # connected
    mock_gateway._agent_resources = {"test_agent": {"platforms": [adapter]}}

    with patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
        resp = api_client.post("/api/connectors/signal/register", json={"account": "+15551230000"})
    data = resp.json()
    assert data["already_registered"] is True
    spawn.assert_not_called()  # no second signal-cli process spawned


def test_signal_register_already_registered_is_not_fatal(api_client):
    proc = _fake_proc(1, err=b"Failed to register: Account is already registered (IOException)")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        resp = api_client.post("/api/connectors/signal/register", json={"account": "+15551230000"})
    data = resp.json()
    assert data["already_registered"] is True
    assert data["captcha_required"] is False


def test_signal_verify_ok(api_client):
    proc = _fake_proc(0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as spawn:
        resp = api_client.post(
            "/api/connectors/signal/verify", json={"account": "+15551230000", "code": "123456"}
        )
    assert resp.json()["ok"] is True
    # The verification code is passed as an argv element (never a shell string).
    argv = list(spawn.call_args.args)
    assert "verify" in argv and "123456" in argv


def test_signal_profile_via_cli_when_no_daemon(api_client, mock_gateway):
    mock_gateway._agent_resources = {}  # no live managed daemon → shell path
    proc = _fake_proc(0)
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        resp = api_client.post(
            "/api/connectors/signal/profile",
            json={"account": "+15551230000", "given_name": "Aurelius"},
        )
    data = resp.json()
    assert data["ok"] is True
    assert data["via"] == "cli"
