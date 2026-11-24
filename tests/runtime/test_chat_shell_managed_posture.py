"""The chat "!" lane under a REAL signed managed posture, not a stubbed one.

`tests/runtime/test_chat_shell_sandbox.py` stubs `terminal_uses_sandbox()` to
pick a lane. That pins the routing but not the thing that decides it: a signed
deployment license resolved through `deployment/resolver` + `enforcement`. This
module mints a real test-signed license (same machinery as
`test_deployment_api.py`) and drives the endpoint through the actual
posture/entitlement chain, so a change to how a posture is resolved shows up as
"the `!` shell went to the wrong place" rather than passing on a stale mock.

Covers all three postures the endpoint distinguishes:
  * managed + sandbox available   → runs in the sandbox
  * managed + no sandbox          → 403, never a host fallback
  * managed + terminal entitlement revoked → 403
  * open/unmanaged                → runs on the runtime host
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from deepagents.backends.protocol import ExecuteResponse
from fastapi.testclient import TestClient

from olano.deployment import enforcement, resolver
from olano.deployment.envelope import sign_license
from olano.orchestration.event_bus import EventBus
from olano.runtime import terminal as terminal_mod
from olano.runtime.api import create_api
from olano.webhooks.db import WebhookDB
from olano.webhooks.dispatcher import WebhookDispatcher

KEY_ID = "shell-posture-test-2026-07"
DEP_ID = "dep_shell_posture"
_PRIVATE = ed25519.Ed25519PrivateKey.generate()
_KEYS = {KEY_ID: _PRIVATE.public_key()}


class FakeSandbox:
    def __init__(self, ref: str | None = "olano-merli-live") -> None:
        self.ref = ref
        self.calls: list[str] = []

    def container_ref(self) -> str | None:
        return self.ref

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        self.calls.append(command)
        return ExecuteResponse(output="ran-in-sandbox\n", exit_code=0)


def _mint(settings: dict) -> str:
    return sign_license(
        {
            "schema": "olano.deployment-settings",
            "schema_version": 1,
            "issuer": "cloud.olano.ai",
            "audience": "olano-deployment-settings",
            "channel": "local",
            "deployment_id": DEP_ID,
            "plan": "pro",
            "policy_revision": 1,
            "issued_at": 1_780_000_000,
            "expires_at": 4_000_000_000,
            "settings": settings,
        },
        _PRIVATE,
        key_id=KEY_ID,
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LIONCLAW_INSTALLATION_FILE", str(tmp_path / "installation.json"))
    monkeypatch.delenv("LIONCLAW_LICENSE", raising=False)
    monkeypatch.delenv("LIONCLAW_LICENSE_PATH", raising=False)
    monkeypatch.setattr("olano.deployment.envelope.load_verify_keys", lambda: _KEYS)
    # A managed posture defaults multi-user mode ON (unset flag ⇒ login gate),
    # which would 401 every anonymous request below. These tests are about the
    # shell lane, not auth — pin the operator's explicit single-user choice,
    # which always wins over the managed default.
    from olano.config.home import update_home_config

    update_home_config({"multi_user_enabled": False})
    resolver.invalidate()
    enforcement.invalidate_counts()
    yield tmp_path
    resolver.invalidate()
    enforcement.invalidate_counts()


def _go_managed(home: Path, settings: dict) -> None:
    """Make this install genuinely managed, with signed settings."""
    (home / "installation.json").write_text(
        json.dumps({"managed": True, "mode": "local", "deployment_id": DEP_ID}),
        encoding="utf-8",
    )
    from olano.config.home import get_olano_paths

    paths = get_olano_paths()
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.deployment_license_path.write_text(_mint(settings) + "\n", encoding="utf-8")
    resolver.invalidate()
    enforcement.invalidate_counts()


@pytest.fixture
def client(home: Path, tmp_path: Path) -> TestClient:
    runtime = MagicMock()
    runtime._runners, runtime._agent_resources, runtime._blocked_agents = {}, {}, {}
    runtime.registry.get.return_value = None
    runtime.webhook_dispatcher = WebhookDispatcher(
        EventBus(), db=WebhookDB(db_path=tmp_path / "wh.db")
    )
    return TestClient(create_api(runtime), raise_server_exceptions=False)


def test_managed_posture_really_resolves_to_the_sandbox_lane(client, home, monkeypatch):
    """No stubbing of the posture — a signed managed license must route `!` into
    the agent's sandbox, which is what every managed deployment is."""
    _go_managed(home, {"features": {"sandbox_exec": True, "terminal": True, "shell": True}})
    sandbox = FakeSandbox()
    monkeypatch.setattr(terminal_mod, "sandbox_backend_for", lambda agent: sandbox)

    assert enforcement.require_sandboxed_exec() is True  # the real chain agrees
    assert terminal_mod.terminal_enabled() is True

    resp = client.post("/api/shell/run", json={"command": "whoami", "agent": "merli"})

    assert resp.status_code == 200, resp.text
    assert sandbox.calls == ["whoami"]
    assert "ran-in-sandbox" in "\n".join(resp.json()["head"])


def test_managed_posture_without_a_live_sandbox_refuses(client, home, monkeypatch):
    _go_managed(home, {"features": {"sandbox_exec": True, "terminal": True, "shell": True}})
    monkeypatch.setattr(terminal_mod, "sandbox_backend_for", lambda agent: None)

    resp = client.post("/api/shell/run", json={"command": "whoami", "agent": "merli"})

    assert resp.status_code == 403
    assert "sandbox" in resp.json()["detail"].lower()


def test_managed_posture_with_sandbox_exec_off_has_no_shell_at_all(client, home, monkeypatch):
    """`sandbox_exec` off ⇒ `terminal_enabled()` is false ⇒ no `!` lane. Managed
    boxes get no code execution until the operator validates the sandbox."""
    _go_managed(home, {"features": {"sandbox_exec": False, "terminal": True, "shell": True}})
    monkeypatch.setattr(terminal_mod, "sandbox_backend_for", lambda agent: FakeSandbox())

    assert terminal_mod.terminal_enabled() is False

    resp = client.post("/api/shell/run", json={"command": "whoami", "agent": "merli"})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "terminal_disabled"


def test_open_posture_really_resolves_to_the_host_lane(client, home, monkeypatch):
    """No installation manifest, no license ⇒ the operator's own box."""

    def boom(agent):
        raise AssertionError("open posture must not consult the sandbox registry")

    monkeypatch.setattr(terminal_mod, "sandbox_backend_for", boom)

    assert enforcement.require_sandboxed_exec() is False

    resp = client.post("/api/shell/run", json={"command": "echo host-lane", "agent": "merli"})

    assert resp.status_code == 200, resp.text
    assert "host-lane" in "\n".join(resp.json()["head"])
