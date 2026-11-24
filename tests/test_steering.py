"""Mid-turn steering: the registry (core/steering.py) and the graph hook
(core/steer_middleware.py) that hands a running turn a message at its next
model call. The runner/API halves are covered in test_runner.py and
test_runtime_api.py."""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from olano.core.steer_middleware import SteerMiddleware, build_steer_middleware
from olano.core.steering import (
    MAX_PER_TURN,
    MAX_STEER_CHARS,
    REGISTRY,
    STEER_HEADER,
    SteerRegistry,
    steer_message_content,
)
from olano.security.request_context import push_request_context


@pytest.fixture(autouse=True)
def _clean_registry():
    REGISTRY.reset()
    yield
    REGISTRY.reset()


class TestRegistry:
    def test_steer_without_a_running_turn_is_refused(self) -> None:
        reg = SteerRegistry()
        assert reg.steer("bot", "t1", "use blue") is None
        assert reg.active_turn("bot", "t1") is None

    def test_lifecycle_pending_delivered_reported(self) -> None:
        reg = SteerRegistry()
        handle = reg.begin_turn("bot", "t1", "turn-a")
        assert reg.active_turn("bot", "t1") == "turn-a"
        accepted = reg.steer(
            "bot", "t1", "  use blue  ", steer_id="s-1", sender="Ana", channel="dashboard"
        )
        assert accepted is not None
        assert accepted.id == "s-1"
        assert accepted.text == "use blue"  # stripped
        assert accepted.turn_id == "turn-a"
        assert accepted.sender == "Ana"
        assert accepted.channel == "dashboard"
        assert not accepted.delivered
        # Nothing to report until the graph hook drains it.
        assert reg.pop_delivered(handle) == []
        drained = reg.drain("bot", "t1")
        assert [m.id for m in drained] == ["s-1"]
        assert drained[0].delivered
        # Reported exactly once, in order, then nothing.
        assert [m.id for m in reg.pop_delivered(handle)] == ["s-1"]
        assert reg.pop_delivered(handle) == []
        # A clean end: nothing undelivered, key released.
        assert reg.end_turn(handle) == []
        assert reg.active_turn("bot", "t1") is None

    def test_drain_is_idempotent(self) -> None:
        reg = SteerRegistry()
        reg.begin_turn("bot", "t1")
        reg.steer("bot", "t1", "a")
        assert len(reg.drain("bot", "t1")) == 1
        assert reg.drain("bot", "t1") == []
        assert reg.drain("bot", "other") == []

    def test_expected_turn_id_must_match(self) -> None:
        reg = SteerRegistry()
        reg.begin_turn("bot", "t1", "turn-a")
        assert reg.steer("bot", "t1", "x", expected_turn_id="turn-stale") is None
        assert reg.steer("bot", "t1", "x", expected_turn_id="turn-a") is not None
        # No expectation = the running turn, whichever it is.
        assert reg.steer("bot", "t1", "y") is not None

    def test_end_turn_returns_undelivered_and_only_clears_its_own_turn(self) -> None:
        reg = SteerRegistry()
        h1 = reg.begin_turn("bot", "t1", "turn-1")
        reg.steer("bot", "t1", "never read")
        h2 = reg.begin_turn("bot", "t1", "turn-2")  # supersedes h1
        assert reg.active_turn("bot", "t1") == "turn-2"
        # h1 finishing late hands back what it never delivered but does not
        # evict its successor.
        left = reg.end_turn(h1)
        assert [m.text for m in left] == ["never read"]
        assert reg.active_turn("bot", "t1") == "turn-2"
        assert reg.end_turn(h2) == []
        assert reg.active_turn("bot", "t1") is None

    def test_cancel_outcomes(self) -> None:
        reg = SteerRegistry()
        reg.begin_turn("bot", "t1")
        reg.steer("bot", "t1", "a", steer_id="s-a")
        reg.steer("bot", "t1", "b", steer_id="s-b")
        assert reg.cancel("bot", "t1", "s-a") == "cancelled"
        assert reg.cancel("bot", "t1", "s-a") == "unknown"
        assert [m.id for m in reg.drain("bot", "t1")] == ["s-b"]
        assert reg.cancel("bot", "t1", "s-b") == "delivered"
        assert reg.cancel("bot", "nope", "s-b") == "unknown"

    def test_caps(self) -> None:
        reg = SteerRegistry()
        reg.begin_turn("bot", "t1")
        with pytest.raises(ValueError):
            reg.steer("bot", "t1", "   ")
        with pytest.raises(ValueError):
            reg.steer("bot", "t1", "x" * (MAX_STEER_CHARS + 1))
        for i in range(MAX_PER_TURN):
            assert reg.steer("bot", "t1", f"m{i}") is not None
        with pytest.raises(ValueError):
            reg.steer("bot", "t1", "one too many")
        # Delivered ones still count against the turn.
        reg.drain("bot", "t1")
        with pytest.raises(ValueError):
            reg.steer("bot", "t1", "still too many")

    def test_snapshot_and_reset(self) -> None:
        reg = SteerRegistry()
        reg.begin_turn("bot", "t1", "turn-a")
        reg.steer("bot", "t1", "a")
        assert reg.snapshot() == {"bot:t1": {"turn_id": "turn-a", "pending": 1, "delivered": 0}}
        reg.reset()
        assert reg.snapshot() == {}


class TestMiddleware:
    def test_before_model_appends_each_pending_steer_once(self) -> None:
        handle = REGISTRY.begin_turn("bot", "th-1")
        REGISTRY.steer("bot", "th-1", "use blue", steer_id="steer-01")
        REGISTRY.steer("bot", "th-1", "and bigger", steer_id="steer-02")
        mw = SteerMiddleware("bot")
        with push_request_context(session_id="th-1"):
            out = mw.before_model({"messages": []}, None)
            assert out is not None
            msgs = out["messages"]
            assert [type(m) for m in msgs] == [HumanMessage, HumanMessage]
            assert msgs[0].content == steer_message_content("use blue")
            assert msgs[0].content.startswith(STEER_HEADER)
            assert msgs[0].id == "steer:steer-01"
            assert msgs[0].additional_kwargs == {"olano_steer_id": "steer-01"}
            assert msgs[1].id == "steer:steer-02"
            # Drained: the next call (the next model call) finds nothing.
            assert mw.before_model({"messages": []}, None) is None
        # …and the runner can now report them.
        assert [m.id for m in REGISTRY.pop_delivered(handle)] == ["steer-01", "steer-02"]

    async def test_abefore_model_matches_the_sync_hook(self) -> None:
        REGISTRY.begin_turn("bot", "th-1")
        REGISTRY.steer("bot", "th-1", "async please")
        mw = SteerMiddleware("bot")
        with push_request_context(session_id="th-1"):
            out = await mw.abefore_model({}, None)
        assert out is not None and len(out["messages"]) == 1
        assert out["messages"][0].content == steer_message_content("async please")

    def test_another_agent_never_drains_this_turn(self) -> None:
        """A peer callee inherits the caller's request context but has its own
        middleware instance — it must not consume the caller's aside."""
        REGISTRY.begin_turn("bot", "th-1")
        REGISTRY.steer("bot", "th-1", "for bot only")
        with push_request_context(session_id="th-1"):
            assert SteerMiddleware("peer").before_model({}, None) is None
            assert SteerMiddleware("bot").before_model({}, None) is not None

    def test_no_request_context_or_no_pending_is_a_noop(self) -> None:
        mw = SteerMiddleware("bot")
        assert mw.before_model({}, None) is None
        REGISTRY.begin_turn("bot", "th-1")
        with push_request_context(session_id="th-1"):
            assert mw.before_model({}, None) is None

    def test_subgraph_guard_leaves_the_steer_for_the_root_graph(self, monkeypatch) -> None:
        """A `task` subagent runs under the same thread id: it must not eat the
        user's message — the main agent reads it at ITS next step."""
        import olano.core.steer_middleware as mod

        REGISTRY.begin_turn("bot", "th-1")
        REGISTRY.steer("bot", "th-1", "main agent only")
        monkeypatch.setattr(mod, "_in_root_graph", lambda: False)
        with push_request_context(session_id="th-1"):
            assert SteerMiddleware("bot").before_model({}, None) is None
        assert REGISTRY.snapshot()["bot:th-1"]["pending"] == 1
        monkeypatch.setattr(mod, "_in_root_graph", lambda: True)
        with push_request_context(session_id="th-1"):
            assert SteerMiddleware("bot").before_model({}, None) is not None

    def test_in_root_graph_outside_any_graph(self) -> None:
        from olano.core.steer_middleware import _in_root_graph

        assert _in_root_graph() is True

    def test_builder_returns_one_named_instance(self, base_config) -> None:
        mws = build_steer_middleware(base_config)
        assert len(mws) == 1
        assert mws[0].name == "SteerMiddleware"
        assert mws[0].agent_name == base_config.name
