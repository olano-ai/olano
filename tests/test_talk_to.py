"""Unit tests for the talk_to_agent peer messaging primitive.

These cover the contract that downstream agents rely on:
- Name resolution (case-insensitive + near-miss suggestion).
- Thread routing (sync/cold isolate on a per-pair ``acp::<caller>::<callee>``
  thread, async callbacks land on the originating user thread via the pair-map).
- Idempotency: in-flight dedup + cooldown cache.
- Context propagation: ``originator_user_thread`` carries through nested
  hops of an async callback chain but does NOT leak into sync calls.
- Per-pair conversation isolation + the opt-in shared digest.

The runner is mocked end-to-end — we never need a real LangGraph here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from olano.security.request_context import push_request_context
from olano.tools import talk_to as tt

# ── Fixtures ────────────────────────────────────────────────────────────────


@dataclass
class _FakeCfg:
    name: str
    description: str = ""
    display_name: str = ""
    emoji: str = "🦁"
    tools: list[str] = field(default_factory=list)
    hidden: bool = False

    @property
    def label(self) -> str:
        """Mirrors AgentConfig.label so the roster/resolver see the real shape."""
        return self.display_name.strip() or self.name


class _FakeRunner:
    """Records every ainvoke call so tests can assert routing decisions."""

    def __init__(
        self,
        name: str,
        reply: str | None = None,
        delay: float = 0.0,
        display_name: str = "",
    ) -> None:
        self.config = _FakeCfg(
            name=name,
            description=f"peer:{name}",
            display_name=display_name,
            tools=["talk_to_agent"],
        )
        self.invocations: list[tuple[str, str | None]] = []
        self._reply = reply if reply is not None else f"OK from {name}"
        self._delay = delay

    async def ainvoke(self, message: str, thread_id: str | None = None) -> str:
        self.invocations.append((message, thread_id))
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._reply


class _FakeRuntime:
    def __init__(self, runners: dict[str, _FakeRunner]) -> None:
        self._runners = runners


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Wipe all module-level dedup/pair-map state between tests."""
    tt._RUNTIME = None
    tt._PUBLISHER = None
    tt._pair_links.clear()
    tt._inflight.clear()
    tt._completed.clear()
    # Reset current_agent contextvar to default — tests that need a specific
    # caller set it explicitly.
    tt.current_agent.set("")
    yield
    tt._RUNTIME = None
    tt._PUBLISHER = None
    tt._pair_links.clear()
    tt._inflight.clear()
    tt._completed.clear()


@pytest.fixture
def two_peers():
    runners = {
        "kevin": _FakeRunner("kevin"),
        "academic-anthropologist": _FakeRunner("academic-anthropologist"),
    }
    runtime = _FakeRuntime(runners)
    events: list[tuple[str, dict]] = []
    tt.bind_runtime(runtime, publisher=lambda et, payload: events.append((et, payload)))
    return runtime, events


# ── Name resolution ─────────────────────────────────────────────────────────


def test_resolve_peer_name_exact_match(two_peers):
    canonical, key = tt._resolve_peer_name("kevin")
    assert canonical == "kevin" and key == "kevin"


@pytest.mark.parametrize("typed", ["Kevin", "KEVIN", "kEvIn"])
def test_resolve_peer_name_is_case_insensitive(two_peers, typed):
    canonical, _ = tt._resolve_peer_name(typed)
    assert canonical == "kevin", f"{typed!r} should fold to 'kevin'"


def test_resolve_peer_name_unknown_returns_none(two_peers):
    assert tt._resolve_peer_name("zorbax") == (None, None)


def test_resolve_peer_name_accepts_a_display_name():
    """The roster shows the display name, so a model will sometimes send it."""
    tt.bind_runtime(
        _FakeRuntime({"kevin": _FakeRunner("kevin", display_name="Kevin the Ops Lead")})
    )
    assert tt._resolve_peer_name("Kevin the Ops Lead") == ("kevin", "kevin")
    assert tt._resolve_peer_name("kevin the ops lead") == ("kevin", "kevin")


def test_a_handle_always_beats_another_agents_display_name():
    """Load-bearing ordering: if one agent's display name could shadow another
    agent's actual handle, a message silently reaches the wrong peer."""
    tt.bind_runtime(
        _FakeRuntime(
            {
                "kevin": _FakeRunner("kevin"),
                "dana": _FakeRunner("dana", display_name="kevin"),
            }
        )
    )
    assert tt._resolve_peer_name("kevin") == ("kevin", "kevin")


def test_list_peers_info_carries_both_names():
    tt.bind_runtime(
        _FakeRuntime({"kevin": _FakeRunner("kevin", display_name="Kevin the Ops Lead")})
    )
    peer = tt.list_peers_info()[0]
    # The roster renders the label but must keep the handle: it is what
    # talk_to_agent(agent_name=...) takes.
    assert peer["name"] == "kevin"
    assert peer["label"] == "Kevin the Ops Lead"


def test_list_peers_info_label_falls_back_to_the_handle():
    tt.bind_runtime(_FakeRuntime({"kevin": _FakeRunner("kevin")}))
    assert tt.list_peers_info()[0]["label"] == "kevin"


def test_unknown_peer_error_includes_near_miss_suggestion(two_peers):
    error = tt._format_unknown_peer_error("kevin-bot")
    # Surfaces the closest match prominently — model sees a concrete retry.
    assert "Retry with `kevin`." in error


def test_unknown_peer_error_omits_suggestion_when_no_close_match(two_peers):
    error = tt._format_unknown_peer_error("totally-unrelated-string")
    assert "Retry with" not in error and "Did you mean" not in error


# ── Routing: sync ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_call_isolates_on_per_pair_thread(two_peers):
    """Caller mid-turn on a user thread: callee runs on a per-pair acp::
    thread keyed on BOTH parties, NOT on T and NOT on a callee-only thread."""
    runtime, _ = two_peers
    tt.current_agent.set("academic-anthropologist")
    USER_THREAD = "user-thread-T"
    with push_request_context(source="api", session_id=USER_THREAD):
        reply = await tt.talk_to_agent.ainvoke(
            {
                "agent_name": "kevin",
                "message": "fetch BTC",
            }
        )
    # Tool returns ONLY the peer's reply — the "Sent → Replied" presentation
    # format lives in the prompt, not in the tool return value (single source
    # of truth, no double-echo).
    assert reply == "OK from kevin"
    assert runtime._runners["kevin"].invocations == [
        ("fetch BTC", "acp::academic-anthropologist::kevin")
    ]


@pytest.mark.asyncio
async def test_sync_call_remembers_pair_for_future_callback(two_peers):
    """Even though sync uses acp::, the pair is recorded so future async
    callbacks land on the user thread."""
    tt.current_agent.set("academic-anthropologist")
    USER_THREAD = "user-thread-T"
    with push_request_context(source="api", session_id=USER_THREAD):
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "fetch BTC"})
    # Pair stored as (kevin → academic-anthropologist) so when kevin later
    # calls academic-anthropologist, the callback routes to USER_THREAD.
    assert tt._recall_pair_thread("kevin", "academic-anthropologist") == USER_THREAD


@pytest.mark.asyncio
async def test_sync_call_does_not_propagate_originator_metadata(two_peers):
    """Sync mode deliberately drops the user-thread anchor so a nested
    talk_to_from the callee doesn't accidentally re-route onto the
    caller's thread (would defeat the isolation we just chose)."""
    runtime, _ = two_peers
    # Stub kevin so it captures the request-context metadata it sees.
    captured: dict = {}

    async def _capturing_ainvoke(message: str, thread_id: str | None = None):
        from olano.security.request_context import get_request_context

        captured["metadata"] = dict(get_request_context().metadata or {})
        return "ok"

    runtime._runners["kevin"].ainvoke = _capturing_ainvoke  # type: ignore[method-assign]
    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id="user-thread-T"):
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "hi"})
    assert "originator_user_thread" not in captured["metadata"]


# ── Routing: async callback ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_callback_routes_via_pair_map(two_peers):
    runtime, events = two_peers
    USER_THREAD = "user-thread-T"

    # Step 1: sync setup — anthropologist on T calls kevin, pair remembered.
    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id=USER_THREAD):
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "monitor BTC"})

    # Step 2: kevin (from scheduled run) calls anthropologist. No user
    # context on kevin's request — the pair-map is the only signal.
    tt.current_agent.set("kevin")
    with push_request_context(source="scheduled", session_id="scheduled_kevin_xyz"):
        await tt.talk_to_agent.ainvoke(
            {
                "agent_name": "academic-anthropologist",
                "message": "BTC dropped 6%",
            }
        )

    # Anthropologist should have been invoked on the original user thread,
    # not on acp::academic-anthropologist.
    anthr_invocations = runtime._runners["academic-anthropologist"].invocations
    assert anthr_invocations == [("BTC dropped 6%", USER_THREAD)]


@pytest.mark.asyncio
async def test_async_callback_emits_thread_message_events(two_peers):
    _, events = two_peers
    USER_THREAD = "user-thread-T"
    # Prime the pair-map as if anthropologist had earlier called kevin on T,
    # which is exactly when this route is registered in production.
    tt._remember_pair("academic-anthropologist", "kevin", USER_THREAD)
    tt.current_agent.set("kevin")
    with push_request_context(source="scheduled", session_id="scheduled_kevin_xyz"):
        await tt.talk_to_agent.ainvoke(
            {
                "agent_name": "academic-anthropologist",
                "message": "BTC dropped 6%",
            }
        )

    # Exactly two thread_message events: peer_inbound + assistant, both keyed
    # to anthropologist::USER_THREAD so the dashboard chat panel can render
    # them live.
    thread_msgs = [p for et, p in events if et == "thread_message"]
    assert len(thread_msgs) == 2
    assert {m["role"] for m in thread_msgs} == {"peer_inbound", "assistant"}
    for m in thread_msgs:
        assert m["agent"] == "academic-anthropologist"
        assert m["thread_id"] == USER_THREAD


@pytest.mark.asyncio
async def test_sync_call_emits_target_thread_message(two_peers):
    """Sync calls are visible in the caller stream and also mirrored into the
    callee's acp thread so opening Kevin's chat shows the peer request as a
    peer-inbound message instead of losing the other side of the exchange."""
    _, events = two_peers
    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id="user-thread-T"):
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "hi"})
    thread_msgs = [p for et, p in events if et == "thread_message"]
    assert len(thread_msgs) == 2
    assert {m["role"] for m in thread_msgs} == {"peer_inbound", "assistant"}
    for m in thread_msgs:
        assert m["agent"] == "kevin"
        assert m["thread_id"] == "acp::academic-anthropologist::kevin"


# ── Routing: cold ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cold_call_falls_back_to_acp(two_peers):
    """No user-thread context anywhere → acp:: thread, no thread_message."""
    runtime, events = two_peers
    tt.current_agent.set("kevin")
    with push_request_context(source="scheduled", session_id="scheduled_kevin_xyz"):
        await tt.talk_to_agent.ainvoke(
            {
                "agent_name": "academic-anthropologist",
                "message": "cold ping",
            }
        )
    assert runtime._runners["academic-anthropologist"].invocations == [
        ("cold ping", "acp::kevin::academic-anthropologist")
    ]
    assert not any(et == "thread_message" for et, _ in events)


# ── Per-pair thread isolation ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_callers_get_isolated_threads(two_peers):
    """Different callers talking to the SAME peer must land on distinct
    per-pair threads — this is the whole point: concurrent conversations no
    longer collapse onto one shared acp::<callee> thread and contaminate."""
    runtime, _ = two_peers

    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id="user-thread-A"):
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "from anthropologist"})

    tt.current_agent.set("some-other-caller")
    with push_request_context(source="api", session_id="user-thread-B"):
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "from someone else"})

    threads = [tid for _msg, tid in runtime._runners["kevin"].invocations]
    assert threads == [
        "acp::academic-anthropologist::kevin",
        "acp::some-other-caller::kevin",
    ]
    assert len(set(threads)) == 2, "the two callers must not share a thread"


@pytest.mark.asyncio
async def test_pair_thread_id_is_case_stable(two_peers):
    """`Kevin` and `kevin` as the caller must fold to one thread so casing
    drift never forks a conversation into two checkpoints."""
    runtime, _ = two_peers

    tt.current_agent.set("Academic-Anthropologist")
    with push_request_context(source="api", session_id="user-thread-A"):
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "caps caller"})

    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id="user-thread-A"):
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "lower caller"})

    threads = {tid for _msg, tid in runtime._runners["kevin"].invocations}
    assert threads == {"acp::academic-anthropologist::kevin"}


def test_pair_thread_id_helper_is_a_system_thread():
    """Regression: the new per-pair id keeps the acp:: prefix so it's still
    classified as a system thread (never a routing target for callbacks)."""
    tid = tt._pair_thread_id("Aurelius", "kevin")
    assert tid == "acp::aurelius::kevin"
    assert tt._is_user_thread(tid) is False


# ── Tool return format ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_returns_peer_reply_unmodified(two_peers):
    """Contract: the tool returns ONLY the peer's reply text. Presentation
    (the "Sent → Replied" user-facing format) belongs in the prompt, not
    the tool return — wrapping in the return causes small models to
    double-echo (verbatim copy + wrapped copy)."""
    runtime, _ = two_peers
    runtime._runners["kevin"]._reply = "absolutely, on it"
    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id="user-thread-T"):
        out = await tt.talk_to_agent.ainvoke(
            {
                "agent_name": "kevin",
                "message": "Please check XYZ",
            }
        )
    assert out == "absolutely, on it"


@pytest.mark.asyncio
async def test_callee_runs_with_current_agent_context(two_peers):
    """Nested tools inside the callee must attribute themselves to the callee,
    not to the outer caller that happened to invoke talk_to_agent."""
    runtime, _ = two_peers
    captured: dict = {}

    async def _capturing_ainvoke(message: str, thread_id: str | None = None):
        captured["current_agent"] = tt.current_agent.get()
        return "ok"

    runtime._runners["kevin"].ainvoke = _capturing_ainvoke  # type: ignore[method-assign]
    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id="user-thread-T"):
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "hi"})

    assert captured["current_agent"] == "kevin"
    assert tt.current_agent.get() == "academic-anthropologist"


@pytest.mark.asyncio
async def test_programmatic_a2a_peer_to_subagent_round_trip():
    """Dashboard-free regression for:
    user -> aurelius -> kevin -> MarketAgent -> kevin -> aurelius -> user.

    The key contract is attribution: Kevin's subagent delegation must be
    Kevin -> MarketAgent, never Aurelius -> MarketAgent.
    """
    events: list[tuple[str, dict]] = []

    class _KevinRunner(_FakeRunner):
        async def ainvoke(self, message: str, thread_id: str | None = None) -> str:
            self.invocations.append((message, thread_id))
            assert tt.current_agent.get() == "kevin"
            sender = tt.current_agent.get()
            started = "2026-06-02T01:12:00+00:00"
            finished = "2026-06-02T01:12:07+00:00"
            subagent_request = (
                "Fetch the current Bitcoin (BTC) price today. Return a concise update "
                "with BTC-USD price, 24h % change if available, source, and timestamp/time if available."
            )
            subagent_reply = (
                "BTC-USD: $71,487\n"
                "24h change: -2.60%\n"
                "Source: CoinGecko\n"
                "Timestamp: Current live quote; exact timestamp not provided"
            )
            tt._PUBLISHER(
                "agent_message",
                {
                    "kind": "subagent",
                    "phase": "send",
                    "from": sender,
                    "to": "MarketAgent",
                    "message": subagent_request,
                    "ts": started,
                    "tool_call_id": "task-market-1",
                },
            )
            tt._PUBLISHER(
                "agent_message",
                {
                    "kind": "subagent",
                    "phase": "reply",
                    "from": sender,
                    "to": "MarketAgent",
                    "message": subagent_request,
                    "reply": subagent_reply,
                    "ts_sent": started,
                    "ts_reply": finished,
                    "tool_call_id": "task-market-1",
                },
            )
            return subagent_reply

    runtime = _FakeRuntime(
        {
            "aurelius": _FakeRunner("aurelius"),
            "kevin": _KevinRunner("kevin"),
        }
    )
    tt.bind_runtime(runtime, publisher=lambda et, payload: events.append((et, payload)))
    tt.current_agent.set("aurelius")

    with push_request_context(source="api", session_id="user-thread-T"):
        reply = await tt.talk_to_agent.ainvoke(
            {
                "agent_name": "Kevin",
                "message": (
                    "Carlos asked: what is the price of BTC today? Please check the current "
                    "Bitcoin price and return a concise update with source/time if available."
                ),
            }
        )

    assert "BTC-USD: $71,487" in reply
    assert runtime._runners["kevin"].invocations == [
        (
            "Carlos asked: what is the price of BTC today? Please check the current "
            "Bitcoin price and return a concise update with source/time if available.",
            "acp::aurelius::kevin",
        )
    ]

    agent_messages = [payload for event_type, payload in events if event_type == "agent_message"]
    assert [
        (event["kind"], event["phase"], event["from"], event["to"]) for event in agent_messages
    ] == [
        ("peer", "send", "aurelius", "kevin"),
        ("subagent", "send", "kevin", "MarketAgent"),
        ("subagent", "reply", "kevin", "MarketAgent"),
        ("peer", "reply", "aurelius", "kevin"),
    ]
    assert not any(
        event["from"] == "aurelius" and event["to"] == "MarketAgent" for event in agent_messages
    )

    thread_messages = [payload for event_type, payload in events if event_type == "thread_message"]
    assert [(event["role"], event["agent"], event["thread_id"]) for event in thread_messages] == [
        ("peer_inbound", "kevin", "acp::aurelius::kevin"),
        ("assistant", "kevin", "acp::aurelius::kevin"),
    ]


def test_docstring_tells_model_dashboard_renders_natively():
    """Contract: the docstring tells the model the dashboard handles the
    structured display, so the model should NOT echo the peer's reply.
    This is the anti-duplication contract — if it drifts, the chat panel
    starts double-rendering Kevin's reply (once natively, once in text)."""
    description = tt.talk_to_agent.description or ""
    assert "dashboard renders this tool call" in description
    assert "Do NOT paste the peer's reply" in description
    assert "Do NOT lead your response with the peer's reply" in description


def test_roster_block_tells_model_dashboard_renders_natively():
    """Same anti-duplication contract mirrored at the system-prompt level
    so small models that under-weight tool descriptions still see it."""
    import tempfile
    from pathlib import Path

    from olano.config.agent_config import (
        AgentConfig,
        BudgetConfig,
        MemoryConfig,
        PulseConfig,
        SecurityConfig,
        WalletConfig,
    )
    from olano.config.workspace import Workspace
    from olano.core.prompt_assembler import PromptAssembler

    tmp = Path(tempfile.mkdtemp())
    ws = Workspace(root=tmp)
    cfg = AgentConfig(
        name="caller",
        model="anthropic:claude-sonnet-4-6",
        system_prompt="test",
        security=SecurityConfig(),
        budget=BudgetConfig(),
        pulse=PulseConfig(),
        wallet=WalletConfig(),
        memory=MemoryConfig(),
        # The peer must be *visible* for the talk_to_agent block to render;
        # visibility defaults to "none" (safe), so opt this test into "all".
        agent_visibility={"mode": "all"},
    )

    class _Stub:
        def __init__(self, name):
            self.config = type(
                "C",
                (),
                {
                    "name": name,
                    "description": "stub",
                    "emoji": "🦁",
                    "tools": ["talk_to_agent"],
                    "hidden": False,
                },
            )

    tt.bind_runtime(type("R", (), {"_runners": {"kevin": _Stub("kevin")}})())

    block = PromptAssembler(cfg, ws)._build_agent_roster_block()
    assert "dashboard renders the" in block
    assert "brief and additive" in block
    assert "never a copy of the reply" in block


# ── Prompt-roster visibility (agent_visibility) ──────────────────────────────


def _roster_for(caller_cfg, peers):
    """Bind a fake runtime exposing ``peers`` and return ``caller``'s roster.

    ``peers`` is a list of ``_FakeCfg`` (each becomes a booted runner). The
    caller's own config drives the visibility / org partitioning.
    """
    import tempfile
    from pathlib import Path

    from olano.config.workspace import Workspace
    from olano.core.prompt_assembler import PromptAssembler

    runners = {c.name: type("R", (), {"config": c})() for c in peers}
    tt.bind_runtime(type("R", (), {"_runners": runners})())
    ws = Workspace(root=Path(tempfile.mkdtemp()))
    return PromptAssembler(caller_cfg, ws)._build_agent_roster_block()


def _caller(**kwargs):
    from olano.config.agent_config import AgentConfig

    return AgentConfig(name="caller", model="anthropic:claude-sonnet-4-6", **kwargs)


def test_visibility_none_hides_unrelated_peers():
    """Default visibility ('none') keeps unrelated peers out of the roster —
    the agent doesn't even know they exist."""
    cfg = _caller()  # default agent_visibility.mode == "none"
    block = _roster_for(cfg, [_FakeCfg(name="kevin", description="peer:kevin")])
    assert "kevin" not in block


def test_visibility_all_lists_every_non_hidden_peer():
    cfg = _caller(agent_visibility={"mode": "all"})
    block = _roster_for(
        cfg,
        [
            _FakeCfg(name="kevin", description="a peer"),
            _FakeCfg(name="dana", description="another"),
        ],
    )
    assert "kevin" in block and "dana" in block


def test_visibility_hidden_agents_never_listed_even_in_all_mode():
    """Internal/test agents (hidden: true) must never leak into the roster."""
    cfg = _caller(agent_visibility={"mode": "all"})
    block = _roster_for(
        cfg,
        [
            _FakeCfg(name="kevin", description="real peer"),
            _FakeCfg(name="test-agent-openai", description="internal", hidden=True),
        ],
    )
    assert "kevin" in block
    assert "test-agent-openai" not in block


def test_visibility_list_only_named_peers():
    cfg = _caller(agent_visibility={"mode": "list", "agents": ["dana"]})
    block = _roster_for(
        cfg,
        [
            _FakeCfg(name="zoltan", description="UNIQUE_EXCLUDED_DESC"),
            _FakeCfg(name="dana", description="UNIQUE_LISTED_DESC"),
        ],
    )
    # `dana` is on the visibility list; `zoltan` is not — assert on the peers'
    # unique descriptions (the section boilerplate itself name-drops examples).
    assert "UNIQUE_LISTED_DESC" in block
    assert "UNIQUE_EXCLUDED_DESC" not in block
    assert "zoltan" not in block


def test_reachable_peers_injected_despite_none_visibility():
    """Manager / reports / talk_to allow-list are auto-injected even when
    visibility is 'none' — declaring a relationship makes the peer known."""
    cfg = _caller(org={"manager": "marcus", "output_mode": "list", "output_agents": ["emma"]})
    block = _roster_for(
        cfg,
        [
            _FakeCfg(name="marcus", description="the manager"),
            _FakeCfg(name="emma", description="talk-to target"),
            _FakeCfg(name="stranger", description="unrelated"),
        ],
    )
    assert "marcus" in block and "emma" in block
    assert "stranger" not in block
    # marcus is the reporting manager — tagged as such.
    assert "[your manager]" in block


def test_no_talk_tool_downgrades_peers_to_aware_only():
    """Removing talk_to_agent (disabled_tools) means visible peers appear as
    'aware only' with an explicit can't-message note, never as talk_to targets."""
    cfg = _caller(agent_visibility={"mode": "all"}, disabled_tools=["talk_to_agent"])
    assert "talk_to_agent" not in cfg.tools  # honored by _inject_builtin_ops
    block = _roster_for(cfg, [_FakeCfg(name="kevin", description="a peer")])
    assert "kevin" in block
    assert "cannot" in block.lower() and "your own `talk_to_agent` tool is disabled" in block
    # It must NOT advertise the talk_to_agent call form for these peers.
    assert "call them via `talk_to_agent`" not in block


@pytest.mark.asyncio
async def test_sse_publishers_receive_peer_reply_unmodified(two_peers):
    """SSE consumers (A2A panel, chat panel) and the tool return value
    share one source of truth — the peer's actual reply text."""
    _, events = two_peers
    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id="user-thread-T"):
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "hi"})

    reply_events = [p for et, p in events if et == "agent_message" and p["phase"] == "reply"]
    assert len(reply_events) == 1
    assert reply_events[0]["reply"] == "OK from kevin"


# ── Idempotency ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cooldown_returns_cached_reply_for_duplicate(two_peers):
    runtime, _ = two_peers
    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id="user-thread-T"):
        first = await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "list orders"})
        second = await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "list orders"})

    # Peer was invoked exactly once; the second call returned from cache.
    assert len(runtime._runners["kevin"].invocations) == 1
    assert "OK from kevin" in second
    assert second.startswith("[talk_to_agent note]")


@pytest.mark.asyncio
async def test_cooldown_does_not_collide_across_different_messages(two_peers):
    runtime, _ = two_peers
    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id="user-thread-T"):
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "list orders"})
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "place TSLA order"})
    # Different messages → different keys → both reach the peer.
    assert len(runtime._runners["kevin"].invocations) == 2


@pytest.mark.asyncio
async def test_inflight_dedup_collapses_concurrent_duplicates(two_peers):
    runtime, _ = two_peers
    # Make kevin slow so two concurrent calls overlap.
    runtime._runners["kevin"]._delay = 0.1
    tt.current_agent.set("academic-anthropologist")

    async def _one():
        with push_request_context(source="api", session_id="user-thread-T"):
            return await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "hi"})

    results = await asyncio.gather(_one(), _one(), _one())
    # All three callers got the same reply, but kevin was only invoked once.
    assert len(runtime._runners["kevin"].invocations) == 1
    assert all(r == "OK from kevin" for r in results)


@pytest.mark.asyncio
async def test_cooldown_does_not_cache_errors(two_peers):
    runtime, _ = two_peers
    # First call fails; second should still reach the peer.
    boom_count = {"n": 0}

    async def _flaky(message, thread_id=None):
        boom_count["n"] += 1
        if boom_count["n"] == 1:
            raise RuntimeError("kaboom")
        return "second time worked"

    runtime._runners["kevin"].ainvoke = _flaky  # type: ignore[method-assign]
    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id="user-thread-T"):
        first = await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "hi"})
        second = await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "hi"})
    assert "kaboom" in first
    assert second == "second time worked"
    assert boom_count["n"] == 2


# ── Context propagation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_callback_propagates_originator_user_thread(two_peers):
    """Nested talk_to_agent calls inside an async chain should stay on the
    same user thread so the whole conversation lands in one place."""
    runtime, _ = two_peers
    USER_THREAD = "user-thread-T"
    tt._remember_pair("academic-anthropologist", "kevin", USER_THREAD)

    # Capture metadata the anthropologist sees when invoked.
    captured: dict = {}

    async def _capturing(message, thread_id=None):
        from olano.security.request_context import get_request_context

        captured["metadata"] = dict(get_request_context().metadata or {})
        captured["thread_id"] = thread_id
        return "ack"

    runtime._runners["academic-anthropologist"].ainvoke = _capturing  # type: ignore[method-assign]
    tt.current_agent.set("kevin")
    with push_request_context(source="scheduled", session_id="scheduled_kevin"):
        await tt.talk_to_agent.ainvoke(
            {
                "agent_name": "academic-anthropologist",
                "message": "callback payload",
            }
        )

    assert captured["thread_id"] == USER_THREAD
    assert captured["metadata"].get("originator_user_thread") == USER_THREAD


# ── Per-pair conversation digest (opt-in) ──────────────────────────────────


@pytest.fixture
def digest_on(monkeypatch):
    """Enable the opt-in per-pair digest and back it with an in-memory store."""
    from langgraph.store.memory import InMemoryStore

    store = InMemoryStore()

    async def _fake_get_async_store(home=None):
        return store

    monkeypatch.setattr("olano.core.stores.get_async_store", _fake_get_async_store)
    monkeypatch.setenv("LIONCLAW_A2A_DIGEST", "1")
    return store


@pytest.mark.asyncio
async def test_digest_disabled_by_default(two_peers):
    """With the flag unset (default) the callee sees no a2a_preamble and the
    store is never touched — behaviour is exactly Layer-1-only."""
    runtime, _ = two_peers
    captured: dict = {}

    async def _capturing(message, thread_id=None):
        from olano.security.request_context import get_request_context

        captured["metadata"] = dict(get_request_context().metadata or {})
        return "ok"

    runtime._runners["kevin"].ainvoke = _capturing  # type: ignore[method-assign]
    assert tt._a2a_digest_enabled() is False
    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id="user-thread-T"):
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "hi"})
    assert "a2a_preamble" not in captured["metadata"]


@pytest.mark.asyncio
async def test_pair_digest_persists_and_reloads(two_peers, digest_on):
    """Two exchanges are recorded in the shared canonical-pair digest and can
    be reloaded from either direction."""
    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id="user-thread-T"):
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "first"})
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "second"})

    record = await tt._load_pair_digest("kevin", "academic-anthropologist")
    recent = record["recent"]
    assert [e["message"] for e in recent] == ["first", "second"]
    assert all(e["reply"] == "OK from kevin" for e in recent)


@pytest.mark.asyncio
async def test_digest_summary_folds_on_overflow(two_peers, digest_on):
    """More than the short-term window folds the overflow into the rolling
    long-term summary and caps `recent`."""
    tt.current_agent.set("academic-anthropologist")
    n = tt._A2A_DIGEST_RECENT + 3
    with push_request_context(source="api", session_id="user-thread-T"):
        for i in range(n):
            await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": f"msg-{i}"})

    record = await tt._load_pair_digest("academic-anthropologist", "kevin")
    assert len(record["recent"]) == tt._A2A_DIGEST_RECENT
    assert record["summary"].strip(), "overflow should have populated the summary"
    # The newest messages are retained verbatim; the oldest rolled into summary.
    assert record["recent"][-1]["message"] == f"msg-{n - 1}"
    assert record["recent"][0]["message"] == f"msg-{n - tt._A2A_DIGEST_RECENT}"


@pytest.mark.asyncio
async def test_preamble_injected_via_metadata_after_prior_exchange(two_peers, digest_on):
    """Once a pair has history, the next call injects it as an a2a_preamble on
    the callee's request-context metadata (Layer-2 push into the callee)."""
    runtime, _ = two_peers
    captured: dict = {}

    real_ainvoke = runtime._runners["kevin"].ainvoke

    async def _capturing(message, thread_id=None):
        from olano.security.request_context import get_request_context

        captured["metadata"] = dict(get_request_context().metadata or {})
        return await real_ainvoke(message, thread_id)

    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id="user-thread-T"):
        # First exchange records the digest (no preamble yet — nothing prior).
        await tt.talk_to_agent.ainvoke(
            {"agent_name": "kevin", "message": "remember the alpha report"}
        )
        runtime._runners["kevin"].ainvoke = _capturing  # type: ignore[method-assign]
        # Second exchange should carry the prior context forward.
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "any update?"})

    preamble = captured["metadata"].get("a2a_preamble", "")
    assert "alpha report" in preamble
    assert "academic-anthropologist" in preamble


# ── wait_n_seconds ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_n_seconds_is_async_friendly():
    """The whole reason this tool exists is the non-blocking semantics —
    two concurrent calls should finish in ~max(t1, t2), not t1+t2."""
    import time as _time

    from olano.tools.time_tools import wait_n_seconds

    t0 = _time.monotonic()
    await asyncio.gather(
        wait_n_seconds.ainvoke({"seconds": 1, "reason": "a"}),
        wait_n_seconds.ainvoke({"seconds": 1, "reason": "b"}),
    )
    elapsed = _time.monotonic() - t0
    assert elapsed < 1.5, f"wait_n_seconds is serialising — {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_wait_n_seconds_clamps_max():
    """Anything > 300s should clamp, not error — the model gets a clear
    confirmation in the return string."""
    # We don't actually wait 300s in CI; we patch sleep to a noop.
    from olano.tools.time_tools import _MAX_WAIT_SECONDS, wait_n_seconds

    original_sleep = asyncio.sleep
    seen: list[int] = []

    async def _fast_sleep(n: float) -> None:
        seen.append(int(n))

    asyncio.sleep = _fast_sleep  # type: ignore[assignment]
    try:
        out = await wait_n_seconds.ainvoke({"seconds": 9999, "reason": "huge"})
    finally:
        asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert seen == [_MAX_WAIT_SECONDS]
    assert f"Waited {_MAX_WAIT_SECONDS}s" in out
    assert "clamped from 9999" in out


@pytest.mark.asyncio
async def test_wait_n_seconds_clamps_min():
    """Zero or negative becomes 1 — also surfaces in the return string."""
    from olano.tools.time_tools import wait_n_seconds

    seen: list[int] = []
    original_sleep = asyncio.sleep

    async def _fast_sleep(n: float) -> None:
        seen.append(int(n))

    asyncio.sleep = _fast_sleep  # type: ignore[assignment]
    try:
        out = await wait_n_seconds.ainvoke({"seconds": 0, "reason": "boundary"})
    finally:
        asyncio.sleep = original_sleep  # type: ignore[assignment]
    assert seen == [1]
    assert "clamped from 0" in out


# ── Pair-map TTL ──────────────────────────────────────────────────────────


def test_pair_link_expires_after_ttl(two_peers, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(tt.time, "time", lambda: now[0])

    tt._remember_pair("academic-anthropologist", "kevin", "user-thread-T")
    assert tt._recall_pair_thread("kevin", "academic-anthropologist") == "user-thread-T"

    # Jump past the TTL.
    now[0] += tt._PAIR_LINK_TTL_SECONDS + 1
    assert tt._recall_pair_thread("kevin", "academic-anthropologist") is None


def test_pair_link_ignored_for_system_threads(two_peers):
    """We should never remember acp:: / scheduled_ / heartbeat_ as routing
    targets — those are exactly the threads we route AWAY from."""
    for bad in ("acp::kevin", "scheduled_kevin_xyz", "heartbeat_kevin", "delegate::sub"):
        tt._remember_pair("academic-anthropologist", "kevin", bad)
        assert tt._recall_pair_thread("kevin", "academic-anthropologist") is None


# ── Peer thread rotation ──────────────────────────────────────────────────────


class _PathRunner:
    """A runner double that only knows where its transcripts live."""

    def __init__(self, directory):
        self.directory = directory

    def _session_path(self, thread_id: str):
        return self.directory / f"{thread_id}.jsonl"


def _write_rows(path, rows: int, *, first_ts: str = "2026-10-21T10:00:00+00:00") -> None:
    import json

    with path.open("w", encoding="utf-8") as fh:
        for i in range(rows):
            fh.write(json.dumps({"ts": first_ts, "role": "user", "content": f"r{i}"}) + "\n")


def _peer_bounds(**updates) -> None:
    from olano.config.system_settings import set_peer_thread_settings

    tt._PEER_THREAD_STATS.clear()
    set_peer_thread_settings(updates)


def test_peer_thread_without_a_transcript_dir_is_the_bare_pair_id():
    assert tt._peer_thread_id("Alice", "bob", object()) == "acp::alice::bob"


def test_peer_thread_rolls_over_once_the_transcript_is_long(tmp_path):
    _peer_bounds(max_turns=12)
    runner = _PathRunner(tmp_path)
    now = 1_792_577_600.0  # 2026-10-21T10:13:20Z
    base = "acp::alice::bob"

    # No transcript yet: the bare pair id, as always.
    assert tt._peer_thread_id("alice", "bob", runner, now=now) == base
    # Eleven rows: still under the bound.
    _write_rows(tmp_path / f"{base}.jsonl", 11)
    assert tt._peer_thread_id("alice", "bob", runner, now=now) == base
    # Twelve rows: the next exchange opens a stamped generation...
    _write_rows(tmp_path / f"{base}.jsonl", 12)
    gen1 = tt._peer_thread_id("alice", "bob", runner, now=now)
    assert gen1 == f"{base}::20261021T101320"
    # ...which is reused until it is exhausted in turn, whatever the base holds.
    _write_rows(tmp_path / f"{gen1}.jsonl", 2)
    assert tt._peer_thread_id("alice", "bob", runner, now=now + 60) == gen1
    _write_rows(tmp_path / f"{gen1}.jsonl", 12)
    gen2 = tt._peer_thread_id("alice", "bob", runner, now=now + 60)
    assert gen2 == f"{base}::20261021T101420"
    assert gen2 > gen1  # generations sort by name, so the newest is found by sorting


def test_peer_thread_rolls_over_once_the_transcript_is_old(tmp_path):
    _peer_bounds(max_age_days=7)
    runner = _PathRunner(tmp_path)
    base = "acp::alice::bob"
    _write_rows(tmp_path / f"{base}.jsonl", 2, first_ts="2026-10-01T00:00:00+00:00")
    fresh = datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp()
    stale = datetime(2026, 10, 21, tzinfo=timezone.utc).timestamp()
    assert tt._peer_thread_id("alice", "bob", runner, now=fresh) == base
    assert tt._peer_thread_id("alice", "bob", runner, now=stale) == f"{base}::20261021T000000"


def test_peer_thread_default_bound_is_three_days(tmp_path):
    from olano.config.system_settings import load_peer_thread_settings

    assert load_peer_thread_settings().max_age_days == 3
    assert load_peer_thread_settings().max_turns == 200
    tt._PEER_THREAD_STATS.clear()
    runner = _PathRunner(tmp_path)
    base = "acp::alice::bob"
    _write_rows(tmp_path / f"{base}.jsonl", 2, first_ts="2026-10-18T00:00:00+00:00")
    two_days = datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp()
    four_days = datetime(2026, 10, 22, tzinfo=timezone.utc).timestamp()
    assert tt._peer_thread_id("alice", "bob", runner, now=two_days) == base
    assert tt._peer_thread_id("alice", "bob", runner, now=four_days) != base


def test_peer_thread_rotation_can_be_switched_off(tmp_path):
    _peer_bounds(rotate=False, max_turns=10)
    runner = _PathRunner(tmp_path)
    base = "acp::alice::bob"
    _write_rows(tmp_path / f"{base}.jsonl", 500, first_ts="2025-01-01T00:00:00+00:00")
    assert tt._peer_thread_id("alice", "bob", runner, now=1_792_577_600.0) == base
    # Switching it back on applies to the next call: the setting is read live.
    _peer_bounds(rotate=True, max_turns=10)
    assert tt._peer_thread_id("alice", "bob", runner, now=1_792_577_600.0) != base


def test_peer_thread_does_not_confuse_a_pair_with_a_longer_callee_name(tmp_path):
    _peer_bounds(max_turns=10)
    runner = _PathRunner(tmp_path)
    _write_rows(tmp_path / "acp::alice::bobby::20260101T000000.jsonl", 1)
    _write_rows(tmp_path / "acp::alice::bob.jsonl", 1)
    assert tt._peer_thread_id("alice", "bob", runner, now=1e9) == "acp::alice::bob"


@pytest.mark.asyncio
async def test_sync_call_runs_on_the_current_generation(two_peers, tmp_path):
    """The tool itself asks for the generation: a long transcript on the
    callee's side moves the next exchange onto a fresh thread."""
    _peer_bounds(max_turns=10)
    runtime, _ = two_peers
    runtime._runners["kevin"]._session_path = _PathRunner(tmp_path)._session_path
    _write_rows(tmp_path / "acp::academic-anthropologist::kevin.jsonl", 10)
    tt.current_agent.set("academic-anthropologist")
    with push_request_context(source="api", session_id="user-thread-T"):
        await tt.talk_to_agent.ainvoke({"agent_name": "kevin", "message": "fetch BTC"})
    ((_msg, thread),) = runtime._runners["kevin"].invocations
    assert thread.startswith("acp::academic-anthropologist::kevin::2")
