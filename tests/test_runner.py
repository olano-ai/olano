"""Tests for AgentRunner — session management, invoke, stream, summarization."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from olano.config.agent_config import AgentConfig
from olano.core.middleware_loader import MiddlewareHooks
from olano.core.runner import (
    AgentError,
    AgentRunner,
    _classify_error,
    _explicit_peer_target_from_message,
    _manual_action_indices,
    _repeated_failure_response,
    _resolve_autonomous_decisions,
    _resume_payload_for_actions,
    _tool_error_reason,
)
from olano.security.request_context import make_origin, push_request_context
from olano.tools import talk_to as tt


@pytest.fixture
def runner(base_config: AgentConfig, mock_agent, tmp_workspace: Path) -> AgentRunner:
    runner = AgentRunner(mock_agent, base_config)
    return runner


def _recording_astream(captured: list, reply: str = "ok"):
    """A graph ``astream`` that records every payload it was given and streams
    one text chunk back. ``ainvoke`` is a wrapper over the stream path, so a
    test that wants to see what the model was sent reads ``captured[-1]``."""

    async def _astream(*args, **kwargs):
        captured.append(args[0] if args else kwargs.get("input"))
        yield (AIMessageChunk(content=reply), {})

    return _astream


def _failing_astream(exc: Exception):
    """A graph ``astream`` whose first pull raises *exc*."""

    async def _astream(*args, **kwargs):
        raise exc
        yield  # pragma: no cover - makes this an async generator

    return _astream


class TestClassifyError:
    def test_credits(self) -> None:
        err = Exception("credit balance is too low")
        msg = _classify_error(err)
        assert "credits" in msg.lower()

    def test_auth(self) -> None:
        err = Exception("invalid_api_key")
        msg = _classify_error(err)
        assert "api key" in msg.lower()

    def test_rate_limit(self) -> None:
        err = Exception("rate_limit exceeded")
        msg = _classify_error(err)
        assert "rate limit" in msg.lower()

    def test_context_window(self) -> None:
        err = Exception("context_length exceeded")
        msg = _classify_error(err)
        assert "context" in msg.lower() or "reset" in msg.lower()

    def test_generic(self) -> None:
        err = ValueError("something went wrong")
        msg = _classify_error(err)
        assert "something went wrong" in msg

    def test_codex_stream_tail_parse_error(self) -> None:
        err = TypeError("'NoneType' object is not iterable")
        msg = _classify_error(err, "openai-codex")
        assert msg == "OpenAI Codex stream ended with an unsupported terminal event."


class TestToolErrorReason:
    def test_summarizes_pip_network_failure_and_yaml_package_name(self) -> None:
        content = """
WARNING: Retrying (Retry(total=4, connect=None, read=None, redirect=None, status=None)) after
connection broken by 'NewConnectionError(...): Failed to establish a new connection:
[Errno -3] Temporary failure in name resolution': /simple/yaml/
ERROR: Could not find a version that satisfies the requirement yaml (from versions: none)
ERROR: No matching distribution found for yaml
"""
        reason = _tool_error_reason(content)

        assert "couldn't reach pypi" in reason.lower()
        assert "docker sandbox" in reason.lower()
        assert "PyYAML" in reason

    def test_repeated_failure_response_uses_summarized_pip_reason(self) -> None:
        response = _repeated_failure_response(
            "execute",
            "ERROR: No matching distribution found for yaml",
            3,
        )

        assert response.startswith("I tried that 3 times and it kept failing because ")
        assert "there is no package named 'yaml'; use 'PyYAML' instead." in response


def test_explicit_peer_target_from_message_resolves_case_insensitive_peer() -> None:
    class _Runner:
        pass

    tt.bind_runtime(type("R", (), {"_runners": {"kevin": _Runner(), "aurelius": _Runner()}})())
    try:
        assert (
            _explicit_peer_target_from_message(
                "Can you ask Kevin to buy one stock of AMD?", "aurelius"
            )
            == "kevin"
        )
    finally:
        tt._RUNTIME = None


class TestInterruptApprovals:
    def test_profile_save_tools_auto_approved(self, base_config: AgentConfig) -> None:
        manual = _manual_action_indices(
            [
                {"name": "user_profile_save", "interrupt_id": "1"},
                {"name": "identity_preference_save", "interrupt_id": "2"},
                {"name": "soul_preference_save", "interrupt_id": "3"},
                {"name": "write_file", "interrupt_id": "4"},
            ],
            base_config,
        )

        # The three profile-save tools auto-approve; only write_file needs a human.
        assert manual == [3]

    def test_low_risk_shell_interrupts_auto_approved(self, base_config: AgentConfig) -> None:
        manual = _manual_action_indices(
            [
                {"name": "execute", "interrupt_id": "1", "args": {"command": "pwd"}},
                {"name": "execute", "interrupt_id": "2", "args": {"command": "git status"}},
            ],
            base_config,
        )

        # `pwd` is low-risk (auto); `git status` stays manual.
        assert manual == [1]

    def test_low_risk_auto_approval_can_be_disabled(self, base_config: AgentConfig) -> None:
        base_config.security.hitl_auto_approve_low_risk = False

        manual = _manual_action_indices(
            [
                {"name": "execute", "interrupt_id": "1", "args": {"command": "pwd"}},
            ],
            base_config,
        )

        assert manual == [0]


class TestResumePayloadShape:
    """`Command(resume=...)` must be scoped per interrupt id once more than one
    branch is parked, or LangGraph refuses the resume outright:

        RuntimeError: When there are multiple pending interrupts, you must
        specify the interrupt id when resuming.

    LangGraph reads a dict as an id→value map only when EVERY key is an
    xxh3-128 hexdigest (`pregel/_utils.py:is_xxh3_128_hexdigest`), which is
    exactly the format of `Interrupt.id`.
    """

    # Real ids: 32 lowercase hex chars, per `Interrupt.from_ns`.
    ID_A = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    ID_B = "0f9e8d7c6b5a49382716f5e4d3c2b1a0"

    def test_single_interrupt_keeps_the_flat_decisions_shape(self) -> None:
        actions = [
            {"name": "task", "interrupt_id": self.ID_A},
            {"name": "task", "interrupt_id": self.ID_A},
        ]
        decisions = [{"type": "approve"}, {"type": "reject"}]

        # One pending interrupt carrying two action requests: the middleware
        # wants both decisions in one list, in order.
        assert _resume_payload_for_actions(actions, decisions) == {
            "decisions": [{"type": "approve"}, {"type": "reject"}]
        }

    def test_multiple_interrupts_resume_by_id(self) -> None:
        actions = [
            {"name": "task", "interrupt_id": self.ID_A},
            {"name": "write_file", "interrupt_id": self.ID_B},
            {"name": "execute", "interrupt_id": self.ID_A},
        ]
        decisions = [{"type": "approve"}, {"type": "reject"}, {"type": "approve"}]

        payload = _resume_payload_for_actions(actions, decisions)

        # Keyed by interrupt id, and each interrupt gets only ITS OWN decisions:
        # the middleware raises unless the count matches that interrupt's own
        # action_requests.
        assert payload == {
            self.ID_A: {"decisions": [{"type": "approve"}, {"type": "approve"}]},
            self.ID_B: {"decisions": [{"type": "reject"}]},
        }

    def test_multiple_interrupts_fall_back_when_ids_are_not_hexdigests(self) -> None:
        actions = [
            {"name": "task", "interrupt_id": "interrupt-1"},
            {"name": "task", "interrupt_id": ""},
        ]
        decisions = [{"type": "approve"}, {"type": "reject"}]

        # A part-hexdigest map is read by LangGraph as one scalar resume value,
        # which the middleware cannot parse — stay on the flat shape instead.
        assert _resume_payload_for_actions(actions, decisions) == {
            "decisions": [{"type": "approve"}, {"type": "reject"}]
        }

    def test_manual_indices_distinguish_actions_sharing_one_interrupt_id(self) -> None:
        # All three action requests belong to ONE interrupt (same id), so only
        # position can tell them apart when applying decisions back.
        actions = [
            {"name": "user_profile_save", "interrupt_id": self.ID_A},
            {"name": "write_file", "interrupt_id": self.ID_A},
            {"name": "wallet_send", "interrupt_id": self.ID_A},
        ]

        assert _manual_action_indices(actions) == [1, 2]


class TestAutonomousResolver:
    """Origin-aware resolution when no interactive handler is reachable.

    With no delivery channel wired (the default in tests), deferral falls back:
    interactive (human) → approve, non-interactive → reject. Tests with delivery
    patch ``hitl`` to exercise the real inbox path.
    """

    ACTIONS = [{"name": "wallet_send", "interrupt_id": "1"}]

    @pytest.mark.asyncio
    async def test_scheduled_auto_approves(self, base_config: AgentConfig) -> None:
        d = await _resolve_autonomous_decisions(
            self.ACTIONS, base_config, make_origin("scheduled", False)
        )
        assert d == [{"type": "approve"}]

    @pytest.mark.asyncio
    async def test_human_origin_defers_then_approves_without_delivery(
        self, base_config: AgentConfig
    ) -> None:
        # Human-rooted nested A2A defers to the inbox; with no delivery wired it
        # falls back to approve (a human initiated the chain).
        d = await _resolve_autonomous_decisions(
            self.ACTIONS, base_config, make_origin("human", True)
        )
        assert d == [{"type": "approve"}]

    @pytest.mark.asyncio
    async def test_headless_rejects_high_risk(self, base_config: AgentConfig) -> None:
        d = await _resolve_autonomous_decisions(
            self.ACTIONS, base_config, make_origin("peer-unknown", False)
        )
        assert d == [{"type": "reject"}]  # wallet_send is high-risk, no delivery → reject

    @pytest.mark.asyncio
    async def test_headless_approves_low_risk(self, base_config: AgentConfig) -> None:
        d = await _resolve_autonomous_decisions(
            [{"name": "user_profile_save", "interrupt_id": "1"}],
            base_config,
            make_origin("peer-unknown", False),
        )
        assert d == [{"type": "approve"}]

    @pytest.mark.asyncio
    async def test_cognition_respects_auto_approve_switch(self, base_config: AgentConfig) -> None:
        from unittest.mock import patch

        origin = make_origin("cognition", False)
        # A plain workspace write is outside every default approval_required
        # category, so it is what the switch alone decides. (wallet_send is not:
        # it is "irreversible" and held even with the switch on — see
        # TestApprovalRequiredOnCognitionTurns.)
        plain = [{"name": "write_file", "args": {"file_path": "notes.md"}, "interrupt_id": "1"}]
        with patch("olano.config.system_settings.auto_approve_enabled", return_value=True):
            assert await _resolve_autonomous_decisions(plain, base_config, origin) == [
                {"type": "approve"}
            ]
        # auto-approve off → defer; no delivery → non-interactive → reject.
        with patch("olano.config.system_settings.auto_approve_enabled", return_value=False):
            assert await _resolve_autonomous_decisions(plain, base_config, origin) == [
                {"type": "reject"}
            ]

    @pytest.mark.asyncio
    async def test_defer_routes_to_inbox_when_delivery_wired(
        self, base_config: AgentConfig
    ) -> None:
        from unittest.mock import AsyncMock, patch

        origin = make_origin("cognition", False)
        with (
            patch("olano.config.system_settings.auto_approve_enabled", return_value=False),
            patch("olano.security.hitl.has_delivery", return_value=True),
            patch("olano.security.hitl.request_approval", new=AsyncMock(return_value=True)) as req,
        ):
            d = await _resolve_autonomous_decisions(self.ACTIONS, base_config, origin)
            assert d == [{"type": "approve"}]
            req.assert_awaited_once()


class TestApprovalRequiredOnCognitionTurns:
    """``cognition.approval_required`` on a Fully Autonomous cognition turn.

    With the switch ON the cognition branch approves the run's tool calls —
    except the categories the owner said must always see a human, which defer
    to the inbox exactly as an auto-approve-off turn would (no delivery wired
    here ⇒ non-interactive ⇒ reject).
    """

    ORIGIN = make_origin("cognition", False)

    @pytest.mark.asyncio
    async def test_default_holds_external_irreversible_and_identity(
        self, base_config: AgentConfig
    ) -> None:
        actions = [
            {"name": "gmail_send", "args": {"to": "a@b.c"}, "interrupt_id": "1"},  # external_send
            {"name": "wallet_send", "interrupt_id": "2"},  # irreversible (financial)
            {"name": "secret_set", "args": {"name": "X"}, "interrupt_id": "3"},  # irreversible
            {"name": "write_file", "args": {"file_path": "SOUL.md"}, "interrupt_id": "4"},
            {"name": "edit_file", "args": '{"file_path": "/ws/AGENTS.md"}', "interrupt_id": "5"},
            {"name": "write_file", "args": {"file_path": "notes.md"}, "interrupt_id": "6"},
        ]
        with patch("olano.config.system_settings.auto_approve_enabled", return_value=True):
            d = await _resolve_autonomous_decisions(actions, base_config, self.ORIGIN)
        assert d == [
            {"type": "reject"},
            {"type": "reject"},
            {"type": "reject"},
            {"type": "reject"},
            {"type": "reject"},
            {"type": "approve"},
        ]

    @pytest.mark.asyncio
    async def test_held_action_routes_to_inbox_not_a_bare_reject(
        self, base_config: AgentConfig
    ) -> None:
        # The hold is a *defer*: with delivery wired the human decides.
        actions = [{"name": "write_file", "args": {"file_path": "SOUL.md"}, "interrupt_id": "1"}]
        with (
            patch("olano.config.system_settings.auto_approve_enabled", return_value=True),
            patch("olano.security.hitl.has_delivery", return_value=True),
            patch("olano.security.hitl.request_approval", new=AsyncMock(return_value=True)) as req,
        ):
            d = await _resolve_autonomous_decisions(actions, base_config, self.ORIGIN)
        assert d == [{"type": "approve"}]
        req.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_list_restores_blanket_approve(self, base_config: AgentConfig) -> None:
        base_config.cognition.approval_required = []
        actions = [
            {"name": "gmail_send", "args": {"to": "a@b.c"}, "interrupt_id": "1"},
            {"name": "wallet_send", "interrupt_id": "2"},
            {"name": "write_file", "args": {"file_path": "SOUL.md"}, "interrupt_id": "3"},
        ]
        with patch("olano.config.system_settings.auto_approve_enabled", return_value=True):
            d = await _resolve_autonomous_decisions(actions, base_config, self.ORIGIN)
        assert d == [{"type": "approve"}] * 3

    @pytest.mark.asyncio
    async def test_narrowed_list_holds_only_what_it_names(self, base_config: AgentConfig) -> None:
        base_config.cognition.approval_required = ["identity_edit"]
        actions = [
            {"name": "gmail_send", "args": {"to": "a@b.c"}, "interrupt_id": "1"},
            {"name": "write_file", "args": {"file_path": "SOUL.md"}, "interrupt_id": "2"},
        ]
        with patch("olano.config.system_settings.auto_approve_enabled", return_value=True):
            d = await _resolve_autonomous_decisions(actions, base_config, self.ORIGIN)
        assert d == [{"type": "approve"}, {"type": "reject"}]

    @pytest.mark.asyncio
    async def test_cross_agent_exec_still_held_with_empty_list(
        self, base_config: AgentConfig
    ) -> None:
        # approval_required: [] lifts the category holds, never the standing
        # rule that a command in another agent's workspace sees a human.
        base_config.cognition.approval_required = []
        actions = [
            {
                "name": "olano_run_agent_command",
                "args": {"agent": "other", "command": "ls"},
                "interrupt_id": "1",
            }
        ]
        with patch("olano.config.system_settings.auto_approve_enabled", return_value=True):
            d = await _resolve_autonomous_decisions(actions, base_config, self.ORIGIN)
        assert d == [{"type": "reject"}]


class TestAgentRunnerSync:
    def test_invoke_returns_string(self, runner: AgentRunner) -> None:
        thread_id = str(uuid.uuid4())
        result = runner.invoke("Hello", thread_id=thread_id)
        assert isinstance(result, str)
        assert "mock" in result.lower()

    def test_request_context_metadata_includes_session_shared_paths(
        self, runner: AgentRunner
    ) -> None:
        thread_id = str(uuid.uuid4())
        runner.update_thread_state(
            thread_id,
            session_shared_paths=[
                {
                    "id": "share1",
                    "source": "/tmp/example",
                    "target": "/shared/example",
                    "mode": "ro",
                    "persistence": "session",
                    "subjects": [{"type": "agent", "name": "self"}],
                }
            ],
        )

        metadata = runner._request_context_metadata(thread_id)

        assert metadata["workspace_root"] == str(runner.workspace.root)
        assert metadata["session_shared_paths"][0]["target"] == "/shared/example"

    def test_invoke_persists_session(self, runner: AgentRunner, tmp_workspace: Path) -> None:
        thread_id = str(uuid.uuid4())
        runner.invoke("Hello", thread_id=thread_id)
        session = runner.load_session(thread_id)
        assert len(session) == 2  # user + assistant
        assert session[0]["role"] == "user"
        assert session[1]["role"] == "assistant"

    def test_load_session_empty(self, runner: AgentRunner) -> None:
        session = runner.load_session(str(uuid.uuid4()))
        assert session == []


class TestAgentRunnerAsync:
    @pytest.mark.asyncio
    async def test_ainvoke(self, runner: AgentRunner) -> None:
        thread_id = str(uuid.uuid4())
        result = await runner.ainvoke("Hello", thread_id=thread_id)
        assert isinstance(result, str)
        assert result

    @pytest.mark.asyncio
    async def test_ainvoke_scopes_resume_per_interrupt_id(self, base_config: AgentConfig) -> None:
        """The non-streaming entry point (scheduler / peer / headless) IS the
        stream path now, so it resumes with the same id-scoped map as chat and
        resolves the interrupt by origin (no handler → `_resolve_autonomous_decisions`)."""
        from langgraph.types import Command

        id_a = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        id_b = "0f9e8d7c6b5a49382716f5e4d3c2b1a0"
        resume_payloads: list = []
        streams = 0

        async def fake_astream(payload, *args, **kwargs):
            nonlocal streams
            streams += 1
            if isinstance(payload, Command):
                resume_payloads.append(payload.resume)
                yield AIMessage(content="done")
                return
            # Two gated tool calls left un-executed: that is what makes the
            # runner read the checkpoint and find the interrupts.
            yield AIMessage(
                content="",
                tool_calls=[
                    {"id": "tc-a", "name": "write_file", "args": {"path": "a"}},
                    {"id": "tc-b", "name": "write_file", "args": {"path": "b"}},
                ],
            )

        agent = MagicMock()
        agent.astream = fake_astream
        post_stream_states = [
            MagicMock(
                interrupts=[
                    MagicMock(
                        id=id_a,
                        value={"action_requests": [{"name": "write_file", "args": {}}]},
                    ),
                    MagicMock(
                        id=id_b,
                        value={"action_requests": [{"name": "write_file", "args": {}}]},
                    ),
                ]
            ),
            MagicMock(interrupts=[]),
        ]

        async def fake_get_state(*args, **kwargs):
            # The state is read before the first stream (the usage-billing
            # snapshot) and after each one (the interrupt check). Only the
            # latter advances this script.
            if not streams:
                return MagicMock(interrupts=[], values={"messages": []})
            return post_stream_states.pop(0) if post_stream_states else MagicMock(interrupts=[])

        agent.aget_state = AsyncMock(side_effect=fake_get_state)
        runner = AgentRunner(agent, base_config)

        async def _approve_all(actions, *_args, **_kwargs):
            return [{"type": "approve"} for _ in actions]

        with patch(
            "olano.core.runner._resolve_autonomous_decisions",
            AsyncMock(side_effect=_approve_all),
        ):
            reply = await runner.ainvoke("write both files", thread_id=str(uuid.uuid4()))

        assert reply == "done"
        assert resume_payloads == [
            {
                id_a: {"decisions": [{"type": "approve"}]},
                id_b: {"decisions": [{"type": "approve"}]},
            }
        ]

    @pytest.mark.asyncio
    async def test_ainvoke_bills_only_this_turn_not_the_whole_thread(
        self, base_config: AgentConfig
    ) -> None:
        """`ainvoke` on a checkpointed thread returns the WHOLE history.

        Folding that result into the turn's usage therefore re-billed every
        earlier turn on every turn — quadratic in thread length, and only on
        the non-streaming path (scheduler jobs, heartbeats, `talk_to_agent`
        peers, `delegate_task`), which is exactly the traffic that runs
        unattended on a long-lived thread. It is how one agent booked ten
        BILLION input tokens in a month against a ceiling meant never to be
        reached. The ACP path already deltas against the prior state
        (`acp/server._prior_message_ids`); this is the same guarantee for the
        runner.
        """
        import asyncio
        from types import SimpleNamespace

        history: list[AIMessage] = []

        async def fake_astream(payload, *args, **kwargs):
            msg = AIMessage(
                content="ok",
                id=f"ai-{len(history)}",
                usage_metadata={
                    "input_tokens": 1_000,
                    "output_tokens": 10,
                    "total_tokens": 1_010,
                },
            )
            history.append(msg)
            # A stream hands back only this turn's chunks; the accumulated
            # state lives in the checkpoint the runner snapshots below.
            yield msg

        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(
            side_effect=lambda *a, **k: SimpleNamespace(
                interrupts=[], values={"messages": list(history)}
            )
        )
        runner = AgentRunner(agent, base_config)

        billed: list[dict] = []
        threads: list[str] = []
        with patch(
            "olano.core.runner._record_usage",
            side_effect=lambda cfg, meta, tid="": (
                billed.append(dict(meta)),
                threads.append(tid),
            )
            and 0.0,
        ):
            for turn in range(1, 4):
                await runner.ainvoke("hello", thread_id="long-lived-thread")
                # Recording runs off the turn's critical path (a background
                # task over a thread); give it a moment to land.
                for _ in range(200):
                    if len(billed) >= turn:
                        break
                    await asyncio.sleep(0.01)

        assert [b["input_tokens"] for b in billed] == [1_000, 1_000, 1_000]
        assert [b["model_calls"] for b in billed] == [1, 1, 1]
        # The ledger row names the conversation, so the drill-down can rank
        # threads without mining checkpoint state.
        assert threads == ["long-lived-thread"] * 3

    @pytest.mark.asyncio
    async def test_ainvoke_persists_every_step_like_chat(self, base_config: AgentConfig) -> None:
        """A scheduled run's thread used to show the prompt and the final answer
        and nothing in between: `ainvoke` drove the graph itself and wrote only
        a `user` and an `assistant` row, while the same turn typed into chat
        went through `astream_events` and recorded every tool call, tool result
        and `write_todos` plan. `ainvoke` is a wrapper over the stream path
        now, so the rows (and the live `thread_updated` frames they publish)
        are the same whichever surface started the turn."""

        async def fake_astream(*args, **kwargs):
            yield AIMessage(
                content="Planning.",
                tool_calls=[
                    {
                        "id": "c1",
                        "name": "write_todos",
                        "args": {"todos": [{"content": "load profile", "status": "pending"}]},
                    }
                ],
            )
            yield ToolMessage(content="Updated todo list", tool_call_id="c1", name="write_todos")
            yield AIMessage(content="Done: profile loaded.")

        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        runner = AgentRunner(agent, base_config)

        thread_id = str(uuid.uuid4())
        with push_request_context(source="scheduled", channel="scheduler"):
            reply = await runner.ainvoke("run the daily cycle", thread_id=thread_id)

        assert reply == "Planning.\n\nDone: profile loaded."
        rows = runner.load_session(thread_id)
        assert [r["role"] for r in rows] == ["user", "tool_call", "tool_result", "assistant"]
        assert rows[1]["name"] == "write_todos"
        assert rows[2]["content"].startswith("write_todos: Updated todo list")
        # The surface is stamped, so the rail can label the thread.
        assert rows[0]["channel"] == "scheduler"

    @pytest.mark.asyncio
    async def test_ainvoke_forwards_per_turn_overrides_to_the_stream(
        self, runner: AgentRunner
    ) -> None:
        """A scheduled job's model / effort ride the same per-turn override the
        chat chips use; an absent one is forwarded as None, never as ""."""
        seen: dict = {}

        async def fake_stream(message, **kwargs):
            seen.update(kwargs)
            yield ("text", "ok")

        runner.astream_events = fake_stream  # type: ignore[method-assign]
        reply = await runner.ainvoke(
            "hi", thread_id="t", override_model="olano:pro", override_reasoning_effort="high"
        )
        assert reply == "ok"
        assert seen["override_model"] == "olano:pro"
        assert seen["override_reasoning_effort"] == "high"

        await runner.ainvoke("hi", thread_id="t", override_model="", override_reasoning_effort="")
        assert seen["override_model"] is None
        assert seen["override_reasoning_effort"] is None

    @pytest.mark.asyncio
    async def test_ainvoke_persists_peer_inbound_turns(self, runner: AgentRunner) -> None:
        from olano.security.request_context import push_request_context

        thread_id = "acp::test-agent"
        with push_request_context(source="peer", channel="talk_to_agent", user_id="aurelius"):
            await runner.ainvoke("Carlos asked: what is BTC?", thread_id=thread_id)

        session = runner.load_session(thread_id)
        assert session[0]["role"] == "peer_inbound"
        assert session[0]["from"] == "aurelius"
        assert session[1]["role"] == "assistant"
        assert session[1]["via_peer"] == "aurelius"

    @pytest.mark.asyncio
    async def test_ainvoke_injects_a2a_preamble_but_keeps_jsonl_raw(
        self, runner: AgentRunner
    ) -> None:
        """An `a2a_preamble` on the request-context metadata is injected as a
        first-turn system message but is NOT written to the JSONL transcript
        (which stays the raw peer_inbound message)."""
        from olano.security.request_context import push_request_context

        thread_id = "acp::aurelius::test-agent"
        preamble = "## Prior conversation context with peer agent 'aurelius'\n- earlier stuff"
        captured: list = []
        runner.agent.astream = _recording_astream(captured)
        with push_request_context(
            source="peer",
            channel="talk_to_agent",
            user_id="aurelius",
            metadata={"a2a_preamble": preamble},
        ):
            await runner.ainvoke("what is the latest?", thread_id=thread_id)

        msgs = captured[-1]["messages"]
        assert msgs[0] == {"role": "system", "content": preamble}
        assert msgs[-1] == {"role": "user", "content": "what is the latest?"}

        session = runner.load_session(thread_id)
        assert session[0]["role"] == "peer_inbound"
        assert session[0]["content"] == "what is the latest?"
        assert "Prior conversation context" not in session[0]["content"]

    @pytest.mark.asyncio
    async def test_ainvoke_error_raises_agent_error(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        bad_agent = MagicMock()
        bad_agent.astream = _failing_astream(Exception("invalid_api_key"))
        runner = AgentRunner(bad_agent, base_config)
        with pytest.raises(AgentError) as exc_info:
            await runner.ainvoke("Hello")
        assert "api key" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_ainvoke_classifies_with_the_agents_provider(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        """`ainvoke` used to classify with provider=None while the streaming path
        passed one, so every non-streaming turn — scheduler jobs, delegate_task,
        channels off the stream — skipped the whole ``provider == "olano"``
        branch and fell through to the raw "API error (…)" text."""
        from olano.core.actionable_errors import CODE_OLANO_UPSTREAM

        relayed = (
            "Error code: 402 - litellm.BadRequestError: Insufficient Balance. "
            "Received Model Group=olano-turbo"
        )
        base_config.model = "olano:turbo"
        bad_agent = MagicMock()
        bad_agent.astream = _failing_astream(Exception(relayed))
        runner = AgentRunner(bad_agent, base_config)
        with pytest.raises(AgentError) as exc_info:
            await runner.ainvoke("Hello")
        detail = exc_info.value.detail
        assert detail is not None and detail.code == CODE_OLANO_UPSTREAM
        assert "API error" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_ainvoke_detaches_from_caller_stream_context(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        """A nested ``runner.ainvoke`` (e.g. ``talk_to_agent`` invoking a peer
        while the caller is mid-stream) must NOT inherit the caller's LangChain
        runnable config. Otherwise the caller's streaming callback handler is
        merged into the callee's model call and the callee's tokens interleave
        into the caller's channel reply — the Telegram ``talk_to_agent`` garble.
        """
        from langchain_core.runnables.config import var_child_runnable_config

        seen: dict[str, object] = {}

        async def _capture_astream(payload, *args, **kwargs):
            # What the nested graph would inherit as its ambient run config.
            seen["ambient"] = var_child_runnable_config.get()
            yield AIMessage(content="peer reply")

        agent = MagicMock()
        agent.astream = _capture_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        runner = AgentRunner(agent, base_config)

        # Simulate being called from inside a caller's active stream: the
        # caller's StreamMessagesHandler lives in var_child_runnable_config.
        sentinel = object()
        parent_cfg = {"callbacks": [sentinel]}
        parent_token = var_child_runnable_config.set(parent_cfg)
        try:
            reply = await runner.ainvoke("hi from caller", thread_id="acp::caller::peer")
            # The callee must have run detached — no caller callbacks visible.
            ambient = seen["ambient"] or {}
            assert not ambient.get("callbacks"), (
                "peer inherited the caller's streaming callbacks — its tokens "
                "will interleave into the caller's reply"
            )
            # ...and the caller's own context is restored on the way out, so the
            # detachment is properly scoped rather than a permanent clobber.
            assert var_child_runnable_config.get() == parent_cfg
        finally:
            var_child_runnable_config.reset(parent_token)

        assert reply == "peer reply"

    @pytest.mark.asyncio
    async def test_astream(self, runner: AgentRunner) -> None:
        thread_id = str(uuid.uuid4())
        chunks: list[str] = []
        async for chunk in runner.astream("Hello", thread_id=thread_id):
            chunks.append(chunk)
        assert chunks
        assert "".join(chunks)

    @pytest.mark.asyncio
    async def test_astream_persists_session(self, runner: AgentRunner) -> None:
        thread_id = str(uuid.uuid4())
        async for _ in runner.astream("Hello", thread_id=thread_id):
            pass
        session = runner.load_session(thread_id)
        assert any(e["role"] == "assistant" for e in session)

    @pytest.mark.asyncio
    async def test_astream_keeps_session_append_only(self, runner: AgentRunner) -> None:
        # deepagents' SummarizationMiddleware owns model-context bounding; olano
        # must NOT destructively compact its JSONL transcript after a turn.
        thread_id = str(uuid.uuid4())
        async for _ in runner.astream("Hello", thread_id=thread_id):
            pass

        session = runner.load_session(thread_id)
        assert any(e["role"] == "user" for e in session)
        assert any(e["role"] == "assistant" for e in session)

    @pytest.mark.asyncio
    async def test_astream_events_ignores_codex_terminal_parse_error_after_text(
        self, base_config: AgentConfig
    ) -> None:
        base_config.model = "openai-codex:gpt-5.5"
        agent = MagicMock()

        async def failing_stream(*args, **kwargs):
            yield (AIMessageChunk(content="Hello from Codex."), {})
            raise TypeError("'NoneType' object is not iterable")

        agent.astream = failing_stream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock()
        runner = AgentRunner(agent, base_config)

        events = []
        async for event in runner.astream_events("Hello", thread_id="codex-tail"):
            events.append(event)

        assert ("text", "Hello from Codex.") in events
        assert runner.load_session("codex-tail")[-1]["content"] == "Hello from Codex."

    @pytest.mark.asyncio
    async def test_astream_events_drops_nostream_chunks_from_the_answer(
        self, base_config: AgentConfig
    ) -> None:
        """A model called from inside a tool must never reach the reply.

        This is the Telegram garble, reproduced at the seam that renders it: two
        `image_describe` calls from one photo burst ran concurrently, both
        streamed into the parent turn, and `render_events` joined every "text"
        event into one message — two transcriptions interleaved chunk-by-chunk.
        `stream_isolation` stops those calls at the source; this is the backstop
        that holds even if a future in-graph caller forgets to detach.
        """
        agent = MagicMock()

        async def stream(*args, **kwargs):
            # Two nested vision calls, interleaved exactly as they arrived.
            yield (AIMessageChunk(content="Here is all the visible text"), {"tags": ["nostream"]})
            yield (AIMessageChunk(content="Here is all the text from"), {"tags": ["nostream"]})
            yield (AIMessageChunk(content=" in the image:"), {"tags": ["nostream"]})
            # …and the agent's own answer.
            yield (AIMessageChunk(content="Your 11NCO schedule starts at 16.20."), {})

        agent.astream = stream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock()
        runner = AgentRunner(agent, base_config)

        events = [ev async for ev in runner.astream_events("schedule", thread_id="nostream")]
        answer = "".join(p for k, p in events if k == "text")
        assert answer == "Your 11NCO schedule starts at 16.20."
        assert "visible text" not in answer
        # The transcript records the answer, not the machinery behind it.
        assert runner.load_session("nostream")[-1]["content"] == answer

    def test_is_internal_chunk_markers(self) -> None:
        """Both markers, and neither firing on an ordinary answer chunk."""
        plain = AIMessageChunk(content="hello")
        assert AgentRunner._is_internal_chunk(plain, {}) is False
        assert AgentRunner._is_internal_chunk(plain, {"tags": ["nostream"]}) is True
        assert AgentRunner._is_internal_chunk(plain, {"lc_source": "summarization"}) is True
        assert AgentRunner._is_internal_chunk(plain, {"tags": ["cognition"]}) is False

    @pytest.mark.asyncio
    async def test_astream_events_counts_subagent_usage(self, base_config: AgentConfig) -> None:
        """Subagent (`task`) tokens bill the same account as the parent's, so
        the turn's usage frame — and the ledger row built from it — must fold
        in namespaced chunks. They used to be skipped wholesale, which is how
        the local credits number under-counted every subagent-heavy turn."""
        agent = MagicMock()

        async def stream(*args, **kwargs):
            sub = AIMessageChunk(content="sub thinking")
            sub.usage_metadata = {"input_tokens": 400, "output_tokens": 40, "total_tokens": 440}
            yield (("tools:abc",), "messages", (sub, {}))
            root = AIMessageChunk(content="answer")
            root.usage_metadata = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
            yield (root, {})

        agent.astream = stream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock()
        runner = AgentRunner(agent, base_config)

        events = [ev async for ev in runner.astream_events("Hi", thread_id="sub-usage")]
        frames = [d for k, d in events if k == "usage"]
        assert frames, "turn with usage must emit a usage frame"
        assert frames[0]["input_tokens"] == 500
        assert frames[0]["output_tokens"] == 50
        assert frames[0]["model_calls"] == 2
        # The subagent text still surfaces as its own event, not as parent text.
        assert any(k == "subagent_text" for k, _ in events)

    @pytest.mark.asyncio
    async def test_astream_events_still_excludes_peer_callee_usage(
        self, base_config: AgentConfig
    ) -> None:
        """The double-count guard: chunks streamed while ANOTHER agent is bound
        (a talk_to_agent callee's nested run) are that runner's to record, not
        this one's — the subagent merge must stay below the active-agent skip."""
        from olano.tools.talk_to import current_agent

        agent = MagicMock()

        async def stream(*args, **kwargs):
            token = current_agent.set("someone-else")
            try:
                foreign = AIMessageChunk(content="callee text")
                foreign.usage_metadata = {
                    "input_tokens": 999,
                    "output_tokens": 99,
                    "total_tokens": 1098,
                }
                yield (foreign, {})
            finally:
                current_agent.reset(token)
            mine = AIMessageChunk(content="mine")
            mine.usage_metadata = {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}
            yield (mine, {})

        agent.astream = stream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock()
        runner = AgentRunner(agent, base_config)

        events = [ev async for ev in runner.astream_events("Hi", thread_id="peer-skip")]
        frames = [d for k, d in events if k == "usage"]
        assert frames and frames[0]["input_tokens"] == 10
        assert frames[0]["model_calls"] == 1

    @pytest.mark.asyncio
    async def test_astream_events_override_model_uses_transient_agent(
        self, base_config: AgentConfig
    ) -> None:
        # The shared (configured) agent would stream "from shared".
        shared = MagicMock()

        async def shared_stream(*args, **kwargs):
            yield (AIMessageChunk(content="from shared"), {})

        shared.astream = shared_stream
        shared.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        shared.ainvoke = AsyncMock()
        runner = AgentRunner(shared, base_config)
        original_model = base_config.model

        # The transient agent built for the override streams "from override".
        transient = MagicMock()

        async def transient_stream(*args, **kwargs):
            yield (AIMessageChunk(content="from override"), {})

        transient.astream = transient_stream
        transient.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        transient.ainvoke = AsyncMock()

        built: dict = {}

        async def fake_build(cfg, *args, **kwargs):
            built["model"] = cfg.model
            return transient

        with (
            patch("olano.core.builder.build_agent_async", side_effect=fake_build),
            patch("olano.core.builder.build_agent_with_mcp", side_effect=fake_build),
        ):
            events = [
                ev
                async for ev in runner.astream_events(
                    "Hi", thread_id="ov-1", override_model="anthropic:claude-opus-4-6"
                )
            ]

        # Ran on the override model's transient agent, not the shared one.
        assert ("text", "from override") in events
        assert ("text", "from shared") not in events
        assert built["model"] == "anthropic:claude-opus-4-6"
        # Shared agent + config are never mutated (safe for parallel streams).
        assert runner.agent is shared
        assert runner.config.model == original_model
        # The transient agent is cached per (override model, reasoning effort).
        assert runner._override_agents.get("anthropic:claude-opus-4-6|") is transient

    @pytest.mark.asyncio
    async def test_astream_events_override_equal_to_config_skips_build(
        self, base_config: AgentConfig
    ) -> None:
        shared = MagicMock()

        async def shared_stream(*args, **kwargs):
            yield (AIMessageChunk(content="from shared"), {})

        shared.astream = shared_stream
        shared.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        shared.ainvoke = AsyncMock()
        runner = AgentRunner(shared, base_config)

        with patch("olano.core.builder.build_agent_async") as bld:
            events = [
                ev
                async for ev in runner.astream_events(
                    "Hi", thread_id="ov-2", override_model=base_config.model
                )
            ]

        assert ("text", "from shared") in events
        bld.assert_not_called()
        assert runner._override_agents == {}

    @pytest.mark.asyncio
    async def test_set_agent_clears_override_cache(
        self, base_config: AgentConfig, mock_agent
    ) -> None:
        runner = AgentRunner(mock_agent, base_config)
        runner._override_agents["anthropic:claude-opus-4-6|"] = MagicMock()
        new_agent = MagicMock()
        runner.set_agent(new_agent)
        assert runner.agent is new_agent
        assert runner._override_agents == {}

    @pytest.mark.asyncio
    async def test_ensure_thread_loaded_hydrates_empty_graph(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        agent = MagicMock()
        empty_state = MagicMock(values={})
        agent.aget_state = AsyncMock(return_value=empty_state)
        agent.aupdate_state = AsyncMock(return_value={})
        runner = AgentRunner(agent, base_config)

        thread_id = str(uuid.uuid4())
        runner._append_session(thread_id, "user", "Where is Costa Rica?")
        runner._append_session(thread_id, "assistant", "Costa Rica is in Central America.")

        entries = await runner.ensure_thread_loaded(thread_id)

        assert len(entries) == 2
        agent.aupdate_state.assert_awaited_once()
        args, kwargs = agent.aupdate_state.await_args
        # Checkpoint key is agent-scoped (per-agent isolation).
        assert args[0] == runner._invoke_config(thread_id)
        assert args[0]["configurable"]["thread_id"] == f"{base_config.name}::{thread_id}"
        hydrated_messages = args[1]["messages"]
        assert len(hydrated_messages) == 2
        assert hydrated_messages[0].content == "Where is Costa Rica?"
        assert hydrated_messages[1].content == "Costa Rica is in Central America."

    @pytest.mark.asyncio
    async def test_fork_thread_copies_visible_transcript_only(
        self, base_config: AgentConfig, tmp_workspace: Path
    ) -> None:
        agent = MagicMock()
        agent.aupdate_state = AsyncMock(return_value={})
        runner = AgentRunner(agent, base_config)

        source_thread_id = str(uuid.uuid4())
        runner._append_session(source_thread_id, "assistant", "Hey Carlos. What can I help with?")
        runner._append_session(source_thread_id, "user", "Debug prompt mode none.")
        runner.update_thread_state(source_thread_id, bash_cwd="/workspace/system")

        new_thread_id = await runner.fork_thread(source_thread_id)

        assert new_thread_id != source_thread_id
        copied_entries = runner.load_session(new_thread_id)
        assert copied_entries == runner.load_session(source_thread_id)
        copied_state = runner.load_thread_state(new_thread_id)
        assert copied_state["bash_cwd"] == "/workspace/system"

        agent.aupdate_state.assert_awaited_once()
        args, _ = agent.aupdate_state.await_args
        # The forked thread's checkpoint key is agent-scoped too.
        assert args[0] == runner._invoke_config(new_thread_id)
        hydrated_messages = args[1]["messages"]
        assert [msg.content for msg in hydrated_messages] == [
            "Hey Carlos. What can I help with?",
            "Debug prompt mode none.",
        ]

    @pytest.mark.asyncio
    async def test_astream_events_logs_raw_user_message_not_injected_prompt(
        self, runner: AgentRunner
    ) -> None:
        hooks = MiddlewareHooks()
        runner.agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))

        async def on_input(text, config, context):
            return f"[hidden instruction]\\n{text}"

        hooks.on_input.append(on_input)
        runner._mw_hooks = hooks

        thread_id = str(uuid.uuid4())
        async for _ in runner.astream_events("Hello there", thread_id=thread_id):
            pass

        session = runner.load_session(thread_id)
        assert session[0]["role"] == "user"
        assert session[0]["content"] == "Hello there"
        assert "[hidden instruction]" not in session[0]["content"]

    @pytest.mark.asyncio
    async def test_astream_events_reuses_middleware_context_within_thread_and_isolates_threads(
        self, base_config: AgentConfig
    ) -> None:
        seen_counts: list[tuple[str, int]] = []
        seen_context_ids: list[tuple[str, int]] = []
        hooks = MiddlewareHooks()

        async def on_input(text, config, context):
            counts = context.setdefault("counts", {})
            counts["on_input"] = counts.get("on_input", 0) + 1
            seen_counts.append((context["thread_id"], counts["on_input"]))
            seen_context_ids.append((context["thread_id"], id(context)))
            return text

        hooks.on_input.append(on_input)

        async def empty_astream(*args, **kwargs):
            if False:
                yield None

        agent = MagicMock()
        agent.astream = empty_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock(return_value={"messages": [MagicMock(type="ai", content="ok")]})
        runner = AgentRunner(agent, base_config)
        runner._mw_hooks = hooks

        async for _ in runner.astream_events("first", thread_id="thread-a"):
            pass
        async for _ in runner.astream_events("second", thread_id="thread-a"):
            pass
        async for _ in runner.astream_events("other", thread_id="thread-b"):
            pass

        assert seen_counts == [("thread-a", 1), ("thread-a", 2), ("thread-b", 1)]
        thread_a_context_id = next(ctx_id for tid, ctx_id in seen_context_ids if tid == "thread-a")
        thread_b_context_id = next(ctx_id for tid, ctx_id in seen_context_ids if tid == "thread-b")
        assert thread_a_context_id != thread_b_context_id
        assert runner._hook_contexts["thread-a"]["counts"]["on_input"] == 2
        assert runner._hook_contexts["thread-b"]["counts"]["on_input"] == 1

    @pytest.mark.asyncio
    async def test_astream_events_replace_message_stops_after_rejected_interrupt(
        self, base_config: AgentConfig
    ) -> None:
        async def fake_astream(*args, **kwargs):
            # A real approval interrupt is always preceded by the tool-call
            # message in the stream (which populates the runner's pending map);
            # mirror that so the post-stream interrupt check runs.
            yield AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "interrupt-1",
                        "name": "task",
                        "args": {"description": "Fetch a page", "subagent_type": "BrowserAgent"},
                    }
                ],
            )

        interrupt = MagicMock(
            id="interrupt-1",
            value={
                "action_requests": [
                    {
                        "name": "task",
                        "args": {"description": "Fetch a page", "subagent_type": "BrowserAgent"},
                    }
                ]
            },
        )
        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(
            side_effect=[MagicMock(interrupts=[interrupt]), MagicMock(interrupts=[])]
        )
        agent.ainvoke = AsyncMock(return_value={"messages": []})
        runner = AgentRunner(agent, base_config)

        async def approval_handler(actions):
            return [{"type": "replace_message"} for _ in actions]

        events = []
        async for event in runner.astream_events(
            "Describe this page",
            thread_id=str(uuid.uuid4()),
            approval_handler=approval_handler,
        ):
            events.append(event)

        assert events == [
            (
                "approval",
                {
                    "actions": [
                        {
                            "interrupt_id": "interrupt-1",
                            "name": "task",
                            "args": {
                                "description": "Fetch a page",
                                "subagent_type": "BrowserAgent",
                            },
                            "risk": "medium",
                        }
                    ]
                },
            ),
            (
                "tool_result",
                {
                    "id": "interrupt-1",
                    "name": "task",
                    "content": "Blocked by user approval policy.",
                },
            ),
        ]
        agent.ainvoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_astream_events_auto_mode_approves_every_interrupt(
        self, base_config: AgentConfig
    ) -> None:
        """Auto session mode = "act freely": every interrupted tool is approved
        and no ("approval", ...) card surfaces, even with no approval_handler.

        Regression for the silent-reject bug where Auto mode passed
        approval_handler=None and the runner rejected medium/high-risk tools.
        """
        from langgraph.types import Command

        calls = {"n": 0}
        resume_payloads: list = []

        async def fake_astream(payload, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                # Stream the tool call that triggers the interrupt (production
                # always emits this before pausing for approval).
                yield AIMessage(
                    content="",
                    tool_calls=[
                        {"id": "interrupt-1", "name": "write_file", "args": {"file_path": "x"}}
                    ],
                )
                return
            if isinstance(payload, Command):
                resume_payloads.append(payload.resume)
            yield AIMessage(content="done")

        interrupt = MagicMock(
            id="interrupt-1",
            value={"action_requests": [{"name": "write_file", "args": {"file_path": "x"}}]},
        )
        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(
            side_effect=[MagicMock(interrupts=[interrupt]), MagicMock(interrupts=[])]
        )
        agent.ainvoke = AsyncMock(return_value={"messages": []})
        runner = AgentRunner(agent, base_config)

        events = []
        async for event in runner.astream_events(
            "write the file",
            thread_id=str(uuid.uuid4()),
            session_mode="auto",
        ):
            events.append(event)

        kinds = [k for k, _ in events]
        assert "approval" not in kinds  # no card surfaced in Auto
        assert not any(
            k == "tool_result" and "Blocked by user approval policy" in str(p.get("content", ""))
            for k, p in events
        )
        assert resume_payloads and resume_payloads[0]["decisions"][0]["type"] == "approve"

    @pytest.mark.asyncio
    async def test_astream_events_manual_mode_surfaces_card_and_blocks_on_reject(
        self, base_config: AgentConfig
    ) -> None:
        """Manual mode WITH a handler surfaces the approval card and blocks when
        the handler rejects (the realistic dashboard flow)."""
        from langgraph.types import Command

        async def fake_astream(payload, *args, **kwargs):
            # First pass streams the tool call (populating pending so the
            # interrupt is detected); the resume pass yields nothing.
            if not isinstance(payload, Command):
                yield AIMessage(
                    content="",
                    tool_calls=[
                        {"id": "interrupt-1", "name": "write_file", "args": {"file_path": "x"}}
                    ],
                )

        interrupt = MagicMock(
            id="interrupt-1",
            value={"action_requests": [{"name": "write_file", "args": {"file_path": "x"}}]},
        )
        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(
            side_effect=[MagicMock(interrupts=[interrupt]), MagicMock(interrupts=[])]
        )
        agent.ainvoke = AsyncMock(return_value={"messages": []})
        runner = AgentRunner(agent, base_config)

        async def reject_handler(actions):
            return [{"type": "reject"} for _ in actions]

        events = []
        async for event in runner.astream_events(
            "write the file",
            thread_id=str(uuid.uuid4()),
            session_mode="manual",
            approval_handler=reject_handler,
        ):
            events.append(event)

        kinds = [k for k, _ in events]
        assert "approval" in kinds
        assert any(
            k == "tool_result" and "Blocked by user approval policy" in str(p.get("content", ""))
            for k, p in events
        )

    @pytest.mark.asyncio
    async def test_astream_events_scopes_resume_per_interrupt_id(
        self, base_config: AgentConfig
    ) -> None:
        """Two parked branches — parallel subagents each pausing on their own
        gated tool — must resume with a {interrupt_id: value} map.

        Regression: a flat {"decisions": [...]} made LangGraph refuse the resume
        with "When there are multiple pending interrupts, you must specify the
        interrupt id when resuming", killing the turn with an API error.
        """
        from langgraph.types import Command

        id_a = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
        id_b = "0f9e8d7c6b5a49382716f5e4d3c2b1a0"
        resume_payloads: list = []

        async def fake_astream(payload, *args, **kwargs):
            if isinstance(payload, Command):
                resume_payloads.append(payload.resume)
                yield AIMessage(content="resumed")
                return
            yield AIMessage(
                content="",
                tool_calls=[
                    {"id": "call-a", "name": "write_file", "args": {"file_path": "a"}},
                    {"id": "call-b", "name": "write_file", "args": {"file_path": "b"}},
                ],
            )

        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(
            side_effect=[
                MagicMock(
                    interrupts=[
                        MagicMock(
                            id=id_a,
                            value={
                                "action_requests": [
                                    {"name": "write_file", "args": {"file_path": "a"}}
                                ]
                            },
                        ),
                        MagicMock(
                            id=id_b,
                            value={
                                "action_requests": [
                                    {"name": "write_file", "args": {"file_path": "b"}}
                                ]
                            },
                        ),
                    ]
                ),
                MagicMock(interrupts=[]),
            ]
        )
        agent.ainvoke = AsyncMock(return_value={"messages": []})
        runner = AgentRunner(agent, base_config)

        async for _ in runner.astream_events(
            "write both files", thread_id=str(uuid.uuid4()), session_mode="auto"
        ):
            pass

        # Each interrupt carries only its OWN decisions — the middleware raises
        # unless the count matches that interrupt's own action_requests.
        assert resume_payloads == [
            {
                id_a: {"decisions": [{"type": "approve"}]},
                id_b: {"decisions": [{"type": "approve"}]},
            }
        ]

    @pytest.mark.asyncio
    async def test_astream_events_matches_each_decision_to_its_own_action(
        self, base_config: AgentConfig
    ) -> None:
        """Parallel tool calls in ONE AI message are batched into one interrupt
        carrying an action_request each, so decisions apply per action.

        Regression: decisions were keyed by interrupt id alone, so every action
        of an interrupt inherited whichever decision was written last —
        approving the first call and rejecting the second rejected both.
        """
        from langgraph.types import Command

        resume_payloads: list = []

        async def fake_astream(payload, *args, **kwargs):
            if isinstance(payload, Command):
                resume_payloads.append(payload.resume)
                return
            yield AIMessage(
                content="",
                tool_calls=[
                    {"id": "call-a", "name": "write_file", "args": {"file_path": "a"}},
                    {"id": "call-b", "name": "write_file", "args": {"file_path": "b"}},
                ],
            )

        interrupt = MagicMock(
            id="a1b2c3d4e5f60718293a4b5c6d7e8f90",
            value={
                "action_requests": [
                    {"name": "write_file", "args": {"file_path": "a"}},
                    {"name": "write_file", "args": {"file_path": "b"}},
                ]
            },
        )
        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(
            side_effect=[MagicMock(interrupts=[interrupt]), MagicMock(interrupts=[])]
        )
        agent.ainvoke = AsyncMock(return_value={"messages": []})
        runner = AgentRunner(agent, base_config)

        async def approve_then_reject(actions):
            return [{"type": "approve"}, {"type": "reject"}]

        events = []
        async for event in runner.astream_events(
            "write both files",
            thread_id=str(uuid.uuid4()),
            session_mode="manual",
            approval_handler=approve_then_reject,
        ):
            events.append(event)

        # One pending interrupt → the flat shape, carrying the decisions in the
        # order the human gave them rather than the last one twice.
        assert resume_payloads == [{"decisions": [{"type": "approve"}, {"type": "reject"}]}]
        blocked = [
            payload
            for kind, payload in events
            if kind == "tool_result"
            and "Blocked by user approval policy" in str(payload.get("content", ""))
        ]
        assert len(blocked) == 1  # only the rejected call is blocked

    @pytest.mark.asyncio
    async def test_astream_events_rejecting_manual_keeps_interleaved_auto_approved(
        self, base_config: AgentConfig
    ) -> None:
        """An auto-approved action and a manually-rejected one in the SAME
        interrupt keep their own decisions, whatever order they interleave in.

        Also pins the fail-closed posture: the manual slot starts as reject and
        only the handler's answer (applied at its own index) can approve it.
        """
        from langgraph.types import Command

        resume_payloads: list = []

        async def fake_astream(payload, *args, **kwargs):
            if isinstance(payload, Command):
                resume_payloads.append(payload.resume)
                return
            yield AIMessage(
                content="",
                tool_calls=[
                    {"id": "call-a", "name": "user_profile_save", "args": {}},
                    {"id": "call-b", "name": "write_file", "args": {"file_path": "x"}},
                ],
            )

        interrupt = MagicMock(
            id="a1b2c3d4e5f60718293a4b5c6d7e8f90",
            value={
                "action_requests": [
                    {"name": "user_profile_save", "args": {}},
                    {"name": "write_file", "args": {"file_path": "x"}},
                ]
            },
        )
        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(
            side_effect=[MagicMock(interrupts=[interrupt]), MagicMock(interrupts=[])]
        )
        agent.ainvoke = AsyncMock(return_value={"messages": []})
        runner = AgentRunner(agent, base_config)

        seen_by_handler: list = []

        async def reject_handler(actions):
            seen_by_handler.extend(a.get("name") for a in actions)
            return [{"type": "reject"} for _ in actions]

        events = []
        async for event in runner.astream_events(
            "save the profile and write the file",
            thread_id=str(uuid.uuid4()),
            session_mode="manual",
            approval_handler=reject_handler,
        ):
            events.append(event)

        # Only the manual action reached the handler; the auto one stayed approved.
        assert seen_by_handler == ["write_file"]
        assert resume_payloads == [{"decisions": [{"type": "approve"}, {"type": "reject"}]}]
        blocked = [
            payload
            for kind, payload in events
            if kind == "tool_result"
            and "Blocked by user approval policy" in str(payload.get("content", ""))
        ]
        assert len(blocked) == 1

    @pytest.mark.asyncio
    async def test_astream_runs_interrupt_loop_instead_of_truncating(
        self, base_config: AgentConfig
    ) -> None:
        """Regression for H3: astream() used to stream the raw graph with no
        interrupt handling, silently truncating on an interrupted tool. It now
        delegates to astream_events and runs the full resume loop."""
        from langgraph.types import Command

        async def fake_astream(payload, *args, **kwargs):
            if isinstance(payload, Command):
                yield AIMessage(content="resumed-text")
            else:
                # The initial pass streams the interrupting tool call.
                yield AIMessage(
                    content="",
                    tool_calls=[{"id": "i1", "name": "write_file", "args": {}}],
                )

        interrupt = MagicMock(
            id="i1",
            value={"action_requests": [{"name": "write_file", "args": {}}]},
        )
        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(
            side_effect=[MagicMock(interrupts=[interrupt]), MagicMock(interrupts=[])]
        )
        agent.ainvoke = AsyncMock(return_value={"messages": []})
        runner = AgentRunner(agent, base_config)

        chunks = []
        async for chunk in runner.astream(
            "do it", thread_id=str(uuid.uuid4()), session_mode="auto"
        ):
            chunks.append(chunk)

        assert "resumed-text" in "".join(chunks)
        assert agent.aget_state.await_count == 2  # the interrupt loop ran

    @pytest.mark.asyncio
    async def test_astream_events_recovers_after_mutating_tool_error(
        self, base_config: AgentConfig
    ) -> None:
        """A single recoverable tool error must NOT abort — the model's follow-up
        message (its recovery) should stream through normally."""

        async def fake_astream(*args, **kwargs):
            yield AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "write_file",
                        "args": {"path": "notes/test.txt", "content": "hello"},
                    }
                ],
            )
            yield ToolMessage(
                content="Error: parent directory 'notes' does not exist.",
                tool_call_id="call-1",
                name="write_file",
                status="error",
            )
            yield AIMessage(content="I'll create the directory first, then write the file.")

        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock(return_value={"messages": []})
        runner = AgentRunner(agent, base_config)

        thread_id = str(uuid.uuid4())
        texts = []
        async for kind, payload in runner.astream_events(
            "Write notes/test.txt with hello",
            thread_id=thread_id,
        ):
            if kind == "text":
                texts.append(payload)

        joined = "".join(texts)
        # The model's recovery message streamed through; no canned abort text.
        assert "I'll create the directory first" in joined
        assert "I couldn't complete that action" not in joined
        assert "I tried that" not in joined

        session = runner.load_session(thread_id)
        assert any(
            "I'll create the directory first" in entry["content"]
            for entry in session
            if entry["role"] == "assistant"
        )

    @pytest.mark.asyncio
    async def test_astream_events_recovers_after_shell_command_not_found(
        self, base_config: AgentConfig
    ) -> None:
        """The screenshot scenario: `which claude` fails, the agent must continue
        (e.g. proceed to install) rather than stop at the first failure."""

        async def fake_astream(*args, **kwargs):
            yield AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "execute", "args": {"command": "which claude"}}],
            )
            yield ToolMessage(
                content="Error: bash: claude: command not found",
                tool_call_id="c1",
                name="execute",
                status="error",
            )
            yield AIMessage(content="claude isn't installed — installing it via npm now.")

        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock(return_value={"messages": []})
        runner = AgentRunner(agent, base_config)

        texts = [
            payload
            async for kind, payload in runner.astream_events(
                "install the claude-code CLI", thread_id=str(uuid.uuid4())
            )
            if kind == "text"
        ]
        joined = "".join(texts)
        assert "installing it via npm" in joined
        assert "I couldn't complete that action" not in joined
        assert "I tried that" not in joined

    @pytest.mark.asyncio
    async def test_astream_events_circuit_breaker_aborts_after_limit(
        self, base_config: AgentConfig
    ) -> None:
        """The identical failing call repeated up to the limit trips the breaker."""
        base_config = base_config.model_copy(update={"tool_error_retry_limit": 3})

        def _fail(i):
            return [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"id": f"c{i}", "name": "execute", "args": {"command": "claude --version"}}
                    ],
                ),
                ToolMessage(
                    content="Error: bash: claude: command not found",
                    tool_call_id=f"c{i}",
                    name="execute",
                    status="error",
                ),
            ]

        async def fake_astream(*args, **kwargs):
            for i in range(3):
                for msg in _fail(i):
                    yield msg
            yield AIMessage(content="It worked now!")  # must be suppressed after the breaker trips

        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock(return_value={"messages": []})
        runner = AgentRunner(agent, base_config)

        events = [
            event
            async for event in runner.astream_events(
                "run claude --version", thread_id=str(uuid.uuid4())
            )
        ]
        texts = [p for k, p in events if k == "text"]
        results = [p for k, p in events if k == "tool_result"]
        joined = "".join(texts)
        assert "I tried that 3 times" in joined
        assert "It worked now!" not in joined
        assert len(results) == 3  # all three failures surfaced before stopping

    @pytest.mark.asyncio
    async def test_astream_events_circuit_breaker_disabled_when_limit_zero(
        self, base_config: AgentConfig
    ) -> None:
        """tool_error_retry_limit=0 disables the breaker — identical failures never abort."""
        base_config = base_config.model_copy(update={"tool_error_retry_limit": 0})

        async def fake_astream(*args, **kwargs):
            for i in range(4):
                yield AIMessage(
                    content="",
                    tool_calls=[{"id": f"c{i}", "name": "execute", "args": {"command": "false"}}],
                )
                yield ToolMessage(
                    content="Error: command failed",
                    tool_call_id=f"c{i}",
                    name="execute",
                    status="error",
                )
            yield AIMessage(content="Giving a real answer now.")

        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock(return_value={"messages": []})
        runner = AgentRunner(agent, base_config)

        texts = [
            payload
            async for kind, payload in runner.astream_events(
                "run false repeatedly", thread_id=str(uuid.uuid4())
            )
            if kind == "text"
        ]
        joined = "".join(texts)
        assert "Giving a real answer now." in joined
        assert "I tried that" not in joined

    @pytest.mark.asyncio
    async def test_astream_events_circuit_breaker_trips_on_guarded_tool_error(
        self, base_config: AgentConfig
    ) -> None:
        """A guarded tool-boundary failure carries status='success' and the ⚠️ marker
        (it is a normal tool message). The breaker must still recognise it, so an
        always-failing tool trips the limit instead of looping forever."""
        from olano.tools._error_boundary import TOOL_ERROR_MARKER

        base_config = base_config.model_copy(update={"tool_error_retry_limit": 2})
        boundary_content = (
            f"{TOOL_ERROR_MARKER}monitor_seen_add' failed — FileNotFoundError: boom\n"
            "This is recoverable, not a crash. The target wasn't found."
        )

        def _fail(i):
            return [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"id": f"c{i}", "name": "monitor_seen_add", "args": {"namespace": "x"}}
                    ],
                ),
                ToolMessage(
                    content=boundary_content,
                    tool_call_id=f"c{i}",
                    name="monitor_seen_add",
                    status="success",  # boundary results look successful — marker is the signal
                ),
            ]

        async def fake_astream(*args, **kwargs):
            for i in range(2):
                for msg in _fail(i):
                    yield msg
            yield AIMessage(content="recovered!")  # must be suppressed after the breaker trips

        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock(return_value={"messages": []})
        runner = AgentRunner(agent, base_config)

        collected = [
            p
            async for k, p in runner.astream_events("go", thread_id=str(uuid.uuid4()))
            if k == "text"
        ]
        texts = "".join(collected)
        assert "I tried that 2 times" in texts
        assert "recovered!" not in texts

    @pytest.mark.asyncio
    async def test_astream_events_suppresses_nested_task_while_peer_call_pending(
        self, base_config: AgentConfig
    ) -> None:
        """Kevin's internal task call must not render in Aurelius' chat stream."""

        async def fake_astream(*args, **kwargs):
            yield AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "peer-1",
                        "name": "talk_to_agent",
                        "args": {"agent_name": "kevin", "message": "price of QCOM"},
                    }
                ],
            )
            yield AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "task-1",
                        "name": "task",
                        "args": {
                            "subagent_type": "MarketAgent",
                            "description": "Fetch QCOM",
                        },
                    }
                ],
            )
            yield ToolMessage(content="QCOM: $228.90", tool_call_id="task-1", name="task")
            yield ToolMessage(
                content="QCOM: $228.90",
                tool_call_id="peer-1",
                name="talk_to_agent",
            )

        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock(return_value={"messages": []})
        runner = AgentRunner(agent, base_config)

        events = []
        async for event in runner.astream_events("ask Kevin price of QCOM", thread_id="thread-a"):
            events.append(event)

        tool_calls = [payload for kind, payload in events if kind == "tool_call"]
        tool_results = [payload for kind, payload in events if kind == "tool_result"]
        assert [(ev["name"], ev["id"]) for ev in tool_calls] == [("talk_to_agent", "peer-1")]
        assert [(ev["name"], ev["id"]) for ev in tool_results] == [("talk_to_agent", "peer-1")]
        assert not any(ev["name"] == "task" for ev in tool_calls + tool_results)

    async def test_astream_events_reroutes_peer_text_as_subagent_text(
        self, base_config: AgentConfig
    ) -> None:
        """Callee text leaking into the stream mid talk_to_agent must not splice
        into the caller's reply — it is re-tagged as attributed subagent_text."""

        async def fake_astream(*args, **kwargs):
            yield AIMessage(content="Let me ask Kevin. ")
            yield AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "peer-1",
                        "name": "talk_to_agent",
                        "args": {"agent_name": "kevin", "message": "price of QCOM"},
                    }
                ],
            )
            # Kevin's nested run streams through the caller's callbacks with an
            # empty namespace — indistinguishable from caller text without the guard.
            yield AIMessageChunk(content="QCOM is trading at $228.90 right now.")
            yield ToolMessage(
                content="QCOM: $228.90",
                tool_call_id="peer-1",
                name="talk_to_agent",
            )
            yield AIMessage(content="Kevin says QCOM trades at $228.90.")

        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        runner = AgentRunner(agent, base_config)

        events = []
        async for event in runner.astream_events("ask Kevin price of QCOM", thread_id="thread-b"):
            events.append(event)

        answer = "".join(p for k, p in events if k == "text")
        assert "Let me ask Kevin." in answer
        assert "Kevin says QCOM trades at $228.90." in answer
        # the callee's leaked stream is NOT part of the caller's own text…
        assert "trading at $228.90 right now" not in answer
        # …it surfaces as attributed subagent text instead.
        sub = [p for k, p in events if k == "subagent_text"]
        assert len(sub) == 1
        assert sub[0]["agents"] == ["kevin"]
        assert "trading at $228.90 right now" in sub[0]["text"]

    @pytest.mark.asyncio
    async def test_astream_events_drops_summarization_middleware_output(
        self, base_config: AgentConfig
    ) -> None:
        """deepagents' SummarizationMiddleware compacts long threads by calling
        the model with LangChain's DEFAULT_SUMMARY_PROMPT ("## SESSION INTENT /
        ## SUMMARY / ## ARTIFACTS / ## NEXT STEPS"). That call runs in the root
        namespace and its tokens stream as ordinary AIMessageChunks marked only
        by lc_source="summarization" metadata — and, since summarization runs
        before the answer, they arrive first. Unfiltered they get spliced ahead
        of the real reply and shipped to the user (the comodin Telegram leak).
        The runner must drop them from the text stream, the transcript, and the
        channel renderer."""
        from olano.platforms.verbosity import render_events

        summary = (
            "## SESSION INTENT\nAtender a Andrés en español.\n"
            "## SUMMARY\n...\n## ARTIFACTS\n...\n## NEXT STEPS\nBuscar pastillas."
        )
        reply = "Para Yamaha de 250 cc, encontré esta opción."

        async def fake_astream(*args, **kwargs):
            # Real runtime shape under stream_mode=["messages"] + subgraphs=True:
            # (namespace, stream_mode, (message, metadata)). Empty namespace =>
            # root/main agent, so only the lc_source marker separates the
            # summarizer's output from the agent's own answer.
            yield (
                (),
                "messages",
                (AIMessageChunk(content=summary), {"lc_source": "summarization"}),
            )
            yield ((), "messages", (AIMessageChunk(content=reply), {}))

        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock()
        runner = AgentRunner(agent, base_config)

        events = []
        async for event in runner.astream_events("pastillas Yamaha", thread_id="comodin-leak"):
            events.append(event)

        # The summary never surfaces as reply text…
        answer = "".join(p for k, p in events if k == "text")
        assert answer == reply
        assert not any(k == "text" and "SESSION INTENT" in p for k, p in events)

        # …nor is it recorded in the assistant transcript…
        last = runner.load_session("comodin-leak")[-1]
        assert last["role"] == "assistant"
        assert last["content"] == reply

        # …nor does the channel renderer (server.py -> verbosity.render_events)
        # join it into the outbound message.
        rendered = render_events(events, "normal")
        assert rendered == reply
        assert "SESSION INTENT" not in rendered

    @pytest.mark.asyncio
    async def test_astream_events_usage_frame_context_is_last_call_not_sum(
        self, base_config: AgentConfig
    ) -> None:
        """A tool-heavy turn makes several model sub-calls; each reports the full
        prompt for THAT call. `input_tokens` must SUM them (billing), but
        `context_tokens` (the live window fill) must be just the LAST call's prompt
        — otherwise the dashboard bar shows totals far past the context window."""

        async def fake_astream(*args, **kwargs):
            # Three sub-calls in one turn; context grows monotonically as tool
            # results accumulate (90k → 110k → 120k against a 128k window).
            yield AIMessageChunk(
                content="step 1 ",
                usage_metadata={
                    "input_tokens": 90_000,
                    "output_tokens": 500,
                    "total_tokens": 90_500,
                },
            )
            yield AIMessageChunk(
                content="step 2 ",
                usage_metadata={
                    "input_tokens": 110_000,
                    "output_tokens": 400,
                    "total_tokens": 110_400,
                },
            )
            yield AIMessageChunk(
                content="done",
                usage_metadata={
                    "input_tokens": 120_000,
                    "output_tokens": 300,
                    "total_tokens": 120_300,
                },
            )

        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        runner = AgentRunner(agent, base_config)

        usage = [
            p
            for k, p in [ev async for ev in runner.astream_events("big task", thread_id="ctx-sum")]
            if k == "usage"
        ]

        assert len(usage) == 1
        frame = usage[0]
        # Billing total is the SUM across sub-calls (unchanged behavior).
        assert frame["input_tokens"] == 320_000
        assert frame["output_tokens"] == 1_200
        # The context-window fill is the LAST sub-call's prompt, not the sum, so it
        # stays within a real 128k window instead of reading ~320k.
        assert frame["context_tokens"] == 120_000

    @pytest.mark.asyncio
    async def test_astream_events_usage_context_falls_back_to_input_for_single_call(
        self, base_config: AgentConfig
    ) -> None:
        """A single-call turn: context_tokens equals input_tokens (they coincide)."""

        async def fake_astream(*args, **kwargs):
            yield AIMessageChunk(
                content="hi",
                usage_metadata={
                    "input_tokens": 4_200,
                    "output_tokens": 50,
                    "total_tokens": 4_250,
                },
            )

        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        runner = AgentRunner(agent, base_config)

        usage = [
            p
            for k, p in [ev async for ev in runner.astream_events("hi", thread_id="ctx-one")]
            if k == "usage"
        ]

        assert len(usage) == 1
        assert usage[0]["input_tokens"] == 4_200
        assert usage[0]["context_tokens"] == 4_200


class TestSummarization:
    def test_budget_notice_copy_is_human_readable(self, base_config: AgentConfig) -> None:
        base_config.iteration_budget = 10
        runner = AgentRunner(MagicMock(), base_config)

        notices_70 = runner._maybe_budget_notice(7, set())
        notices_90 = runner._maybe_budget_notice(9, {7})

        assert notices_70 == [
            "[I've used most of the allowed tool steps for this reply, so I'm going to start wrapping up.]"
        ]
        assert notices_90 == [
            "[I'm almost at the tool-step limit for this reply, so I'll finish up now.]"
        ]

    def test_extract_response_ignores_stale_assistant_turns(self, base_config: AgentConfig) -> None:
        runner = AgentRunner(MagicMock(), base_config)
        result = {
            "messages": [
                MagicMock(type="ai", content="Old Barcelona rewrite"),
                MagicMock(type="human", content="Tell me about my projects"),
            ]
        }

        assert runner._extract_response(result) == ""

    @pytest.mark.asyncio
    async def test_astream_events_does_not_replay_previous_assistant_response_when_fallback_has_no_new_ai(
        self, base_config: AgentConfig
    ) -> None:
        async def empty_astream(*args, **kwargs):
            if False:
                yield None

        agent = MagicMock()
        agent.astream = empty_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock(
            return_value={
                "messages": [
                    MagicMock(type="ai", content="Old Barcelona rewrite"),
                    MagicMock(type="human", content="Let me share my projects"),
                ]
            }
        )
        runner = AgentRunner(agent, base_config)

        thread_id = str(uuid.uuid4())
        events = []
        async for event in runner.astream_events("Let me share my projects", thread_id=thread_id):
            events.append(event)

        assert len(events) == 1
        kind, payload = events[0]
        assert kind == "usage"
        # `usd` (a derived cost field) may be present; assert on the token fields.
        assert {k: v for k, v in payload.items() if k != "usd"} == {
            "input_tokens": 2,
            "output_tokens": 2,
            "total_tokens": 2,
            # No streamed sub-call usage (fallback path) → context fill mirrors input.
            "context_tokens": 2,
            # One per message that reported usage — the same two the token
            # totals were summed from.
            "model_calls": 2,
        }
        session = runner.load_session(thread_id)
        assert session == [
            {
                "schema_version": 1,
                "ts": session[0]["ts"],
                "role": "user",
                "content": "Let me share my projects",
            }
        ]

    @pytest.mark.asyncio
    async def test_astream_events_does_not_auto_compact_on_high_token_turn(
        self, base_config: AgentConfig
    ) -> None:
        # Automatic context bounding is owned by deepagents' SummarizationMiddleware,
        # not by olano. A high-token turn must NOT fork a compacted successor
        # thread or emit a compacting_thread/thread_switch event.
        async def empty_astream(*args, **kwargs):
            if False:
                yield None

        agent = MagicMock()
        agent.astream = empty_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock(
            return_value={
                "messages": [
                    MagicMock(
                        type="ai",
                        content="Emily's in your profile now.",
                        usage_metadata={
                            "input_tokens": 90_000,
                            "output_tokens": 200,
                            "total_tokens": 90_200,
                        },
                    )
                ]
            }
        )
        runner = AgentRunner(agent, base_config)

        thread_id = str(uuid.uuid4())
        events = []
        async for event in runner.astream_events("my daughter name is Emily", thread_id=thread_id):
            events.append(event)

        assert events[0] == ("text", "Emily's in your profile now.")
        assert events[1][0] == "usage"
        # `usd` (a derived cost field) may be present; assert on the token fields.
        assert {k: v for k, v in events[1][1].items() if k != "usd"} == {
            "input_tokens": 90_000,
            "output_tokens": 200,
            "total_tokens": 90_200,
            # Single AI message → context fill equals its input tokens.
            "context_tokens": 90_000,
            # …and exactly one model call for the turn.
            "model_calls": 1,
        }
        kinds = [e[0] for e in events]
        assert "thread_switch" not in kinds
        assert not any(
            e[0] == "status" and isinstance(e[1], dict) and e[1].get("kind") == "compacting_thread"
            for e in events
        )


class TestCheckpointScoping:
    """Every checkpoint key must be bound to the agent, so two agents that the same
    human talks to (same human-derived thread_id) never share a LangGraph lineage.
    """

    def test_invoke_config_scopes_thread_by_agent(
        self, base_config: AgentConfig, mock_agent
    ) -> None:
        runner = AgentRunner(mock_agent, base_config)
        cfg = runner._invoke_config("5550001234")
        assert cfg["configurable"]["thread_id"] == "test-agent::5550001234"

    def test_invoke_config_idempotent(self, base_config: AgentConfig, mock_agent) -> None:
        runner = AgentRunner(mock_agent, base_config)
        once = runner._invoke_config("5550001234")["configurable"]["thread_id"]
        twice = runner._invoke_config(once)["configurable"]["thread_id"]
        assert once == twice == "test-agent::5550001234"

    def test_two_agents_same_thread_are_disjoint(
        self, base_config: AgentConfig, mock_agent
    ) -> None:
        marcus = AgentRunner(mock_agent, base_config.model_copy(update={"name": "marcus"}))
        cindy = AgentRunner(mock_agent, base_config.model_copy(update={"name": "cindy"}))
        # The reported bug: both keyed on Telegram chat id 5550001234.
        m = marcus._invoke_config("5550001234")["configurable"]["thread_id"]
        c = cindy._invoke_config("5550001234")["configurable"]["thread_id"]
        assert m == "marcus::5550001234"
        assert c == "cindy::5550001234"
        assert m != c

    async def test_ainvoke_passes_scoped_config_to_graph(
        self, base_config: AgentConfig, mock_agent
    ) -> None:
        seen: dict = {}

        async def _astream(payload, *args, **kwargs):
            seen["config"] = kwargs.get("config")
            yield (AIMessageChunk(content="ok"), {})

        mock_agent.astream = _astream
        runner = AgentRunner(mock_agent, base_config)
        await runner.ainvoke("Hello", thread_id="5550001234")
        cfg = seen["config"]
        assert cfg["configurable"]["thread_id"] == "test-agent::5550001234"


class TestPromptChangeNotice:
    """Per-thread prompt-update notices (olano.core.prompt_state wiring)."""

    @staticmethod
    def _bump_instructions(state):
        """A PromptState whose `instructions` moved relative to *state*."""
        from olano.core.prompt_state import PromptState

        fingerprint = dict(state.fingerprint)
        fingerprint["instructions"] = "deadbeef" * 8
        sections = dict(state.sections)
        sections["instructions"] = "NEW INSTRUCTIONS TEXT"
        return PromptState(sections=sections, fingerprint=fingerprint, built_at="test")

    @pytest.mark.asyncio
    async def test_first_turn_persists_nothing(self, runner: AgentRunner) -> None:
        """A brand-new thread (no state, no JSONL) must not grow a state file —
        fresh scheduled_/heartbeat_ runs would otherwise leave one per firing."""
        thread_id = str(uuid.uuid4())
        captured: list = []
        runner.agent.astream = _recording_astream(captured)
        await runner.ainvoke("Hello", thread_id=thread_id)
        assert not runner._thread_state_path(thread_id).exists()
        assert all(m["role"] != "system" for m in captured[-1]["messages"])

    @pytest.mark.asyncio
    async def test_second_turn_baselines_silently(self, runner: AgentRunner) -> None:
        thread_id = str(uuid.uuid4())
        captured: list = []
        runner.agent.astream = _recording_astream(captured)
        await runner.ainvoke("one", thread_id=thread_id)
        await runner.ainvoke("two", thread_id=thread_id)
        seen = runner.load_thread_state(thread_id).get("prompt_sections_seen")
        assert seen == runner.prompt_state().fingerprint
        assert all(m["role"] != "system" for m in captured[-1]["messages"])

    @pytest.mark.asyncio
    async def test_changed_sections_inject_notice_once_in_ainvoke(
        self, runner: AgentRunner
    ) -> None:
        thread_id = str(uuid.uuid4())
        captured: list = []
        runner.agent.astream = _recording_astream(captured)
        await runner.ainvoke("one", thread_id=thread_id)
        await runner.ainvoke("two", thread_id=thread_id)  # baseline written

        runner._prompt_state = self._bump_instructions(runner.prompt_state())
        await runner.ainvoke("three", thread_id=thread_id)

        msgs = captured[-1]["messages"]
        assert msgs[0]["role"] == "system"
        assert "[System update — your operating instructions changed]" in msgs[0]["content"]
        assert "NEW INSTRUCTIONS TEXT" in msgs[0]["content"]
        assert msgs[-1] == {"role": "user", "content": "three"}
        # Marker advanced + audit row in the JSONL.
        seen = runner.load_thread_state(thread_id)["prompt_sections_seen"]
        assert seen["instructions"] == "deadbeef" * 8
        session = runner.load_session(thread_id)
        assert any(e["role"] == "system" and "[System update" in e["content"] for e in session)

        # Exactly once: the next turn carries no further notice.
        await runner.ainvoke("four", thread_id=thread_id)
        assert all(m["role"] != "system" for m in captured[-1]["messages"])

    @pytest.mark.asyncio
    async def test_astream_orders_prompt_notice_before_mcp_notice(
        self, runner: AgentRunner
    ) -> None:
        thread_id = str(uuid.uuid4())
        await runner.ainvoke("one", thread_id=thread_id)
        await runner.ainvoke("two", thread_id=thread_id)  # baseline

        runner._prompt_state = self._bump_instructions(runner.prompt_state())
        runner.set_mcp_notice("[MCP status update]\nserver came online")
        captured: list = []
        runner.agent.astream = _recording_astream(captured)
        async for _ in runner.astream_events("three", thread_id=thread_id):
            pass

        msgs = captured[0]["messages"]
        assert "[System update" in msgs[0]["content"] and msgs[0]["role"] == "system"
        assert "[MCP status update]" in msgs[1]["content"] and msgs[1]["role"] == "system"
        assert msgs[2]["role"] == "user"

    @pytest.mark.asyncio
    async def test_internal_message_skips_notice_and_marker(self, runner: AgentRunner) -> None:
        thread_id = str(uuid.uuid4())
        await runner.ainvoke("one", thread_id=thread_id)
        await runner.ainvoke("two", thread_id=thread_id)  # baseline
        baseline = runner.load_thread_state(thread_id)["prompt_sections_seen"]

        runner._prompt_state = self._bump_instructions(runner.prompt_state())
        captured: list = []
        runner.agent.astream = _recording_astream(captured)
        async for _ in runner.astream_events(
            "internal directive", thread_id=thread_id, internal_message=True
        ):
            pass

        msgs = captured[0]["messages"]
        assert all(m["role"] != "system" for m in msgs)
        assert runner.load_thread_state(thread_id)["prompt_sections_seen"] == baseline

    @pytest.mark.asyncio
    async def test_notify_threads_off_advances_marker_silently(self, runner: AgentRunner) -> None:
        from olano.config.system_settings import PromptRefreshSettings

        thread_id = str(uuid.uuid4())
        captured: list = []
        runner.agent.astream = _recording_astream(captured)
        await runner.ainvoke("one", thread_id=thread_id)
        await runner.ainvoke("two", thread_id=thread_id)  # baseline

        runner._prompt_state = self._bump_instructions(runner.prompt_state())
        with patch(
            "olano.config.system_settings.load_prompt_refresh_settings",
            return_value=PromptRefreshSettings(notify_threads=False),
        ):
            await runner.ainvoke("three", thread_id=thread_id)

        assert all(m["role"] != "system" for m in captured[-1]["messages"])
        seen = runner.load_thread_state(thread_id)["prompt_sections_seen"]
        assert seen["instructions"] == "deadbeef" * 8
        session = runner.load_session(thread_id)
        assert not any(e["role"] == "system" for e in session)

    @pytest.mark.asyncio
    async def test_notice_failure_never_breaks_the_turn(self, runner: AgentRunner) -> None:
        thread_id = str(uuid.uuid4())
        runner.prompt_state = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        result = await runner.ainvoke("Hello", thread_id=thread_id)
        assert result

    @pytest.mark.asyncio
    async def test_fork_thread_carries_seen_marker(self, runner: AgentRunner) -> None:
        thread_id = str(uuid.uuid4())
        captured: list = []
        runner.agent.astream = _recording_astream(captured)
        await runner.ainvoke("one", thread_id=thread_id)
        await runner.ainvoke("two", thread_id=thread_id)  # baseline
        seen = runner.load_thread_state(thread_id)["prompt_sections_seen"]

        forked = await runner.fork_thread(thread_id)
        assert runner.load_thread_state(forked)["prompt_sections_seen"] == seen


class TestTextSegmentsAcrossToolBoundaries:
    """Text that resumes after a tool call is a new paragraph and a new segment.

    Every consumer concatenates the ``text`` chunks verbatim — the dashboard's
    ``m.body += text``, the messenger renderers, the persisted JSONL row — so
    without a separator "…are drawn." followed by "Found it —" read as one
    sentence. The runner now emits a blank line at that boundary and records
    the turn's ordered ``segments`` so the dashboard can interleave tool cards
    with the paragraphs they belong to.
    """

    @staticmethod
    def _agent(fake_astream):
        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock(return_value={"messages": []})
        return agent

    @staticmethod
    async def _collect(runner, thread_id):
        events = []
        async for kind, payload in runner.astream_events("go", thread_id=thread_id):
            events.append((kind, payload))
        return events

    @pytest.mark.asyncio
    async def test_text_resumed_after_a_tool_call_gets_a_paragraph_break(
        self, base_config: AgentConfig
    ) -> None:
        async def fake_astream(*args, **kwargs):
            yield AIMessage(
                content="Let me look at the script.",
                tool_calls=[{"id": "c1", "name": "read_file", "args": {"path": "build.py"}}],
            )
            yield ToolMessage(content="BG = gradient(...)", tool_call_id="c1", name="read_file")
            yield AIMessage(content="Found it — the gradient renders as three bands.")

        runner = AgentRunner(self._agent(fake_astream), base_config)
        thread_id = str(uuid.uuid4())
        events = await self._collect(runner, thread_id)

        texts = [p for k, p in events if k == "text"]
        assert "".join(texts) == (
            "Let me look at the script.\n\nFound it — the gradient renders as three bands."
        )
        # The separator is its own chunk, so live consumers get it for free.
        assert "\n\n" in texts

        rows = [e for e in runner.load_session(thread_id) if e["role"] == "assistant"]
        assert len(rows) == 1
        assert rows[0]["content"] == (
            "Let me look at the script.\n\nFound it — the gradient renders as three bands."
        )
        assert rows[0]["segments"] == [
            {"text": "Let me look at the script.", "after": None},
            {"text": "Found it — the gradient renders as three bands.", "after": "c1"},
        ]

    @pytest.mark.asyncio
    async def test_no_extra_blank_line_when_the_text_already_ends_with_a_newline(
        self, base_config: AgentConfig
    ) -> None:
        async def fake_astream(*args, **kwargs):
            yield AIMessage(
                content="Checking.\n",
                tool_calls=[{"id": "c1", "name": "read_file", "args": {"path": "x"}}],
            )
            yield ToolMessage(content="ok", tool_call_id="c1", name="read_file")
            yield AIMessage(content="Done.")

        runner = AgentRunner(self._agent(fake_astream), base_config)
        thread_id = str(uuid.uuid4())
        events = await self._collect(runner, thread_id)

        assert "".join(p for k, p in events if k == "text") == "Checking.\nDone."

    @pytest.mark.asyncio
    async def test_a_turn_that_opens_with_a_tool_call_has_no_leading_separator(
        self, base_config: AgentConfig
    ) -> None:
        async def fake_astream(*args, **kwargs):
            yield AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "read_file", "args": {"path": "x"}}],
            )
            yield ToolMessage(content="ok", tool_call_id="c1", name="read_file")
            yield AIMessage(content="Done.")

        runner = AgentRunner(self._agent(fake_astream), base_config)
        thread_id = str(uuid.uuid4())
        events = await self._collect(runner, thread_id)

        assert "".join(p for k, p in events if k == "text") == "Done."
        rows = [e for e in runner.load_session(thread_id) if e["role"] == "assistant"]
        # One segment only → the row keeps its historical shape (no `segments`).
        assert rows[0]["content"] == "Done."
        assert "segments" not in rows[0]

    @pytest.mark.asyncio
    async def test_a_turn_without_tools_persists_no_segments(
        self, base_config: AgentConfig
    ) -> None:
        async def fake_astream(*args, **kwargs):
            yield AIMessage(content="Hello ")
            yield AIMessage(content="there.")

        runner = AgentRunner(self._agent(fake_astream), base_config)
        thread_id = str(uuid.uuid4())
        events = await self._collect(runner, thread_id)

        assert "".join(p for k, p in events if k == "text") == "Hello there."
        rows = [e for e in runner.load_session(thread_id) if e["role"] == "assistant"]
        assert rows[0]["content"] == "Hello there."
        assert "segments" not in rows[0]


class TestMidTurnSteering:
    """core/steering.py through the runner: a steer accepted while the turn
    runs is reported as a ``("steer", …)`` event between the model calls and
    persisted as a ``user`` row (``steer: true``) with the reply split around
    it — the order a reload and a second device redraw."""

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        from olano.core.steering import REGISTRY

        REGISTRY.reset()
        yield
        REGISTRY.reset()

    @pytest.mark.asyncio
    async def test_steer_event_and_transcript_rows(self, base_config: AgentConfig) -> None:
        from olano.core.steering import REGISTRY

        thread = "steer-thread"
        agent_name = base_config.name

        async def fake_astream(*args, **kwargs):
            yield ((), "messages", (AIMessageChunk(content="first "), {}))
            # Between two model calls the graph's before_model hook drains
            # the steer the API accepted meanwhile (emulated inline here) and
            # the node's own `updates` chunk wakes the loop.
            accepted = REGISTRY.steer(
                agent_name, thread, "use blue", steer_id="steer0001", sender="Ana"
            )
            assert accepted is not None
            assert [m.id for m in REGISTRY.drain(agent_name, thread)] == ["steer0001"]
            yield ((), "updates", {"SteerMiddleware.before_model": {"messages": []}})
            yield ((), "messages", (AIMessageChunk(content="second"), {}))

        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock()
        runner = AgentRunner(agent, base_config)

        events = [ev async for ev in runner.astream_events("make a chart", thread_id=thread)]
        # The steer lands after the first call's text and before the next call's.
        ordered = [(k, p) for k, p in events if k in ("text", "steer")]
        assert ordered == [
            ("text", "first "),
            ("steer", {"id": "steer0001", "text": "use blue", "sender": "Ana"}),
            ("text", "second"),
        ]
        session = runner.load_session(thread)
        assert [(e["role"], e["content"]) for e in session] == [
            ("user", "make a chart"),
            ("assistant", "first "),
            ("user", "use blue"),
            ("assistant", "second"),
        ]
        steer_row = session[2]
        assert steer_row["steer"] is True
        assert steer_row["steer_id"] == "steer0001"
        assert steer_row["sender"] == "Ana"
        assert "steer" not in session[0]
        # The turn is closed: nothing left to steer.
        assert REGISTRY.active_turn(agent_name, thread) is None
        assert runner.steer(thread, "too late") is None

    @pytest.mark.asyncio
    async def test_undelivered_steer_leaves_no_trace(self, base_config: AgentConfig) -> None:
        """Accepted after the last model call: never drained, never reported,
        never written — the dashboard still holds it and sends it next."""
        from olano.core.steering import REGISTRY

        thread = "steer-late"
        agent_name = base_config.name

        async def fake_astream(*args, **kwargs):
            yield ((), "messages", (AIMessageChunk(content="all done"), {}))
            assert REGISTRY.steer(agent_name, thread, "one more thing") is not None

        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock()
        runner = AgentRunner(agent, base_config)

        events = [ev async for ev in runner.astream_events("hi", thread_id=thread)]
        assert not any(k == "steer" for k, _ in events)
        session = runner.load_session(thread)
        assert [(e["role"], e["content"]) for e in session] == [
            ("user", "hi"),
            ("assistant", "all done"),
        ]
        assert REGISTRY.active_turn(agent_name, thread) is None

    @pytest.mark.asyncio
    async def test_turn_id_and_internal_turns(self, base_config: AgentConfig) -> None:
        from olano.core.steering import REGISTRY

        seen: dict[str, object] = {}
        agent_name = base_config.name

        async def fake_astream(*args, **kwargs):
            seen["active"] = REGISTRY.active_turn(agent_name, "t-id")
            yield ((), "messages", (AIMessageChunk(content="ok"), {}))

        agent = MagicMock()
        agent.astream = fake_astream
        agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
        agent.ainvoke = AsyncMock()
        runner = AgentRunner(agent, base_config)

        # The API's turn id is what the registry files the turn under.
        async for _ in runner.astream_events("hi", thread_id="t-id", turn_id="turn-xyz"):
            pass
        assert seen["active"] == "turn-xyz"
        # No id from the caller: the runner mints one.
        async for _ in runner.astream_events("hi", thread_id="t-id"):
            pass
        assert isinstance(seen["active"], str) and seen["active"]
        # An internal turn (a compaction directive) is never steerable.
        async for _ in runner.astream_events("hi", thread_id="t-id", internal_message=True):
            pass
        assert seen["active"] is None

    def test_runner_steer_scans_input_like_a_normal_message(
        self, base_config: AgentConfig, monkeypatch
    ) -> None:
        from olano.core import runner as runner_mod
        from olano.core.steering import REGISTRY

        calls: list[str] = []

        def fake_scan(text: str, config: AgentConfig) -> str:
            calls.append(text)
            return text.upper()

        monkeypatch.setattr(runner_mod, "_scan_input", fake_scan)
        runner = AgentRunner(MagicMock(), base_config)
        REGISTRY.begin_turn(base_config.name, "t-scan", "turn-1")
        accepted = runner.steer("t-scan", "use blue", turn_id="turn-1", steer_id="abcdefgh")
        assert calls == ["use blue"]
        assert accepted is not None and accepted.text == "USE BLUE" and accepted.id == "abcdefgh"
        assert runner.steer("t-scan", "x", turn_id="turn-stale") is None
        assert runner.cancel_steer("t-scan", "abcdefgh") == "cancelled"
        assert runner.cancel_steer("t-scan", "abcdefgh") == "unknown"


class TestTurnModelStamp:
    """Every assistant row records the model that produced it, and a
    non-streaming turn can run on a per-turn override (scheduled tasks)."""

    @staticmethod
    def _rows(runner: AgentRunner, thread_id: str) -> list[dict]:
        import json

        path = runner._session_path(thread_id)
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    async def test_ainvoke_stamps_the_configured_model(self, runner: AgentRunner) -> None:
        thread_id = str(uuid.uuid4())
        await runner.ainvoke("Hello", thread_id=thread_id)
        assistant = [r for r in self._rows(runner, thread_id) if r["role"] == "assistant"]
        assert assistant and assistant[-1]["model"] == runner.config.model
        # No effort configured → no key, not an empty one.
        assert "reasoning_effort" not in assistant[-1]

    async def test_ainvoke_override_runs_stamps_and_bills_that_model(
        self, base_config: AgentConfig
    ) -> None:
        import asyncio
        from types import SimpleNamespace

        def make_agent(reply: str) -> MagicMock:
            # `ainvoke` is a wrapper over the stream path, so the fake graph
            # answers through `astream`; its one chunk carries the usage.
            agent = MagicMock()

            async def stream(*args, **kwargs):
                chunk = AIMessageChunk(content=reply, id=f"ai-{reply}")
                chunk.usage_metadata = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
                yield (chunk, {})

            agent.astream = stream
            agent.ainvoke = AsyncMock()
            agent.aget_state = AsyncMock(return_value=SimpleNamespace(interrupts=[], values={}))
            return agent

        shared = make_agent("from the configured model")
        pinned = make_agent("from the pinned model")
        runner = AgentRunner(shared, base_config)
        thread_id = str(uuid.uuid4())

        billed: list[str] = []

        async def settled(turns: int) -> None:
            # Recording runs off the turn's critical path (a background task
            # over a thread); give it a moment to land.
            for _ in range(200):
                if len(billed) >= turns:
                    return
                await asyncio.sleep(0.01)

        with (
            patch("olano.core.builder.build_agent_async", AsyncMock(return_value=pinned)),
            patch(
                "olano.core.runner._record_usage",
                side_effect=lambda cfg, meta, tid="": billed.append(cfg.model) and 0.0,
            ),
        ):
            reply = await runner.ainvoke("go", thread_id=thread_id, override_model="fake:fast")
            await settled(1)
            again = await runner.ainvoke("go again", thread_id=thread_id)
            await settled(2)

        assert reply == "from the pinned model"
        assert again == "from the configured model"
        # Usage attributed to the model that actually ran, per turn.
        assert billed == ["fake:fast", base_config.model]
        # The shared agent/config were never mutated by the override.
        assert runner.config.model == base_config.model
        assert runner.agent is shared
        rows = [r for r in self._rows(runner, thread_id) if r["role"] == "assistant"]
        assert [r["model"] for r in rows] == ["fake:fast", base_config.model]

    async def test_ainvoke_unbuildable_override_names_the_model(
        self, base_config: AgentConfig, mock_agent
    ) -> None:
        runner = AgentRunner(mock_agent, base_config)
        with (
            patch(
                "olano.core.builder.build_agent_async",
                AsyncMock(side_effect=RuntimeError("no API key")),
            ),
            pytest.raises(AgentError, match="Could not load model 'ghost:x'"),
        ):
            await runner.ainvoke("go", thread_id=str(uuid.uuid4()), override_model="ghost:x")
