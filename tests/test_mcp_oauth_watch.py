"""Tests for the chat-initiated MCP OAuth completion watcher.

The watcher (``olano.tools._mcp_oauth_watch``) is what makes MCP OAuth usable
from a messenger: after the user signs in out-of-band it hot-reloads the target
agent's MCP tools and posts a ✅/⚠️ notice back to the originating chat. These
tests exercise that finalisation logic without any network or real runtime —
the runtime and channel registry are faked.
"""

from __future__ import annotations

import pytest

from olano.core import mcp_setup
from olano.tools import _mcp_oauth_watch as watch


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    mcp_setup._OAUTH_SESSIONS.clear()
    mcp_setup._OAUTH_BY_STATE.clear()
    watch._RELOADED_SESSIONS.clear()
    watch._WATCHER_TASKS.clear()
    yield


def _mk_session(
    sid: str = "s1",
    agent: str = "researcher",
    url: str = "https://mcp.notion.com/v1",
    status: str = "authorized",
    tool_count: int = 3,
) -> mcp_setup.OAuthFlowSession:
    session = mcp_setup.OAuthFlowSession(
        session_id=sid, agent=agent, url=url, transport="streamable_http"
    )
    session.status = status
    session.tool_count = tool_count
    mcp_setup._OAUTH_SESSIONS[sid] = session
    return session


class _FakeRuntime:
    def __init__(self, ok: bool = True) -> None:
        self.calls: list[str] = []
        self._ok = ok

    async def reload_agent_mcp(self, name: str) -> dict:
        self.calls.append(name)
        return {"ok": self._ok, "running": True}


class _FakeRegistry:
    def __init__(self) -> None:
        self.sent: list = []

    async def send(self, envelope) -> str:
        self.sent.append(envelope)
        return "delivered"


def _wire(monkeypatch, runtime: object | None, registry: _FakeRegistry) -> None:
    monkeypatch.setattr("olano.tools.talk_to.get_bound_runtime", lambda: runtime)
    monkeypatch.setattr("olano.platforms.get_channel_registry", lambda: registry)


# ── happy path ───────────────────────────────────────────────────────────────


async def test_authorized_reloads_target_agent_and_notifies_chat(monkeypatch):
    _mk_session(status="authorized", tool_count=3)
    runtime, registry = _FakeRuntime(), _FakeRegistry()
    _wire(monkeypatch, runtime, registry)

    await watch._watch(
        "s1",
        "researcher",
        notify_agent="agentfather",
        notify_channel="telegram",
        notify_target="123",
    )

    # Reload targets the agent that owns the MCP, not the notifying agent.
    assert runtime.calls == ["researcher"]
    assert len(registry.sent) == 1
    env = registry.sent[0]
    assert env.channel_type == "telegram"
    assert env.target == "123"
    assert env.agent_name == "agentfather"
    assert "✅" in env.text and "researcher" in env.text and "3 tools" in env.text


async def test_single_tool_is_singularised(monkeypatch):
    _mk_session(status="authorized", tool_count=1)
    runtime, registry = _FakeRuntime(), _FakeRegistry()
    _wire(monkeypatch, runtime, registry)

    await watch._watch(
        "s1", "researcher", notify_agent="af", notify_channel="telegram", notify_target="123"
    )
    assert "1 tool)" in registry.sent[0].text


async def test_reload_not_ok_appends_restart_hint(monkeypatch):
    _mk_session(status="authorized")
    runtime, registry = _FakeRuntime(ok=False), _FakeRegistry()
    _wire(monkeypatch, runtime, registry)

    await watch._watch(
        "s1", "researcher", notify_agent="af", notify_channel="telegram", notify_target="123"
    )
    assert "restart may be needed" in registry.sent[0].text.lower()


# ── failure paths ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["error", "expired"])
async def test_failure_notifies_without_reload(monkeypatch, status):
    session = _mk_session(status=status)
    session.error = "the user denied access"
    runtime, registry = _FakeRuntime(), _FakeRegistry()
    _wire(monkeypatch, runtime, registry)

    await watch._watch(
        "s1", "researcher", notify_agent="af", notify_channel="telegram", notify_target="123"
    )
    assert runtime.calls == []  # never reload a flow that did not authorize
    assert len(registry.sent) == 1
    assert "⚠️" in registry.sent[0].text
    assert "denied access" in registry.sent[0].text


# ── gating & robustness ──────────────────────────────────────────────────────


@pytest.mark.parametrize("channel", ["", "local"])
async def test_non_channel_source_reloads_but_does_not_notify(monkeypatch, channel):
    """A CLI one-shot (local) or an unbound context has no chat to answer.

    ``api`` used to be on this list, which is what left an MCP sign-in started
    from the dashboard chat reporting to nobody — it now takes the dashboard
    fork below instead.
    """
    _mk_session(status="authorized")
    runtime, registry = _FakeRuntime(), _FakeRegistry()
    _wire(monkeypatch, runtime, registry)

    await watch._watch(
        "s1",
        "researcher",
        notify_agent="agentfather",
        notify_channel=channel,
        notify_target="owner",
    )
    assert runtime.calls == ["researcher"]
    assert registry.sent == []


async def test_no_notify_agent_skips_notification(monkeypatch):
    _mk_session(status="authorized")
    runtime, registry = _FakeRuntime(), _FakeRegistry()
    _wire(monkeypatch, runtime, registry)

    await watch._watch(
        "s1", "researcher", notify_agent="", notify_channel="telegram", notify_target="123"
    )
    assert runtime.calls == ["researcher"]
    assert registry.sent == []


async def test_no_runtime_still_notifies_success(monkeypatch):
    _mk_session(status="authorized")
    registry = _FakeRegistry()
    _wire(monkeypatch, None, registry)  # no bound runtime (CLI / tests)

    await watch._watch(
        "s1", "researcher", notify_agent="af", notify_channel="telegram", notify_target="123"
    )
    # Reload was impossible, but the user is still told the sign-in worked.
    assert len(registry.sent) == 1
    assert "✅" in registry.sent[0].text


async def test_reload_is_idempotent_per_session(monkeypatch):
    _mk_session(status="authorized")
    runtime = _FakeRuntime()
    monkeypatch.setattr("olano.tools.talk_to.get_bound_runtime", lambda: runtime)

    assert await watch._reload_agent_mcp("s1", "researcher") is not None
    assert await watch._reload_agent_mcp("s1", "researcher") is None  # guarded
    assert runtime.calls == ["researcher"]


async def test_reload_failure_is_swallowed(monkeypatch):
    _mk_session(status="authorized")

    class _BoomRuntime:
        async def reload_agent_mcp(self, name):
            raise RuntimeError("boom")

    registry = _FakeRegistry()
    _wire(monkeypatch, _BoomRuntime(), registry)

    # Must not raise; still notifies (reload_result is None → no restart hint).
    await watch._watch(
        "s1", "researcher", notify_agent="af", notify_channel="telegram", notify_target="123"
    )
    assert len(registry.sent) == 1
    assert "✅" in registry.sent[0].text


async def test_watch_polls_until_terminal(monkeypatch):
    """A still-pending flow is polled until it reaches a terminal state."""
    session = _mk_session(status="awaiting_authorization")
    runtime, registry = _FakeRuntime(), _FakeRegistry()
    _wire(monkeypatch, runtime, registry)
    monkeypatch.setattr(watch, "_POLL_INTERVAL", 0.0)

    calls = {"n": 0}
    real_poll = mcp_setup.poll_oauth_flow

    def _flipping_poll(sid):
        calls["n"] += 1
        if calls["n"] >= 3:
            session.status = "authorized"
        return real_poll(sid)

    monkeypatch.setattr(mcp_setup, "poll_oauth_flow", _flipping_poll)

    await watch._watch(
        "s1", "researcher", notify_agent="af", notify_channel="telegram", notify_target="1"
    )
    assert calls["n"] >= 3
    assert runtime.calls == ["researcher"]


async def test_vanished_session_is_noop(monkeypatch):
    runtime, registry = _FakeRuntime(), _FakeRegistry()
    _wire(monkeypatch, runtime, registry)
    # No session registered under this id.
    await watch._watch(
        "missing", "researcher", notify_agent="af", notify_channel="telegram", notify_target="1"
    )
    assert runtime.calls == []
    assert registry.sent == []


# ── spawn_watcher ────────────────────────────────────────────────────────────


def test_spawn_watcher_without_running_loop_returns_false():
    # Called from a sync context — no loop to attach the task to.
    ok = watch.spawn_watcher(
        "s1", "researcher", notify_agent="af", notify_channel="telegram", notify_target="1"
    )
    assert ok is False


async def test_spawn_watcher_schedules_and_tracks_task(monkeypatch):
    _mk_session(status="authorized")
    runtime, registry = _FakeRuntime(), _FakeRegistry()
    _wire(monkeypatch, runtime, registry)

    ok = watch.spawn_watcher(
        "s1", "researcher", notify_agent="af", notify_channel="telegram", notify_target="1"
    )
    assert ok is True
    # Let the scheduled watcher run to completion.
    import asyncio

    while watch._WATCHER_TASKS:
        await asyncio.sleep(0)
    assert runtime.calls == ["researcher"]
    assert len(registry.sent) == 1


# ── tool wiring ──────────────────────────────────────────────────────────────


async def test_tool_captures_chat_context_and_spawns_watcher(monkeypatch):
    """olano_authorize_mcp_server threads the live chat context into the watcher."""
    import olano.tools.olano_admin as la
    from olano.security.request_context import push_request_context
    from olano.tools.talk_to import current_agent

    captured = {}

    async def _fake_start(agent, url):
        return {
            "session_id": "sX",
            "status": "awaiting_authorization",
            "authorization_url": "https://auth.olano.ai/authorize?state=abc",
        }

    def _fake_spawn(session_id, agent, *, notify_agent, notify_channel, notify_target):
        captured["args"] = (session_id, agent, notify_agent, notify_channel, notify_target)
        return True

    monkeypatch.setattr(la.admin, "start_mcp_authorization", _fake_start)
    monkeypatch.setattr("olano.tools._mcp_oauth_watch.spawn_watcher", _fake_spawn)

    token = current_agent.set("agentfather")
    try:
        with push_request_context(
            source="platform", channel="telegram", user_id="u1", session_id="chat42"
        ):
            out = await la.olano_authorize_mcp_server.ainvoke(
                {"agent": "researcher", "url": "https://mcp.notion.com/v1"}
            )
    finally:
        current_agent.reset(token)

    assert captured["args"] == ("sX", "researcher", "agentfather", "telegram", "chat42")
    assert "sX" in out  # the sign-in session is still returned to the agent


async def test_tool_does_not_spawn_watcher_for_terminal_result(monkeypatch):
    """An immediate error (e.g. no relay configured) must not start a watcher."""
    import olano.tools.olano_admin as la

    async def _fake_start(agent, url):
        return {"session_id": "sE", "status": "error", "error": "no redirect"}

    spawned = {"n": 0}

    def _fake_spawn(*a, **k):
        spawned["n"] += 1
        return True

    monkeypatch.setattr(la.admin, "start_mcp_authorization", _fake_start)
    monkeypatch.setattr("olano.tools._mcp_oauth_watch.spawn_watcher", _fake_spawn)

    await la.olano_authorize_mcp_server.ainvoke(
        {"agent": "researcher", "url": "https://mcp.notion.com/v1"}
    )
    assert spawned["n"] == 0


# ── the dashboard fork ───────────────────────────────────────────────────────


class _FakeChatRunner:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append_session_entry(self, thread_id: str, entry: dict) -> None:
        self.rows.append({"thread_id": thread_id, **entry})


class _ChatRuntime(_FakeRuntime):
    """The real RuntimeServer serves both roles — the MCP hot-reload and the
    per-agent runner the transcript write goes through — so the fake must too,
    or the reload silently stops happening under the delivery test.
    """

    def __init__(self, ok: bool = True) -> None:
        super().__init__(ok=ok)
        self.chat_runner = _FakeChatRunner()

    def get_runner(self, name: str) -> _FakeChatRunner:
        return self.chat_runner


async def test_dashboard_source_reports_into_the_thread(monkeypatch):
    """The dashboard chat has no outbound socket (``APIServerPlatform.send`` is
    a stub), so its notice is persisted into the thread and pushed over SSE —
    the same fork ``magic_links.watch`` takes, for the same reason."""
    _mk_session(status="authorized")
    runtime, registry = _ChatRuntime(), _FakeRegistry()
    _wire(monkeypatch, runtime, registry)

    frames: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "olano.runtime.api._push_event",
        lambda kind, data: frames.append((kind, data)),
        raising=False,
    )

    await watch._watch(
        "s1",
        "researcher",
        notify_agent="agentfather",
        notify_channel="api",
        notify_target="thread-42",
    )

    assert runtime.calls == ["researcher"]
    assert registry.sent == []  # never through the messenger registry
    (row,) = runtime.chat_runner.rows
    assert row["thread_id"] == "thread-42"
    assert row["event"] == "magic_link"
    assert row["flow"] == "mcp-oauth"
    assert row["target_agent"] == "researcher"
    # The card reads the magic-link store's "done" as success; the OAuth
    # session's own "authorized" drew a "Didn't complete" banner.
    assert row["status"] == "done"
    assert frames[0][1]["status"] == "done"
    # The reload ran, so the tools are live and no restart is offered.
    assert row["apply"] == "reloaded"
    assert "✅" in row["content"]
    assert frames[0][0] == "magic_link_updated"
    assert frames[0][1]["agent"] == "agentfather"


async def test_dashboard_reload_failure_offers_a_restart(monkeypatch):
    """A hot-reload that did not run leaves the agent signed in and still blind
    to the server's tools — the one case worth a Restart button."""
    _mk_session(status="authorized")
    runtime, registry = _ChatRuntime(ok=False), _FakeRegistry()
    _wire(monkeypatch, runtime, registry)
    monkeypatch.setattr("olano.runtime.api._push_event", lambda kind, data: None, raising=False)

    await watch._watch(
        "s1",
        "researcher",
        notify_agent="agentfather",
        notify_channel="api",
        notify_target="thread-42",
    )
    assert runtime.chat_runner.rows[0]["apply"] == "restart_needed"
