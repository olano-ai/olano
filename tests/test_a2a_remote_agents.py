"""Outbound A2A client (``olano/a2a``) + the ``a2a_*`` toolkit.

The remote side is a real ``a2a-sdk`` server (JSON-RPC binding, Agent Card at
the well-known path) mounted in-process behind ``httpx.ASGITransport`` — so
these tests exercise the actual protocol, not a mock of it. Policy is what the
tests pin: allow-list only, HTTPS + SSRF at both the base URL and the card's
endpoint, same-origin pinning, vault credential resolution, bounded replies,
and the activity frames the Agent to Agent view reads.
"""

from __future__ import annotations

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware

from olano.a2a import client as a2a_client
from olano.a2a.client import RemoteAgentError, RemoteAgentPolicyError
from olano.config.agent_config import AgentConfig
from olano.config.system_settings import RemoteAgentSettings
from olano.security.request_context import push_request_context

pytest.importorskip("a2a")

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Part,
    Task,
    TaskState,
    TaskStatus,
)

REMOTE = "https://remote.test"


class _EchoExecutor(AgentExecutor):
    """Minimal A2A agent: echoes the user text as a completed task artifact."""

    def __init__(self, seen: list) -> None:
        self.seen = seen

    async def execute(self, context, event_queue):  # type: ignore[override]
        self.seen.append(("ctx", context.context_id))
        if not context.current_task:
            await event_queue.enqueue_event(
                Task(
                    id=context.task_id,
                    context_id=context.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                    history=[context.message],
                )
            )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        await updater.add_artifact([Part(text=f"echo: {context.get_user_input()}")])
        await updater.complete()

    async def cancel(self, context, event_queue):  # type: ignore[override]
        raise NotImplementedError


class _RecordHeaders(BaseHTTPMiddleware):
    def __init__(self, app, seen: list) -> None:
        super().__init__(app)
        self.seen = seen

    async def dispatch(self, request, call_next):
        self.seen.append(
            (
                "req",
                request.url.path,
                request.headers.get("authorization"),
                request.headers.get("x-api-key"),
            )
        )
        return await call_next(request)


def _card(endpoint: str = f"{REMOTE}/a2a") -> AgentCard:
    return AgentCard(
        name="Echo",
        description="Echoes what it is told",
        version="1.0",
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[
            AgentInterface(url=endpoint, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[AgentSkill(id="echo", name="Echo", description="Echoes text", tags=["echo"])],
    )


def _remote_app(seen: list, card: AgentCard | None = None) -> Starlette:
    card = card or _card()
    handler = DefaultRequestHandler(
        agent_executor=_EchoExecutor(seen), task_store=InMemoryTaskStore(), agent_card=card
    )
    return Starlette(
        routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/a2a")],
        middleware=[Middleware(_RecordHeaders, seen=seen)],
    )


@pytest.fixture
def seen() -> list:
    return []


@pytest.fixture
def events(monkeypatch) -> list:
    out: list = []
    import olano.tools.talk_to as tt

    monkeypatch.setattr(tt, "_PUBLISHER", lambda t, p: out.append((t, p)))
    return out


@pytest.fixture
def remote(monkeypatch, seen):
    """Wire the client's httpx seam to an in-process a2a-sdk server."""
    a2a_client.reset_caches()

    def install(card: AgentCard | None = None):
        app = _remote_app(seen, card)
        monkeypatch.setattr(
            a2a_client,
            "_HTTPX_CLIENT_FACTORY",
            lambda entry: httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=REMOTE
            ),
        )
        return app

    install()
    yield install
    a2a_client.reset_caches()


@pytest.fixture
def agent(monkeypatch):
    """Point the client at a config with one remote agent; returns a setter."""

    def configure(**entry):
        entry = {"name": "echo", "url": REMOTE, **entry}
        cfg = AgentConfig(name="merli", remote_agents=[entry])
        monkeypatch.setattr(a2a_client, "_load_agent_config", lambda name: cfg)
        return cfg

    configure()
    return configure


@pytest.fixture
def settings(monkeypatch):
    def configure(**kw):
        monkeypatch.setattr(a2a_client, "_settings", lambda: RemoteAgentSettings(**kw))

    configure()
    return configure


# ── happy path ────────────────────────────────────────────────────────────────


async def test_send_message_roundtrip(remote, agent, settings, events, seen):
    reply = await a2a_client.send_remote_message("merli", "echo", "hello")
    assert reply.text == "echo: hello"
    assert reply.state == "TASK_STATE_COMPLETED"
    assert reply.task_id and reply.context_id
    rendered = reply.render()
    assert "echo: hello" in rendered and "state=completed" in rendered
    # Card fetched from the well-known path, then the JSON-RPC call.
    paths = [s[1] for s in seen if s[0] == "req"]
    assert paths == ["/.well-known/agent-card.json", "/a2a"]
    # Activity frames: a send and a reply, same shape talk_to_agent uses.
    phases = [(t, p.get("kind"), p.get("phase")) for t, p in events if t == "agent_message"]
    assert phases == [
        ("agent_message", "remote", "send"),
        ("agent_message", "remote", "reply"),
    ]
    send, reply_frame = [p for t, p in events if t == "agent_message"]
    assert send["from"] == "merli" and send["to"] == "a2a:echo" and send["message"] == "hello"
    assert reply_frame["reply"] == "echo: hello" and reply_frame["task_id"] == reply.task_id


async def test_bearer_credential_sent_only_on_rpc(remote, agent, settings, seen):
    agent(auth="bearer", credential="tok-123")
    await a2a_client.send_remote_message("merli", "echo", "hi")
    by_path = {s[1]: s for s in seen if s[0] == "req"}
    # The public card fetch carries no credential; the RPC call does.
    assert by_path["/.well-known/agent-card.json"][2] is None
    assert by_path["/a2a"][2] == "Bearer tok-123"


async def test_api_key_credential_uses_configured_header(remote, agent, settings, seen):
    agent(auth="api_key", credential="k-1", api_key_header="X-API-Key")
    await a2a_client.send_remote_message("merli", "echo", "hi")
    rpc = next(s for s in seen if s[0] == "req" and s[1] == "/a2a")
    assert rpc[3] == "k-1" and rpc[2] is None


async def test_vault_reference_is_resolved_per_agent(remote, agent, settings, seen, monkeypatch):
    agent(auth="bearer", credential="${vault:ECHO_TOKEN}")
    calls: list = []

    def fake_resolve(value, agent_name, vault):
        calls.append((value, agent_name))
        return "from-vault"

    import olano.core.mcp_setup as mcp_setup

    monkeypatch.setattr(mcp_setup, "_resolve_agent_ref", fake_resolve)
    monkeypatch.setattr("olano.vault.resolver.get_vault", lambda: object())
    await a2a_client.send_remote_message("merli", "echo", "hi")
    assert calls == [("${vault:ECHO_TOKEN}", "merli")]
    rpc = next(s for s in seen if s[0] == "req" and s[1] == "/a2a")
    assert rpc[2] == "Bearer from-vault"


async def test_unresolved_vault_reference_is_a_policy_error(remote, agent, settings, monkeypatch):
    agent(auth="bearer", credential="${vault:MISSING}")
    import olano.core.mcp_setup as mcp_setup

    monkeypatch.setattr(mcp_setup, "_resolve_agent_ref", lambda v, a, vault: v)
    monkeypatch.setattr("olano.vault.resolver.get_vault", lambda: object())
    with pytest.raises(RemoteAgentPolicyError, match="vault has no value"):
        await a2a_client.send_remote_message("merli", "echo", "hi")


async def test_auth_without_credential_is_refused_before_any_request(remote, agent, settings, seen):
    agent(auth="bearer", credential="")
    with pytest.raises(RemoteAgentPolicyError, match="no credential"):
        await a2a_client.send_remote_message("merli", "echo", "hi")
    assert not [s for s in seen if s[0] == "req"]


async def test_context_continues_within_one_olano_conversation(remote, agent, settings, seen):
    with push_request_context(session_id="thread-1"):
        first = await a2a_client.send_remote_message("merli", "echo", "one")
        second = await a2a_client.send_remote_message("merli", "echo", "two")
    assert first.context_id == second.context_id
    ctxs = [s[1] for s in seen if s[0] == "ctx"]
    assert ctxs == [first.context_id, first.context_id]
    # A different Olano conversation starts a fresh remote context.
    with push_request_context(session_id="thread-2"):
        third = await a2a_client.send_remote_message("merli", "echo", "three")
    assert third.context_id != first.context_id


async def test_explicit_context_id_wins(remote, agent, settings, seen):
    with push_request_context(session_id="t"):
        first = await a2a_client.send_remote_message("merli", "echo", "one")
        await a2a_client.send_remote_message("merli", "echo", "two", context_id="ctx-custom")
    ctxs = [s[1] for s in seen if s[0] == "ctx"]
    assert ctxs == [first.context_id, "ctx-custom"]


async def test_get_task_returns_settled_result(remote, agent, settings):
    sent = await a2a_client.send_remote_message("merli", "echo", "hello")
    got = await a2a_client.get_remote_task("merli", "echo", sent.task_id)
    assert got.state == "TASK_STATE_COMPLETED"
    assert got.text == "echo: hello"
    assert got.task_id == sent.task_id


async def test_reply_is_truncated_to_the_home_wide_cap(remote, agent, settings):
    settings(max_reply_chars=500)
    reply = await a2a_client.send_remote_message("merli", "echo", "x" * 900)
    assert reply.truncated and len(reply.text) <= 502
    assert "truncated" in reply.render()


async def test_discover_configured_agent(remote, agent, settings):
    card = await a2a_client.discover_agent_card("merli", "echo")
    assert card["name"] == "Echo"
    assert card["endpoints"][0]["url"] == f"{REMOTE}/a2a"
    assert card["skills"][0]["id"] == "echo"


# ── policy ────────────────────────────────────────────────────────────────────


async def test_unknown_remote_is_refused_and_lists_known(remote, agent, settings, seen):
    with pytest.raises(RemoteAgentPolicyError, match="Known: echo"):
        await a2a_client.send_remote_message("merli", "nobody", "hi")
    assert not [s for s in seen if s[0] == "req"]


async def test_disabled_entry_is_not_reachable(remote, agent, settings):
    agent(enabled=False)
    assert a2a_client.list_remote_agents("merli") == []
    with pytest.raises(RemoteAgentPolicyError, match="none configured"):
        await a2a_client.send_remote_message("merli", "echo", "hi")


async def test_home_wide_switch_off_refuses_everything(remote, agent, settings):
    settings(enabled=False)
    with pytest.raises(RemoteAgentPolicyError, match="disabled home-wide"):
        await a2a_client.send_remote_message("merli", "echo", "hi")
    with pytest.raises(RemoteAgentPolicyError, match="disabled home-wide"):
        await a2a_client.discover_agent_card("merli", "echo")


async def test_message_length_cap(remote, agent, settings):
    settings(max_message_chars=500)
    with pytest.raises(RemoteAgentPolicyError, match="limit is 500"):
        await a2a_client.send_remote_message("merli", "echo", "y" * 501)


async def test_plain_http_is_refused_unless_loopback_dev(remote, agent, settings, seen):
    agent(url="http://remote.test")
    with pytest.raises(RemoteAgentPolicyError, match="must be https"):
        await a2a_client.send_remote_message("merli", "echo", "hi")
    # Even with the dev flag on, a non-loopback host stays refused.
    agent(url="http://remote.test", allow_insecure_http=True)
    with pytest.raises(RemoteAgentPolicyError, match="must be https"):
        await a2a_client.send_remote_message("merli", "echo", "hi")
    assert not [s for s in seen if s[0] == "req"]


async def test_private_network_url_is_refused(remote, agent, settings, seen):
    agent(url="https://10.0.0.5")
    with pytest.raises(RemoteAgentPolicyError, match="private"):
        await a2a_client.send_remote_message("merli", "echo", "hi")
    agent(url="https://169.254.169.254")
    with pytest.raises(RemoteAgentPolicyError):
        await a2a_client.send_remote_message("merli", "echo", "hi")
    assert not [s for s in seen if s[0] == "req"]


async def test_card_endpoint_on_another_origin_is_refused(remote, agent, settings, seen):
    remote(_card(endpoint="https://elsewhere.test/a2a"))
    with pytest.raises(RemoteAgentPolicyError, match="different origin"):
        await a2a_client.send_remote_message("merli", "echo", "hi")
    # Only the card fetch happened — the credential never travelled.
    assert [s[1] for s in seen if s[0] == "req"] == ["/.well-known/agent-card.json"]


async def test_card_endpoint_pointing_at_private_ip_is_refused_even_when_unpinned(
    remote, agent, settings
):
    agent(same_origin_endpoint=False)
    remote(_card(endpoint="https://192.168.1.10/a2a"))
    with pytest.raises(RemoteAgentPolicyError, match="private"):
        await a2a_client.send_remote_message("merli", "echo", "hi")


async def test_card_without_endpoint_is_an_error(remote, agent, settings):
    card = _card()
    del card.supported_interfaces[:]
    remote(card)
    with pytest.raises(RemoteAgentError, match="no service endpoint"):
        await a2a_client.send_remote_message("merli", "echo", "hi")


async def test_ad_hoc_discovery_is_off_by_default(remote, agent, settings):
    with pytest.raises(RemoteAgentPolicyError, match="ad-hoc discovery"):
        await a2a_client.discover_agent_card("merli", "https://remote.test")
    settings(allow_ad_hoc_discovery=True)
    card = await a2a_client.discover_agent_card("merli", "https://remote.test")
    assert card["name"] == "Echo"


async def test_failed_call_still_leaves_a_reply_frame_with_the_error(
    remote, agent, settings, events
):
    remote(_card(endpoint="https://elsewhere.test/a2a"))
    with pytest.raises(RemoteAgentPolicyError):
        await a2a_client.send_remote_message("merli", "echo", "hi")
    frames = [p for t, p in events if t == "agent_message"]
    assert [f["phase"] for f in frames] == ["send", "reply"]
    assert frames[1]["error"] and frames[1]["reply"].startswith("[error]")


# ── toolkit ───────────────────────────────────────────────────────────────────


async def test_tools_render_replies_and_non_retryable_errors(remote, agent, settings):
    from olano.tools import a2a_tools
    from olano.tools.talk_to import current_agent

    token = current_agent.set("merli")
    try:
        listed = a2a_tools.a2a_list_agents.invoke({})
        assert "echo" in listed and REMOTE in listed
        out = await a2a_tools.a2a_send_message.ainvoke({"agent": "echo", "message": "hello"})
        assert out.startswith("echo: hello")
        bad = await a2a_tools.a2a_send_message.ainvoke({"agent": "nope", "message": "hello"})
        assert bad.startswith("[a2a error — do not retry]")
        card = await a2a_tools.a2a_discover_agent.ainvoke({"target": "echo"})
        assert '"name": "Echo"' in card
    finally:
        current_agent.reset(token)


def test_toolkit_registers_and_is_hitl_gated_as_external_write():
    from olano.security.hitl_policy import CATEGORY_TOOLS
    from olano.tools.registry import resolve_tools

    names = {t.name for t in resolve_tools(["a2a_send_message", "a2a_list_agents"])}
    assert names == {"a2a_send_message", "a2a_list_agents"}
    assert "a2a_send_message" in CATEGORY_TOOLS["write_external"]


def test_builder_grant_follows_remote_agents(base_config: AgentConfig):
    from olano.config.internal_tools import available_a2a_tool_names, effective_internal_tools
    from olano.config.ops_defaults import resolve_effective_tool_names

    assert available_a2a_tool_names(base_config) == []
    rows = {r["name"]: r for r in effective_internal_tools(base_config)}
    assert rows["a2a_send_message"]["on"] is False
    assert "remote_agents" in rows["a2a_send_message"]["note"]

    cfg = AgentConfig.model_validate(
        {**base_config.model_dump(), "remote_agents": [{"name": "echo", "url": REMOTE}]}
    )
    assert available_a2a_tool_names(cfg) == [
        "a2a_list_agents",
        "a2a_discover_agent",
        "a2a_send_message",
        "a2a_get_task",
    ]
    assert "a2a_send_message" in resolve_effective_tool_names(cfg.tools, config=cfg)
    rows = {r["name"]: r for r in effective_internal_tools(cfg)}
    assert rows["a2a_send_message"]["on"] is True
    # default_internal_tools: false turns the implicit grant off.
    off = AgentConfig.model_validate({**cfg.model_dump(), "default_internal_tools": False})
    assert available_a2a_tool_names(off) == []


def test_remote_agent_config_validation():
    with pytest.raises(ValueError):
        AgentConfig(name="x", remote_agents=[{"name": "bad name", "url": REMOTE}])
    with pytest.raises(ValueError):
        AgentConfig(name="x", remote_agents=[{"name": "ok", "url": "ftp://remote.test"}])
    cfg = AgentConfig(name="x", remote_agents=[{"name": "ok", "url": "https://r.test/"}])
    assert cfg.remote_agents[0].url == "https://r.test"
