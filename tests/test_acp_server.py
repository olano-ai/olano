"""ACP server must scope checkpoint keys by agent.

Every ACP handler defaulted the thread id to a shared ``acp_default`` /
``delegate_default``, so two agents driven over ACP would land on ONE checkpoint
lineage in the shared threads.db — the same cross-agent identity bleed the runner
path fixes. These assert each handler binds the thread to the routed agent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from olano.acp import server as acp_server
from olano.acp.server import ACPServer, register_agent_runner

pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer


def _mock_graph() -> AsyncMock:
    from types import SimpleNamespace

    graph = AsyncMock()
    graph.ainvoke = AsyncMock(return_value={"messages": [MagicMock(type="ai", content="hi")]})
    # A readable (empty) thread state, so the handlers' usage-recording
    # pre-read gets a real value instead of a bare AsyncMock coroutine chain.
    graph.aget_state = AsyncMock(return_value=SimpleNamespace(values={"messages": []}))
    return graph


@pytest.fixture(autouse=True)
def _clean_registry():
    acp_server._REGISTERED_AGENTS.clear()
    acp_server._TASK_STATUS.clear()
    yield
    acp_server._REGISTERED_AGENTS.clear()
    acp_server._TASK_STATUS.clear()


def _cfg_thread_id(mock_graph: AsyncMock) -> str:
    return mock_graph.ainvoke.await_args.kwargs["config"]["configurable"]["thread_id"]


async def test_chat_scopes_default_thread_per_agent() -> None:
    marcus, cindy = _mock_graph(), _mock_graph()
    srv = ACPServer(marcus, "marcus")  # registers marcus
    register_agent_runner("cindy", cindy)

    app = srv.build_http_app()
    async with TestClient(TestServer(app)) as client:
        # Same (default) thread id for BOTH agents — must not collide.
        r1 = await client.post("/acp/agents/marcus/chat", json={"message": "hi"})
        r2 = await client.post("/acp/agents/cindy/chat", json={"message": "hi"})
        assert r1.status == 200 and r2.status == 200

    assert _cfg_thread_id(marcus) == "marcus::acp_default"
    assert _cfg_thread_id(cindy) == "cindy::acp_default"
    assert _cfg_thread_id(marcus) != _cfg_thread_id(cindy)


async def test_chat_scopes_explicit_thread() -> None:
    cindy = _mock_graph()
    srv = ACPServer(_mock_graph(), "marcus")
    register_agent_runner("cindy", cindy)

    app = srv.build_http_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/acp/agents/cindy/chat", json={"message": "hi", "thread_id": "5550001234"}
        )
        assert r.status == 200

    assert _cfg_thread_id(cindy) == "cindy::5550001234"


async def test_invoke_scopes_by_self_agent() -> None:
    marcus = _mock_graph()
    srv = ACPServer(marcus, "marcus")

    app = srv.build_http_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/invoke", json={"message": "hi"})
        assert r.status == 200

    assert _cfg_thread_id(marcus) == "marcus::acp_default"


async def test_delegate_scopes_by_callee() -> None:
    cindy = _mock_graph()
    srv = ACPServer(_mock_graph(), "marcus")
    register_agent_runner("cindy", cindy)

    app = srv.build_http_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post(
            "/acp/delegate", json={"from_agent": "marcus", "to_agent": "cindy", "task": "do it"}
        )
        assert r.status == 200
        # Delegation runs the callee in a fire-and-forget task; wait for it.
        import asyncio

        for _ in range(50):
            if cindy.ainvoke.await_args is not None:
                break
            await asyncio.sleep(0.02)

    assert _cfg_thread_id(cindy) == "cindy::delegate_default"


async def test_unregister_makes_a_stopped_agent_unreachable() -> None:
    """One site routes every registered agent, so stopping an agent has to drop
    it from the registry — otherwise a torn-down graph stays callable through a
    sibling agent's ACP port."""
    from olano.acp.server import unregister_agent_runner

    srv = ACPServer(_mock_graph(), "marcus")
    register_agent_runner("cindy", _mock_graph())

    app = srv.build_http_app()
    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/acp/agents/cindy/chat", json={"message": "hi"})).status == 200
        unregister_agent_runner("cindy")
        gone = await client.post("/acp/agents/cindy/chat", json={"message": "hi"})
        assert gone.status == 404
        listed = await (await client.get("/acp/agents")).json()
        assert [a["name"] for a in listed["agents"]] == ["marcus"]


async def test_run_http_releases_the_port_on_cancel() -> None:
    """The runtime stops an agent by cancelling this task. If the AppRunner is
    never cleaned up the socket stays bound and the agent's next boot cannot
    re-bind it — an ACP toggle that works once and never again."""
    import asyncio
    import contextlib
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    srv = ACPServer(_mock_graph(), "marcus")
    task = asyncio.create_task(srv.run_http(port=port))
    for _ in range(100):
        await asyncio.sleep(0.02)
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
    else:
        task.cancel()
        raise AssertionError("ACP server never came up")

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # Re-binding must now succeed.
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))


# ── usage recording: ACP invokes run on the raw graph, so the runner never
#    ledgers them; the handlers delta-record against prior thread state ───────


def _usage_ai(content: str, mid: str, input_tokens: int, output_tokens: int):
    from langchain_core.messages import AIMessage

    return AIMessage(
        content=content,
        id=mid,
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )


def _stateful_graph(prior: list, full: list) -> AsyncMock:
    from types import SimpleNamespace

    graph = AsyncMock()
    # First read = prior ids (before the turn), second = settled state (run SSE).
    graph.aget_state = AsyncMock(
        side_effect=[
            SimpleNamespace(values={"messages": list(prior)}),
            SimpleNamespace(values={"messages": list(full)}),
        ]
    )
    graph.ainvoke = AsyncMock(return_value={"messages": list(full)})
    return graph


def _acp_events():
    from olano.analytics.usage_ledger import read_usage_events

    return [e for e in read_usage_events() if e.origin == "acp"]


async def test_chat_ledgers_only_the_new_messages() -> None:
    prior = _usage_ai("old", "m-old", 500, 50)
    new = _usage_ai("hi", "m-new", 100, 10)
    srv = ACPServer(_stateful_graph([prior], [prior, new]), "marcus")

    app = srv.build_http_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/acp/agents/marcus/chat", json={"message": "hi"})
        assert r.status == 200

    events = _acp_events()
    assert len(events) == 1
    # Only the delta ships: the prior turn's 500/50 was ledgered when it ran —
    # ACP threads persist, so ainvoke returns the WHOLE history every call.
    assert events[0].agent == "marcus"
    assert events[0].input_tokens == 100 and events[0].output_tokens == 10


async def test_run_stream_ledgers_from_the_settled_state() -> None:
    prior = _usage_ai("old", "m-old", 500, 50)
    new = _usage_ai("hi", "m-new", 200, 20)
    graph = _stateful_graph([prior], [prior, new])

    async def _agen(*args, **kwargs):
        yield (new, {})

    graph.astream = MagicMock(side_effect=lambda *a, **k: _agen())

    srv = ACPServer(graph, "marcus")
    app = srv.build_http_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/acp/agents/marcus/run", json={"message": "hi"})
        assert r.status == 200
        assert "[DONE]" in await r.text()

    events = _acp_events()
    assert len(events) == 1
    assert events[0].input_tokens == 200 and events[0].output_tokens == 20


async def test_unreadable_prior_state_skips_recording_not_the_reply() -> None:
    new = _usage_ai("hi", "m-new", 100, 10)
    graph = AsyncMock()
    graph.aget_state = AsyncMock(side_effect=RuntimeError("no checkpointer"))
    graph.ainvoke = AsyncMock(return_value={"messages": [new]})
    srv = ACPServer(graph, "marcus")

    app = srv.build_http_app()
    async with TestClient(TestServer(app)) as client:
        r = await client.post("/acp/agents/marcus/chat", json={"message": "hi"})
        assert r.status == 200  # accounting must never break the reply
        assert (await r.json())["response"] == "hi"

    # Undercount (skip), never a possible double-count.
    assert _acp_events() == []


# ── tool results reach the editor with their true status ─────────────────────


def _tool_update(text: str, status: str = "completed"):
    # Built from the schema, the way deepagents-acp builds it: the helper
    # functions the SDK once exported at its top level are gone in 1.0.
    from acp import schema

    return schema.ToolCallProgress(
        session_update="tool_call_update",
        tool_call_id="call_1",
        status=status,
        content=[
            schema.ContentToolCallContent(
                type="content", content=schema.TextContentBlock(type="text", text=text)
            )
        ],
    )


@pytest.mark.parametrize(
    "text",
    [
        "[blocked] Path is outside the workspace: /srv/project/notes.md",
        "Error: file not found: notes/missing.md",
    ],
)
def test_a_failed_tool_result_is_not_reported_completed(text: str) -> None:
    assert acp_server._with_true_status(_tool_update(text)).status == "failed"


def test_a_successful_tool_result_stays_completed() -> None:
    update = _tool_update("Updated file notes/checklist.md")
    assert acp_server._with_true_status(update) is update


async def test_the_editor_connection_gets_the_corrected_update() -> None:
    conn = MagicMock()
    conn.session_update = AsyncMock()
    client = acp_server._TrueStatusClient(conn)

    await client.session_update(
        session_id="s1", update=_tool_update("[blocked] not allowed"), source="DeepAgent"
    )

    sent = conn.session_update.await_args.kwargs
    assert sent["update"].status == "failed"
    assert sent["session_id"] == "s1" and sent["source"] == "DeepAgent"
    # Everything else on the connection passes straight through.
    assert client.request_permission is conn.request_permission
