"""The chat "!" shell lane (`POST /api/shell/run`) routes like the Terminal tab.

Open/unmanaged install → the runtime host. Managed posture → `docker exec` into
that agent's live sandbox (no product source, no vault), which is what the
Terminal tab has always done. Previously the managed posture simply refused with
"Host shell and package-install commands are disabled on this managed
deployment", leaving `!` unusable on every managed deployment even though a
perfectly good sandbox was running next to it.

Fail-closed is preserved: managed posture with no live sandbox still refuses,
and never downgrades to a host shell.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from deepagents.backends.protocol import ExecuteResponse
from fastapi.testclient import TestClient

from olano.orchestration.event_bus import EventBus
from olano.runtime import terminal as terminal_mod
from olano.runtime.api import create_api
from olano.webhooks.db import WebhookDB
from olano.webhooks.dispatcher import WebhookDispatcher


class FakeSandbox:
    """Stands in for a live OlanoDockerBackend bound to a container."""

    def __init__(self, ref: str = "olano-merli-abc123") -> None:
        self.ref = ref
        self.calls: list[tuple[str, int | None]] = []

    def container_ref(self) -> str | None:
        return self.ref

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        self.calls.append((command, timeout))
        return ExecuteResponse(output="sandbox stdout\nsandbox stderr\n", exit_code=0)


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    runtime = MagicMock()
    runtime._runners = {}
    runtime._agent_resources = {}
    runtime._blocked_agents = {}
    runtime.registry.get.return_value = None
    runtime.webhook_dispatcher = WebhookDispatcher(
        EventBus(), db=WebhookDB(db_path=tmp_path / "wh.db")
    )
    app = create_api(runtime)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def terminal_on(monkeypatch):
    monkeypatch.setattr(terminal_mod, "terminal_enabled", lambda: True)


def _managed(monkeypatch, sandbox: FakeSandbox | None) -> None:
    monkeypatch.setattr(terminal_mod, "terminal_uses_sandbox", lambda: True)
    monkeypatch.setattr(terminal_mod, "sandbox_backend_for", lambda agent: sandbox)


def _open_posture(monkeypatch) -> None:
    monkeypatch.setattr(terminal_mod, "terminal_uses_sandbox", lambda: False)

    def boom(agent):  # the host lane must not consult the sandbox registry
        raise AssertionError("sandbox lookup on an open-posture box")

    monkeypatch.setattr(terminal_mod, "sandbox_backend_for", boom)


def test_managed_posture_runs_in_the_agent_sandbox(client, monkeypatch):
    sandbox = FakeSandbox()
    _managed(monkeypatch, sandbox)

    resp = client.post("/api/shell/run", json={"command": "ls -la", "agent": "merli"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["exit_code"] == 0
    assert "sandbox stdout" in "\n".join(body["head"])
    assert sandbox.calls == [("ls -la", 120)]  # default timeout forwarded


def test_managed_posture_forwards_a_custom_timeout(client, monkeypatch):
    sandbox = FakeSandbox()
    _managed(monkeypatch, sandbox)

    resp = client.post(
        "/api/shell/run",
        json={"command": "sleep 1", "agent": "merli", "timeout_seconds": 5},
    )

    assert resp.status_code == 200, resp.text
    assert sandbox.calls == [("sleep 1", 5)]


def test_managed_posture_allows_pipes_and_redirects(client, monkeypatch):
    """`!` is a terminal lane, not the allowlisted /api/shell/exec surface."""
    sandbox = FakeSandbox()
    _managed(monkeypatch, sandbox)

    resp = client.post(
        "/api/shell/run",
        json={"command": "cat a.txt | grep x > out.txt && echo done", "agent": "merli"},
    )

    assert resp.status_code == 200, resp.text
    assert sandbox.calls[0][0] == "cat a.txt | grep x > out.txt && echo done"


def test_managed_posture_without_a_sandbox_refuses(client, monkeypatch):
    """Fail closed — never silently fall back to a host shell."""
    _managed(monkeypatch, None)

    resp = client.post("/api/shell/run", json={"command": "ls", "agent": "merli"})

    assert resp.status_code == 403
    assert "sandbox" in resp.json()["detail"].lower()


def test_managed_posture_with_a_dead_container_refuses(client, monkeypatch):
    """A registered backend whose container never came up is not a shell."""
    _managed(monkeypatch, FakeSandbox(ref=None))

    resp = client.post("/api/shell/run", json={"command": "ls", "agent": "merli"})

    assert resp.status_code == 403


def test_managed_run_is_logged_to_the_workspace(client, monkeypatch, tmp_path):
    """The full output lands in the workspace so the agent's fs tools can read it —
    the workspace is the same dir bind-mounted at /workspace in the sandbox."""
    sandbox = FakeSandbox()
    _managed(monkeypatch, sandbox)

    resp = client.post("/api/shell/run", json={"command": "ls", "agent": "merli"})

    log_rel = resp.json()["log_path"]
    assert log_rel and log_rel.startswith(".olano/shell_runs/")
    logs = list((tmp_path / "home" / "workspace" / ".olano" / "shell_runs").glob("*.log"))
    assert len(logs) == 1
    body = logs[0].read_text(encoding="utf-8")
    assert "sandbox:olano-merli-abc123" in body  # records WHERE it ran
    assert "sandbox stdout" in body


def test_managed_run_log_is_handed_to_the_sandbox_user(client, monkeypatch, tmp_path):
    """The log is written by the runtime as root at 0600, and the agent is told to
    read it back with its file tools. A sandboxed agent is uid 1000, so without
    the hand-off `read_file` on that path answers "permission denied" — verified
    against a real container."""
    _managed(monkeypatch, FakeSandbox())
    chowned: list[str] = []
    monkeypatch.setattr(
        "olano.core.builder.chown_for_sandbox",
        lambda p: chowned.append(str(p)) or True,
    )

    resp = client.post("/api/shell/run", json={"command": "ls", "agent": "merli"})

    log_rel = resp.json()["log_path"]
    assert log_rel
    assert any(name.endswith(log_rel.rsplit("/", 1)[-1]) for name in chowned), chowned
    assert any(name.endswith("shell_runs") for name in chowned), chowned


def test_open_posture_log_is_left_alone(client, monkeypatch):
    """On the host lane the log belongs to the runtime user — don't hand it to a
    sandbox uid that isn't running anything."""
    _open_posture(monkeypatch)
    chowned: list[str] = []
    monkeypatch.setattr(
        "olano.core.builder.chown_for_sandbox",
        lambda p: chowned.append(str(p)) or True,
    )

    resp = client.post("/api/shell/run", json={"command": "echo hi", "agent": "merli"})

    assert resp.status_code == 200, resp.text
    assert chowned == []


def test_open_posture_still_runs_on_the_host(client, monkeypatch):
    _open_posture(monkeypatch)

    resp = client.post("/api/shell/run", json={"command": "echo host-lane", "agent": "merli"})

    assert resp.status_code == 200, resp.text
    assert "host-lane" in "\n".join(resp.json()["head"])
    assert resp.json()["exit_code"] == 0


def test_terminal_disabled_refuses_in_either_posture(client, monkeypatch):
    monkeypatch.setattr(terminal_mod, "terminal_enabled", lambda: False)
    _managed(monkeypatch, FakeSandbox())

    resp = client.post("/api/shell/run", json={"command": "ls", "agent": "merli"})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "terminal_disabled"


def test_empty_command_is_rejected(client, monkeypatch):
    _managed(monkeypatch, FakeSandbox())
    resp = client.post("/api/shell/run", json={"command": "   ", "agent": "merli"})
    assert resp.status_code == 400
