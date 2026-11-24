"""What the dashboard needs to link a reply to its Langfuse trace, and to forget it.

- A traced reply's transcript row carries ``trace_id``, and so does the chat's
  live ``done`` frame, so the admin link works before and after a reload.
- ``GET /api/observability/langfuse/links?agent=`` says where that agent's
  traces open: the deployment's project, or its own keys' project.
- Deleting a thread deletes its traces in Langfuse, in the background, with
  the agent's keys; a Langfuse that is down never fails the delete.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from olano.config.agent_config import AgentConfig
from olano.config.home import get_olano_paths
from olano.config.loader import save_agent_config
from olano.observability import credentials
from olano.observability.deletion import delete_traces, trace_ids_in_transcript
from olano.vault.resolver import set_vault
from olano.vault.store import VaultStore

TID_A = "a" * 32
TID_B = "b" * 32


def _enable(**extra) -> None:
    path = get_olano_paths().config_path
    raw = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    raw["observability"] = {"langfuse": {"enabled": True, **extra}}
    path.write_text(yaml.safe_dump(raw))


def test_transcript_rows_carry_the_trace_id():
    from olano.runtime.api import _transcript_message

    msg = _transcript_message("merli", {"role": "assistant", "content": "hi", "trace_id": TID_A})
    assert msg["trace_id"] == TID_A
    assert "trace_id" not in _transcript_message("merli", {"role": "assistant", "content": "hi"})
    assert "trace_id" not in _transcript_message(
        "merli", {"role": "user", "content": "x", "trace_id": TID_A}
    )


def test_trace_ids_in_transcript(tmp_path: Path):
    path = tmp_path / "t.jsonl"
    rows = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "a", "trace_id": TID_A},
        {"role": "assistant", "content": "b", "trace_id": TID_A},
        {"role": "assistant", "content": "c", "trace_id": TID_B},
        {"role": "assistant", "content": "d", "trace_id": "not-hex"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    assert trace_ids_in_transcript(path) == [TID_A, TID_B]
    assert trace_ids_in_transcript(tmp_path / "missing.jsonl") == []


def test_delete_traces_uses_the_agents_keys(lionclaw_home, monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-del")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-del-secret")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://jp.cloud.langfuse.com")
    credentials._reset_for_tests()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url}")
        assert request.headers["authorization"].startswith("Basic ")
        return httpx.Response(200 if TID_A in str(request.url) else 404)

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)
    )
    # Tracing switched off since: the traces are still there and still deleted.
    assert delete_traces("merli", [TID_A, TID_B]) == 2
    assert seen == [
        f"DELETE https://jp.cloud.langfuse.com/api/public/traces/{TID_A}",
        f"DELETE https://jp.cloud.langfuse.com/api/public/traces/{TID_B}",
    ]


def test_delete_traces_without_keys_or_reachability(lionclaw_home, monkeypatch):
    credentials._reset_for_tests()
    assert delete_traces("merli", [TID_A]) == 0
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-del")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-del-secret")
    credentials._reset_for_tests()

    def down(request):
        raise httpx.ConnectError("refused")

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(down), **kw)
    )
    assert delete_traces("merli", [TID_A, TID_B]) == 0


# ── the API ───────────────────────────────────────────────────────────────────


@pytest.fixture
def vault(lionclaw_home):
    store = VaultStore.open(
        passphrase="pw", db_path=lionclaw_home / "vault-links.db", use_keyring=False
    )
    set_vault(store)
    credentials._reset_for_tests()
    yield store
    set_vault(None)
    credentials._reset_for_tests()


@pytest.fixture
def admin(lionclaw_home, vault):
    from olano.runtime.api import create_api

    save_agent_config(AgentConfig(name="merli", model="anthropic:claude-haiku-4-5-20251001"))
    save_agent_config(AgentConfig(name="solo", model="anthropic:claude-haiku-4-5-20251001"))
    runtime = MagicMock()
    r = MagicMock()
    r.config = MagicMock(model="anthropic:claude-haiku-4-5-20251001")
    r.ainvoke = AsyncMock(return_value="ok")
    runtime._runners = {"merli": r}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    # Entered, so every request shares one event loop: thread deletion hands
    # the Langfuse trace delete to a background task, which a client without
    # `with` can drop along with its per-request loop.
    with TestClient(create_api(runtime)) as client:
        client.post(
            "/api/auth/bootstrap",
            json={"email": "admin@x.com", "password": "pw", "name": "Admin"},
        )
        yield client


def test_links_follow_the_agents_project(admin, vault):
    assert admin.get("/api/observability/langfuse/links", params={"agent": "merli"}).json() == {
        "available": False,
        "traced": False,
    }
    credentials.store_credentials("pk-lf-dep", "sk-lf-dep", "https://jp.cloud.langfuse.com")
    _enable()
    out = admin.get("/api/observability/langfuse/links", params={"agent": "merli"}).json()
    assert out["available"] is False and out["reason"] == "no_project"
    assert (
        out["traced"] is True
        and out["capture"] == "metadata"
        and out["credential_source"] == "vault"
    )
    _enable(project_id="proj-dep")
    out = admin.get("/api/observability/langfuse/links", params={"agent": "merli"}).json()
    assert out["available"] is True
    assert (out["host"], out["project_id"]) == ("https://jp.cloud.langfuse.com", "proj-dep")

    # An agent with its own keys links to its own project, once tested.
    credentials.store_credentials("pk-lf-solo", "sk-lf-solo", agent="solo")
    assert (
        admin.get("/api/observability/langfuse/links", params={"agent": "solo"}).json()["available"]
        is False
    )
    credentials.store_agent_project_id("solo", "proj-solo")
    out = admin.get("/api/observability/langfuse/links", params={"agent": "solo"}).json()
    assert out["project_id"] == "proj-solo"
    assert admin.get("/api/observability/langfuse/links", params={"agent": "ghost"}).json() == {
        "available": False
    }


def test_agent_test_connection_stores_its_project(admin, vault, monkeypatch):
    def _get(url, auth=None, timeout=None):
        return httpx.Response(
            200,
            json={"data": [{"id": "proj-solo", "name": "Solo"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "get", _get)
    credentials.store_credentials("pk-lf-solo", "sk-lf-solo", agent="solo")
    body = admin.post("/api/observability/langfuse/test", json={"agent": "solo"}).json()
    assert body["ok"] is True
    assert credentials.agent_project_id("solo") == "proj-solo"
    # The deployment's project id is untouched by an agent's own test.
    from olano.config.system_settings import load_observability_settings

    assert load_observability_settings().langfuse.project_id == ""


def test_deleting_a_thread_deletes_its_traces(admin, monkeypatch):
    from olano.observability import deletion

    sessions = get_olano_paths().sessions_dir / "merli"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "t1.jsonl").write_text(
        json.dumps({"role": "user", "content": "x"})
        + "\n"
        + json.dumps({"role": "assistant", "content": "y", "trace_id": TID_A})
        + "\n"
    )
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        deletion, "delete_traces", lambda agent, ids: calls.append((agent, ids)) or len(ids)
    )
    r = admin.delete("/api/agents/merli/threads/t1")
    assert r.status_code == 200, r.text
    assert not (sessions / "t1.jsonl").exists()
    for _ in range(500):  # the delete runs in a background task
        if calls:
            break
        time.sleep(0.02)
    assert calls == [("merli", [TID_A])]


def _sse(resp) -> list[dict]:
    return [
        json.loads(line[5:])
        for line in resp.text.splitlines()
        if line.startswith("data:") and line[5:].strip()
    ]


@pytest.mark.parametrize("trace_id", [TID_A, ""])
def test_done_frame(lionclaw_home, tmp_workspace, monkeypatch, trace_id):
    from olano.config.backend_config import FilesystemBackendConfig
    from olano.core.runs import REGISTRY
    from olano.runtime.api import create_api

    REGISTRY.reset()
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    config = AgentConfig(
        name="test_agent",
        model="anthropic:claude-haiku-4-5-20251001",
        backend=FilesystemBackendConfig(root_dir="workspace"),
    )
    runner = MagicMock()
    runner.config = config

    def fake_events(payload, **kwargs):
        async def gen():
            yield ("text", "hi")

        return gen()

    runner.astream_events = fake_events
    runner.last_trace_id = lambda thread_id: trace_id
    runtime = MagicMock()
    runtime._runners = {"test_agent": runner}
    runtime.registry = MagicMock()
    runtime.registry.get.return_value = None
    client = TestClient(create_api(runtime))
    frames = _sse(client.post("/api/chat/stream", json={"message": "Hello", "agent": "test_agent"}))
    done = frames[-1]
    assert done["type"] == "done"
    if trace_id:
        assert done["trace_id"] == trace_id
    else:
        assert "trace_id" not in done
    REGISTRY.reset()
