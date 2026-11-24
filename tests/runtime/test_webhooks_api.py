"""HTTP-level tests for the webhook API surface.

`tests/test_webhooks.py` covers the dispatcher / DB / config model directly.
This file exercises the FastAPI endpoints an operator (and external senders)
actually hit: CRUD over ``agent.json5``'s ``webhooks`` list, the live Test
button, per-webhook logs, and the self-authenticating public ingress endpoint
(``/api/webhooks/ingress/…``) — token auth, body-size cap, and the
respond / fire-and-forget branches.

The agent config is backed by an in-memory store (monkeypatched
``_read_agent_config`` / ``_write_agent_config``) so no ``agent.json5`` files
touch the repo, and a scratch :class:`WebhookDB` is shared between the runtime
dispatcher and the ``get_webhook_db()`` singleton the read endpoints use.
"""

from __future__ import annotations

import copy
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import olano.runtime.api as api
import olano.webhooks.db as webhook_db_mod
from olano.orchestration.event_bus import EventBus
from olano.runtime.api import create_api
from olano.webhooks.db import WebhookDB
from olano.webhooks.dispatcher import WebhookDispatcher


@pytest.fixture
def store() -> dict[str, dict]:
    """In-memory ``name -> agent.json5 dict`` backing the config read/write."""
    return {"scout": {"name": "scout"}}


@pytest.fixture
def runner() -> MagicMock:
    """A booted agent runner whose ``ainvoke`` returns a canned reply."""
    r = MagicMock()
    r.ainvoke = AsyncMock(return_value="agent handled it")
    return r


@pytest.fixture
def app(
    store: dict[str, dict],
    runner: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_read(name: str) -> dict:
        if name not in store:
            raise FileNotFoundError(name)
        return copy.deepcopy(store[name])

    def fake_write(name: str, data: dict, create: bool = False) -> Path:
        data.setdefault("name", name)
        store[name] = copy.deepcopy(data)
        return tmp_path / f"{name}.json5"

    monkeypatch.setattr(api, "_read_agent_config", fake_read)
    monkeypatch.setattr(api, "_write_agent_config", fake_write)

    # Share one scratch DB between the dispatcher (which records deliveries) and
    # the get_webhook_db() singleton the list/logs endpoints read from.
    db = WebhookDB(db_path=tmp_path / "wh.db")
    monkeypatch.setattr(webhook_db_mod, "_DEFAULT_DB", db)

    runtime = MagicMock()
    runtime._runners = {"scout": runner}
    runtime._agent_resources = {}
    runtime.webhook_dispatcher = WebhookDispatcher(EventBus(), db=db)
    return create_api(runtime)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


# ───────────────────────────────── CRUD ──────────────────────────────────────


def test_add_list_outbound_webhook_redacts_secret(client: TestClient) -> None:
    r = client.post(
        "/api/agents/scout/webhooks",
        json={
            "name": "pager",
            "url": "https://hooks.example.com/x",
            "events": ["agent.error"],
            "secret": "topsecret",
        },
    )
    assert r.status_code == 200
    wid = r.json()["id"]

    listing = client.get("/api/agents/scout/webhooks")
    assert listing.status_code == 200
    body = listing.json()
    assert "agent.error" in body["available_events"]
    hooks = body["webhooks"]
    assert len(hooks) == 1
    hook = hooks[0]
    assert hook["id"] == wid
    # Secret is never leaked; a boolean flag stands in for it.
    assert hook["secret"] == "••••••••"
    assert hook["has_secret"] is True
    assert "topsecret" not in listing.text
    # Fresh webhook → zeroed stats, no ingress path for outbound.
    assert hook["stats"]["total"] == 0
    assert "ingress_path" not in hook


def test_add_rejects_bad_url(client: TestClient) -> None:
    r = client.post(
        "/api/agents/scout/webhooks",
        json={"name": "bad", "url": "ftp://nope", "events": ["*"]},
    )
    assert r.status_code == 400
    assert "Invalid webhook" in r.json()["detail"]


def test_add_rejects_duplicate_name(client: TestClient) -> None:
    payload = {"name": "dup", "url": "https://x.com/y"}
    assert client.post("/api/agents/scout/webhooks", json=payload).status_code == 200
    dup = client.post("/api/agents/scout/webhooks", json=payload)
    assert dup.status_code == 409


def test_add_unknown_agent_is_404(client: TestClient) -> None:
    r = client.post("/api/agents/ghost/webhooks", json={"name": "w", "url": "https://x.com/y"})
    assert r.status_code == 404


def test_update_preserves_secret_and_ingress_token(
    client: TestClient, store: dict[str, dict]
) -> None:
    wid = client.post(
        "/api/agents/scout/webhooks",
        json={"name": "in", "direction": "inbound", "prompt": "handle it"},
    ).json()["id"]
    token = store["scout"]["webhooks"][0]["ingress_token"]

    # secret omitted (None) on update must not wipe anything; ingress token is stable.
    r = client.put(
        f"/api/agents/scout/webhooks/{wid}",
        json={"name": "in", "direction": "inbound", "prompt": "handle it differently"},
    )
    assert r.status_code == 200
    entry = store["scout"]["webhooks"][0]
    assert entry["prompt"] == "handle it differently"
    assert entry["ingress_token"] == token


def test_update_unknown_webhook_is_404(client: TestClient) -> None:
    r = client.put(
        "/api/agents/scout/webhooks/nope",
        json={"name": "x", "url": "https://x.com/y"},
    )
    assert r.status_code == 404


def test_delete_webhook(client: TestClient, store: dict[str, dict]) -> None:
    wid = client.post(
        "/api/agents/scout/webhooks", json={"name": "w", "url": "https://x.com/y"}
    ).json()["id"]

    r = client.delete(f"/api/agents/scout/webhooks/{wid}")
    assert r.status_code == 200
    assert store["scout"]["webhooks"] == []
    # Deleting again is a 404.
    assert client.delete(f"/api/agents/scout/webhooks/{wid}").status_code == 404


# ─────────────────────────── logs + test button ──────────────────────────────


def test_logs_endpoint_returns_history(client: TestClient) -> None:
    wid = client.post(
        "/api/agents/scout/webhooks",
        json={"name": "in", "direction": "inbound", "prompt": "p"},
    ).json()["id"]

    # A test delivery of an inbound hook logs one row (agent not invoked here
    # because _run_webhook_test only dispatches when the agent is "running";
    # scout *is* in _runners, so it runs and returns the canned reply).
    tested = client.post(f"/api/agents/scout/webhooks/{wid}/test")
    assert tested.status_code == 200
    assert tested.json()["result"]["ok"] is True

    logs = client.get(f"/api/agents/scout/webhooks/{wid}/logs")
    assert logs.status_code == 200
    rows = logs.json()["logs"]
    assert len(rows) == 1
    assert rows[0]["direction"] == "inbound"
    assert rows[0]["is_test"] is True
    assert rows[0]["response_body"] == "agent handled it"


def test_adhoc_inbound_test_runs_agent(client: TestClient, runner: MagicMock) -> None:
    r = client.post(
        "/api/agents/scout/webhooks/test",
        json={"name": "probe", "direction": "inbound", "prompt": "summarise"},
    )
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["ok"] is True
    assert result["response"] == "agent handled it"
    # The agent saw the prompt + the sample payload.
    message = runner.ainvoke.call_args.args[0]
    assert "summarise" in message
    assert "Test payload from Olano" in message


# ───────────────────────────────── ingress ───────────────────────────────────


def _make_inbound(client: TestClient, store: dict[str, dict], **over) -> tuple[str, str]:
    """Create an inbound webhook, return (webhook_id, ingress_path)."""
    payload = {"name": "ingest", "direction": "inbound", "prompt": "process"}
    payload.update(over)
    wid = client.post("/api/agents/scout/webhooks", json=payload).json()["id"]
    path = client.get("/api/agents/scout/webhooks").json()["webhooks"][0]["ingress_path"]
    return wid, path


def test_ingress_respond_runs_agent_and_returns_reply(
    client: TestClient, store: dict[str, dict]
) -> None:
    _, path = _make_inbound(client, store, respond=True)
    r = client.post(path, json={"hello": "world"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["response"] == "agent handled it"
    assert body["thread_id"]


def test_ingress_fire_and_forget_acks_202(client: TestClient, store: dict[str, dict]) -> None:
    _, path = _make_inbound(client, store, respond=False)
    r = client.post(path, json={"hello": "world"})
    assert r.status_code == 202
    assert r.json() == {"ok": True, "queued": True}


def test_ingress_wrong_token_is_404(client: TestClient, store: dict[str, dict]) -> None:
    _, path = _make_inbound(client, store)
    bad = path.rsplit("/", 1)[0] + "/deadbeefdeadbeefdeadbeefdeadbeef"
    r = client.post(bad, json={})
    assert r.status_code == 404


def test_ingress_disabled_webhook_is_404(client: TestClient, store: dict[str, dict]) -> None:
    _, path = _make_inbound(client, store)
    store["scout"]["webhooks"][0]["enabled"] = False
    r = client.post(path, json={})
    assert r.status_code == 404


def test_ingress_body_too_large_is_413(client: TestClient, store: dict[str, dict]) -> None:
    _, path = _make_inbound(client, store, max_body_bytes=32, respond=True)
    r = client.post(path, content=b"x" * 500)
    assert r.status_code == 413


def test_ingress_rejects_outbound_hook_id(client: TestClient, store: dict[str, dict]) -> None:
    """An outbound webhook's id must not be reachable via the ingress URL."""
    wid = client.post(
        "/api/agents/scout/webhooks",
        json={"name": "out", "url": "https://x.com/y"},
    ).json()["id"]
    # Fabricate an ingress URL for the outbound hook — direction guard → 404.
    r = client.post(f"/api/webhooks/ingress/scout/{wid}/whatever", json={})
    assert r.status_code == 404
