"""End-to-end tests for the OpenAI-compatible API connector.

The adapter is driven as a real aiohttp app over an in-process ``TestServer``
(no port binding, so the network-bind guards stay untouched) with a real
``AgentRunner`` on the offline ``fake:instant`` model — the whole path a
request takes, minus the provider.
"""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from olano.platforms.api_server import APIServerAdapter
from olano.platforms.config import PlatformConfig

API_KEY = "test-key-0123456789abcdef"  # gitleaks:allow


async def _make_runner(base_config, monkeypatch):
    monkeypatch.setenv("LIONCLAW_FAKE_TOKENS", "8")
    base_config.model = "fake:instant"

    from olano.core.builder import build_agent_async
    from olano.core.runner import AgentRunner

    agent = await build_agent_async(base_config)
    return AgentRunner(agent, base_config)


def _make_adapter(runner, api_key: str | None = API_KEY, agent_name: str = "test-agent"):
    cfg: dict = {"enabled": True}
    if api_key is not None:
        cfg["api_key"] = api_key
    adapter = APIServerAdapter(PlatformConfig.from_dict(cfg), agent_name=agent_name)
    if runner is not None:
        adapter.set_agent_runner(runner)
    return adapter


@pytest.fixture
async def client(base_config, monkeypatch):
    """A keyed adapter wired to a live fake-model runner."""
    runner = await _make_runner(base_config, monkeypatch)
    adapter = _make_adapter(runner)
    async with TestClient(TestServer(adapter._build_app())) as c:
        c.adapter = adapter  # type: ignore[attr-defined]
        c.runner = runner  # type: ignore[attr-defined]
        yield c


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"}


def _sse_events(body: str) -> list[dict]:
    """Parse ``data:`` payloads out of an SSE body, skipping [DONE]."""
    out = []
    for line in body.splitlines():
        if line.startswith("data: "):
            payload = line[6:].strip()
            if payload and payload != "[DONE]":
                out.append(json.loads(payload))
    return out


# ── auth ────────────────────────────────────────────────────────────────


async def test_missing_bearer_is_rejected(client):
    resp = await client.get("/v1/models")
    assert resp.status == 401
    assert (await resp.json())["error"]["code"] == "invalid_api_key"


async def test_wrong_bearer_is_rejected(client):
    resp = await client.get("/v1/models", headers={"Authorization": "Bearer nope"})
    assert resp.status == 401


async def test_health_needs_no_auth(client):
    resp = await client.get("/health")
    assert resp.status == 200
    assert (await resp.json())["status"] == "ok"


# ── models ──────────────────────────────────────────────────────────────


async def test_models_advertises_this_agent(client):
    """Each agent advertises its own name, not the box's default agent."""
    resp = await client.get("/v1/models", headers=_auth())
    assert resp.status == 200
    assert [m["id"] for m in (await resp.json())["data"]] == ["test-agent"]


# ── message policy ──────────────────────────────────────────────────────


async def test_message_policy_refuses_an_over_long_prompt(client):
    """`agent.json5 → message_policy` gates this server like every other
    ingress. 422 — the request is valid OpenAI, it is the content the agent's
    own policy declines — and nothing runs: a refused prompt costs nothing."""
    from olano.config.agent_config import MessagePolicyConfig

    client.runner.config.message_policy = MessagePolicyConfig(max_input_chars=10)
    resp = await client.post(
        "/v1/chat/completions",
        headers=_auth(),
        json={"model": "x", "messages": [{"role": "user", "content": "x" * 50}]},
    )
    assert resp.status == 422
    err = (await resp.json())["error"]
    assert err["code"] == "message_policy"
    assert "50" in err["message"] and "10" in err["message"]


async def test_message_policy_gates_runs_too(client):
    """`/v1/runs` is a turn with a handle; a refused prompt must not get one."""
    from olano.config.agent_config import MessagePolicyConfig

    client.runner.config.message_policy = MessagePolicyConfig(max_input_chars=10)
    resp = await client.post("/v1/runs", headers=_auth(), json={"input": "x" * 50})
    assert resp.status == 422
    assert (await resp.json())["error"]["code"] == "message_policy"
    assert not client.adapter._run_streams  # no run was registered


async def test_message_policy_gates_the_responses_api_too(client):
    from olano.config.agent_config import MessagePolicyConfig

    client.runner.config.message_policy = MessagePolicyConfig(ignore_patterns=["buy followers"])
    resp = await client.post(
        "/v1/responses",
        headers=_auth(),
        json={"model": "x", "input": "please buy followers"},
    )
    assert resp.status == 422
    assert (await resp.json())["error"]["code"] == "message_policy"


# ── chat completions ────────────────────────────────────────────────────


async def test_chat_completion_non_streaming(client):
    resp = await client.post(
        "/v1/chat/completions",
        headers=_auth(),
        json={"model": "x", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert resp.status == 200
    body = await resp.json()
    assert "Fake response" in body["choices"][0]["message"]["content"]
    assert body["usage"]["total_tokens"] > 0
    assert resp.headers.get("X-Olano-Session-Id")


async def test_chat_completion_streaming(client):
    resp = await client.post(
        "/v1/chat/completions",
        headers=_auth(),
        json={"model": "x", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status == 200
    chunks = _sse_events(await resp.text())

    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    text = "".join(
        c["choices"][0]["delta"].get("content", "")
        for c in chunks
        if c.get("choices") and "content" in c["choices"][0]["delta"]
    )
    assert "Fake response" in text
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"]["total_tokens"] > 0
    assert (await resp.text()).rstrip().endswith("data: [DONE]")


async def test_system_message_does_not_break_the_turn(client):
    """A client `system` message rides turn_directive, never the transcript."""
    resp = await client.post(
        "/v1/chat/completions",
        headers=_auth(),
        json={
            "model": "x",
            "messages": [
                {"role": "system", "content": "Be terse."},
                {"role": "user", "content": "hello"},
            ],
        },
    )
    assert resp.status == 200
    assert "Fake response" in (await resp.json())["choices"][0]["message"]["content"]


# ── session continuity ──────────────────────────────────────────────────


async def test_session_header_keeps_one_thread(client, base_config):
    """Two requests under one session id land in one transcript."""
    headers = {**_auth(), "X-Olano-Session-Id": "sess-abc"}
    for message in ("first", "second"):
        resp = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "x", "messages": [{"role": "user", "content": message}]},
        )
        assert resp.status == 200

    path = client.runner.workspace.agent_session_dir(base_config.name) / "sess-abc.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    user_rows = [r for r in rows if r["role"] == "user"]
    assert [r["content"] for r in user_rows] == ["first", "second"]
    # Provenance: the only record of which surface a turn arrived on.
    assert all(r.get("channel") == "api_server" for r in rows)


# ── keyless mode ────────────────────────────────────────────────────────


async def test_keyless_allows_local_requests(base_config, monkeypatch):
    runner = await _make_runner(base_config, monkeypatch)
    adapter = _make_adapter(runner, api_key=None)
    async with TestClient(TestServer(adapter._build_app())) as c:
        resp = await c.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status == 200


async def test_keyless_refuses_session_continuation(base_config, monkeypatch):
    """Without a key, session ids would let anyone read another conversation."""
    runner = await _make_runner(base_config, monkeypatch)
    adapter = _make_adapter(runner, api_key=None)
    async with TestClient(TestServer(adapter._build_app())) as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers={"X-Olano-Session-Id": "sess-x"},
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status == 403


def test_keyless_adapter_is_never_publicly_exposable(base_config):
    """The predicate the runtime proxy gates on."""
    assert _make_adapter(None, api_key=None).has_usable_api_key() is False
    assert _make_adapter(None, api_key="changeme").has_usable_api_key() is False
    assert _make_adapter(None, api_key="short").has_usable_api_key() is False
    assert _make_adapter(None, api_key=API_KEY).has_usable_api_key() is True


# ── runs ────────────────────────────────────────────────────────────────


async def test_runs_streams_lifecycle_events(client):
    resp = await client.post("/v1/runs", headers=_auth(), json={"input": "hello"})
    assert resp.status == 202
    run_id = (await resp.json())["run_id"]

    events = _sse_events(
        await (await client.get(f"/v1/runs/{run_id}/events", headers=_auth())).text()
    )
    kinds = [e["event"] for e in events]
    assert "message.delta" in kinds
    assert kinds[-1] == "run.completed"

    completed = events[-1]
    assert "Fake response" in completed["output"]
    assert completed["usage"]["total_tokens"] > 0


async def test_runs_requires_input(client):
    resp = await client.post("/v1/runs", headers=_auth(), json={})
    assert resp.status == 400


async def test_run_events_unknown_id_is_404(client):
    resp = await client.get("/v1/runs/run_nope/events", headers=_auth())
    assert resp.status == 404


# ── responses ───────────────────────────────────────────────────────────


async def test_responses_returns_output_items(client):
    resp = await client.post("/v1/responses", headers=_auth(), json={"input": "hello"})
    assert resp.status == 200
    body = await resp.json()
    assert body["output"][-1]["type"] == "message"
    assert "Fake response" in body["output"][-1]["content"][0]["text"]


async def test_responses_chain_reuses_one_thread(client, base_config):
    first = await (
        await client.post("/v1/responses", headers=_auth(), json={"input": "first"})
    ).json()
    resp = await client.post(
        "/v1/responses",
        headers=_auth(),
        json={"input": "second", "previous_response_id": first["id"]},
    )
    assert resp.status == 200

    session_dir = client.runner.workspace.agent_session_dir(base_config.name)
    transcripts = list(session_dir.glob("*.jsonl"))
    assert len(transcripts) == 1, "a response chain must stay on one thread"


# ── retired surfaces + degraded states ──────────────────────────────────


async def test_cron_jobs_api_is_gone(client):
    """The /api/jobs surface was backed by a package that does not exist."""
    assert (await client.get("/api/jobs", headers=_auth())).status == 404


async def test_requests_before_the_runner_is_attached_are_503(base_config):
    adapter = _make_adapter(None)
    async with TestClient(TestServer(adapter._build_app())) as c:
        resp = await c.post(
            "/v1/chat/completions",
            headers=_auth(),
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status == 503
        assert (await resp.json())["error"]["code"] == "agent_not_ready"
