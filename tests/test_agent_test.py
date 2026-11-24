"""``olano_test_agent``: AgentFather running a test turn of another agent.

The gates are tested as units; the behaviour end to end on the offline
``fake:`` model, through the real ``build_agent_async → AgentRunner`` path,
because what the tool promises (a real turn, its tool calls, a reusable thread,
a prompt override that reaches the model) only exists there.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from olano.config.home import get_olano_paths
from olano.config.loader import load_agent_config
from olano.core.thread_labels import (
    AGENT_TEST_THREAD_PREFIX,
    ORIGIN_AUTONOMOUS,
    classify_thread,
    is_system_thread,
)
from olano.models.fake import FakeChatModel
from olano.security.request_context import (
    AGENT_ADMIN_KEY,
    ORIGIN_KEY,
    make_origin,
    push_request_context,
)
from olano.tools import _agent_test, olano_admin, self_config_tools, talk_to
from olano.tools.talk_to import current_agent

TARGET = "tester"
FATHER = "agentfather"
PREFIX = AGENT_TEST_THREAD_PREFIX


def seed_agent(name: str = TARGET, **fields) -> None:
    agent_dir = get_olano_paths().agents_dir / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    body = {
        "name": name,
        "model": "fake:instant",
        "system_prompt": "CONFIGURED-PROMPT-MARKER",
        "memory": {"markdown": True, "rag": False, "self_learning": False},
        "backend": {"type": "filesystem"},
        **fields,
    }
    (agent_dir / "agent.json5").write_text(json.dumps(body))


def set_script(monkeypatch, steps: list[dict]) -> None:
    """Script the fake model: one entry per model call, in order.

    A model reads its script when it is BUILT, so this has to come before the
    runner (or test copy) that should follow it is built.
    """
    monkeypatch.setenv("LIONCLAW_FAKE_MODEL_SCRIPT", json.dumps(steps))


@pytest.fixture
def as_father():
    token = current_agent.set(FATHER)
    yield FATHER
    current_agent.reset(token)


@pytest.fixture
def admin_turn():
    """An administrator chatting with AgentFather on the dashboard."""
    with push_request_context(source="api", channel="api", metadata={AGENT_ADMIN_KEY: True}):
        yield


@pytest.fixture
def prompts_seen(monkeypatch):
    """Every system prompt the fake model was called with, as plain text."""
    seen: list[str] = []
    original = FakeChatModel._next_step

    def spy(self, messages):
        if messages and isinstance(messages[0], SystemMessage):
            seen.append(_agent_test._message_text(messages[0]))
        return original(self, messages)

    monkeypatch.setattr(FakeChatModel, "_next_step", spy)
    return seen


@pytest.fixture
def runtime(monkeypatch):
    """A runtime registry the tool resolves running agents from."""
    rt = SimpleNamespace(_runners={})
    monkeypatch.setattr(talk_to, "_RUNTIME", rt)
    return rt


@pytest.fixture
def start_agent(monkeypatch, runtime):
    """Build an agent and register it as running, its model following ``steps``."""
    from olano.core.builder import build_agent_async
    from olano.core.runner import AgentRunner

    async def _start(steps: list[dict], name: str = TARGET, **fields) -> AgentRunner:
        seed_agent(name, **fields)
        set_script(monkeypatch, steps)
        config = load_agent_config(name)
        runner = AgentRunner(await build_agent_async(config), config)
        runtime._runners[name] = runner
        return runner

    return _start


async def test_agent(**args) -> dict:
    args.setdefault("agent", TARGET)
    return json.loads(await olano_admin.olano_test_agent.ainvoke(args))


test_agent.__test__ = False  # a helper, not a test


# ── thread ids ───────────────────────────────────────────────────────────────


class TestThreadIds:
    def test_omitted_means_a_fresh_test_thread(self):
        tid, is_new, err = _agent_test.normalize_thread_id(None)
        assert err is None and is_new and tid.startswith(PREFIX)
        assert _agent_test.normalize_thread_id("")[0] != tid

    def test_a_returned_id_is_reused_verbatim(self):
        assert _agent_test.normalize_thread_id("agenttest::abc123") == (
            "agenttest::abc123",
            False,
            None,
        )

    def test_any_other_id_is_put_in_the_test_namespace(self):
        """A test can never be pointed at a real conversation: an owner's chat
        id, or a customer's WhatsApp thread, lands in its own test thread."""
        for raw in ("my-test", "agent:main:whatsapp:dm:123", "3f2a9c1e-0000-4000-8000-1234"):
            tid, _, err = _agent_test.normalize_thread_id(raw)
            assert err is None and tid == PREFIX + raw

    @pytest.mark.parametrize("raw", ["../escape", "a/b", "x" * 97, "-leading", "sp ace"])
    def test_unusable_ids_are_refused(self, raw):
        tid, _, err = _agent_test.normalize_thread_id(raw)
        assert tid == "" and err

    def test_test_threads_are_system_threads(self):
        tid = PREFIX + "abc"
        assert is_system_thread(tid)
        assert classify_thread(tid, {"role": "user", "content": "hi"})[0] == ORIGIN_AUTONOMOUS
        assert not talk_to._is_user_thread(tid)
        from olano.analytics.journal import _is_user_thread_stem

        assert not _is_user_thread_stem(tid)


# ── the tool-call trace ──────────────────────────────────────────────────────


class TestTrace:
    def test_only_the_last_turn_is_reported_with_results_paired(self):
        messages = [
            HumanMessage(content="first"),
            AIMessage(content="", tool_calls=[{"name": "old", "args": {}, "id": "c0"}]),
            ToolMessage(content="old result", tool_call_id="c0"),
            HumanMessage(content="second"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {"path": "/a"}, "id": "c1"},
                    {"name": "broken", "args": {}, "id": "c2"},
                ],
            ),
            ToolMessage(content="contents of a", tool_call_id="c1"),
            ToolMessage(content="boom", tool_call_id="c2", status="error"),
            AIMessage(content="done"),
        ]
        trace = _agent_test.trace_from_messages(messages)
        assert [c["tool"] for c in trace] == ["read_file", "broken"]
        assert json.loads(trace[0]["args"]) == {"path": "/a"}
        assert trace[0]["result"] == "contents of a"
        assert "status" not in trace[0]
        assert trace[1]["status"] == "error"

    def test_long_results_are_clipped(self):
        messages = [
            HumanMessage(content="go"),
            AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c"}]),
            ToolMessage(content="x" * 10_000, tool_call_id="c"),
        ]
        (call,) = _agent_test.trace_from_messages(messages)
        assert len(call["result"]) <= 1500


# ── who may run a test, and what the agent under test may do ─────────────────


class TestGate:
    @pytest.mark.parametrize(
        "source", ["peer", "scheduled", "cognition", "webhook", "mcp", "a2a", "agent_test"]
    )
    async def test_no_administrator_on_the_turn_is_refused(self, as_father, admin_turn, source):
        """Pushed on top of an admin's turn on purpose: context metadata merges,
        so a hop started from an admin's chat inherits AGENT_ADMIN_KEY=True."""
        with push_request_context(source=source):
            out = await test_agent(user_message="hello")
        assert "needs an administrator" in out["error"]

    async def test_a_chain_rooted_on_a_timer_is_refused(self, as_father, admin_turn):
        with push_request_context(metadata={ORIGIN_KEY: make_origin("scheduled", False)}):
            out = await test_agent(user_message="hello")
        assert "needs an administrator" in out["error"]

    async def test_a_member_on_a_multi_user_box_is_refused(self, as_father, monkeypatch):
        monkeypatch.setattr("olano.runtime.auth.multi_user_enabled", lambda: True)
        with push_request_context(source="api", channel="api", is_admin=False):
            out = await test_agent(user_message="hello")
        assert "needs an administrator" in out["error"]

    def test_the_agent_under_test_cannot_reconfigure_itself(self, admin_turn):
        """`_ownership_gate` refuses the test source before it reads the
        (inherited) owner flag."""
        with push_request_context(source="agent_test", metadata={AGENT_ADMIN_KEY: True}):
            refusal = self_config_tools._ownership_gate()
        assert refusal is not None
        assert "a human" in json.loads(refusal)["error"]

    def test_the_agent_under_test_cannot_publish_itself(self, admin_turn):
        from olano.tools._human_gate import admin_human_denial

        with push_request_context(source="agent_test"):
            assert admin_human_denial() is not None


class TestGrant:
    def test_it_is_an_owner_only_hitl_gated_admin_tool(self):
        from olano.security.hitl_policy import CATEGORY_TOOLS
        from olano.security.tool_metadata import get_tool_metadata

        meta = get_tool_metadata(olano_admin.olano_test_agent)
        assert meta.owner_only and meta.requires_hitl
        # Like olano_scheduler_run_now: another agent's full turn on a supplied
        # prompt is approved individually, never by a remembered rule.
        assert "olano_test_agent" in CATEGORY_TOOLS["cross_agent_exec"]
        assert "olano_test_agent" not in CATEGORY_TOOLS["write_external"]
        from olano.core.runner import _CROSS_AGENT_EXEC_TOOL_NAMES

        assert "olano_test_agent" in _CROSS_AGENT_EXEC_TOOL_NAMES

    def test_it_is_privileged_so_no_ordinary_agent_can_be_granted_it(self):
        from olano.admin.integrations import privileged_tool_names

        assert "olano_test_agent" in privileged_tool_names()

    def test_agentfather_ships_with_it_and_other_agents_do_not_get_it(self):
        from olano.config.agent_config import AgentConfig
        from olano.config.json5_fast import loads
        from olano.config.ops_defaults import resolve_effective_tool_names

        template = Path(__file__).parents[1] / "olano/archetypes/templates/agentfather.json5"
        assert "olano_test_agent" in loads(template.read_text())["tools"]
        plain = AgentConfig(name="t", model="fake:instant")
        effective = resolve_effective_tool_names(plain.tools, plain.disabled_tools, plain)
        assert "olano_test_agent" not in effective


class TestMigration:
    mig = importlib.import_module("olano.migrations.0026_agentfather_test_agent")

    def _write(self, home: Path, name: str, body: dict) -> Path:
        path = home / "agents" / name / "agent.json5"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body))
        return path

    def test_grants_the_tool_next_to_run_agent_command(self, tmp_path):
        path = self._write(
            tmp_path,
            "agentfather",
            {"name": "agentfather", "tools": ["olano_run_agent_command", "olano_start_agent"]},
        )
        self.mig.apply(tmp_path)
        tools = json.loads(path.read_text())["tools"]
        assert tools == ["olano_run_agent_command", "olano_test_agent", "olano_start_agent"]
        self.mig.apply(tmp_path)  # idempotent
        assert json.loads(path.read_text())["tools"] == tools

    def test_respects_an_opt_out_and_skips_other_agents(self, tmp_path):
        opted = self._write(
            tmp_path,
            "agentfather",
            {"name": "agentfather", "tools": ["x"], "disabled_tools": ["olano_test_agent"]},
        )
        other = self._write(tmp_path, "helper", {"name": "helper", "tools": ["x"]})
        self.mig.apply(tmp_path)
        assert json.loads(opted.read_text())["tools"] == ["x"]
        assert json.loads(other.read_text())["tools"] == ["x"]


# ── argument validation (before anything is built) ───────────────────────────


class TestValidation:
    @pytest.mark.parametrize(
        ("args", "needle"),
        [
            ({"user_message": "   "}, "user_message is required"),
            ({"user_message": "x" * 20_001}, "the limit is"),
            ({"user_message": "hi", "system_prompt": "  "}, "system_prompt is empty"),
            ({"user_message": "hi", "system_prompt_mode": "partial"}, "system_prompt_mode"),
            ({"user_message": "hi", "thread_id": "../x"}, "thread_id must be"),
        ],
    )
    async def test_bad_arguments_are_explained(self, args, needle):
        out = await _agent_test.run_agent_test(TARGET, caller=FATHER, **args)
        assert out["status"] == "error"
        assert needle in out["error"]

    async def test_an_unknown_agent_lists_the_real_ones(self, runtime):
        seed_agent("alpha")
        out = await _agent_test.run_agent_test("nobody", caller=FATHER, user_message="hi")
        assert "no agent named 'nobody'" in out["error"]
        assert "alpha" in out["error"]

    async def test_an_agent_cannot_test_itself(self, runtime):
        seed_agent(FATHER)
        out = await _agent_test.run_agent_test(FATHER, caller=FATHER, user_message="hi")
        assert "cannot test itself" in out["error"]

    async def test_a_busy_thread_is_refused(self, runtime):
        seed_agent()
        _agent_test._INFLIGHT.add((TARGET, PREFIX + "busy"))
        try:
            out = await _agent_test.run_agent_test(
                TARGET, caller=FATHER, user_message="hi", thread_id=PREFIX + "busy"
            )
        finally:
            _agent_test._INFLIGHT.discard((TARGET, PREFIX + "busy"))
        assert "already running" in out["error"]


# ── end to end, on the offline model ─────────────────────────────────────────


class TestEndToEnd:
    async def test_a_turn_returns_output_tool_calls_and_a_thread(
        self, as_father, admin_turn, start_agent
    ):
        workspace = str(get_olano_paths().agent_workspace_dir(TARGET))
        runner = await start_agent(
            [{"tool": "ls", "args": {"path": workspace}}, {"text": "I can see my files."}]
        )

        out = await test_agent(agent="TESTER", user_message="what is in your workspace?")

        assert out["status"] == "completed", out
        assert out["agent"] == TARGET  # resolved case-insensitively, like talk_to_agent
        assert out["output"] == "I can see my files."
        assert out["new_thread"] is True and out["turn"] == 1
        assert out["thread_id"].startswith(PREFIX)
        assert out["ran_with"] == {"config": "live", "system_prompt": "configured"}
        (call,) = out["tool_calls"]
        assert call["tool"] == "ls" and call["result"]
        assert out["thread_id"] in out["next"]

        # Recorded under the TARGET's sessions, on its own thread.
        rows = runner.load_session(out["thread_id"])
        assert [r["role"] for r in rows if r["role"] in ("user", "assistant")] == [
            "user",
            "assistant",
        ]
        assert not (get_olano_paths().sessions_dir / FATHER).exists()

    async def test_reusing_the_thread_continues_the_conversation(
        self, as_father, admin_turn, start_agent
    ):
        runner = await start_agent([{"text": "first answer"}, {"text": "second answer"}])
        first = await test_agent(user_message="remember the word pelican")
        second = await test_agent(user_message="what was the word?", thread_id=first["thread_id"])

        assert second["status"] == "completed", second
        assert second["thread_id"] == first["thread_id"]
        assert second["new_thread"] is False and second["turn"] == 2
        assert second["output"] == "second answer"
        state = await runner.agent.aget_state(runner._invoke_config(first["thread_id"]))
        humans = [m.content for m in state.values["messages"] if m.type == "human"]
        assert humans == ["remember the word pelican", "what was the word?"]

    async def test_instructions_override_reaches_the_model_and_saves_nothing(
        self, as_father, admin_turn, start_agent, prompts_seen
    ):
        runner = await start_agent([{"text": "ok"}])
        out = await test_agent(user_message="hi", system_prompt="TRIAL-PROMPT-MARKER")

        assert out["status"] == "completed", out
        assert out["ran_with"]["system_prompt"] == "override (instructions)"
        prompt = prompts_seen[-1]
        assert "TRIAL-PROMPT-MARKER" in prompt
        assert "CONFIGURED-PROMPT-MARKER" not in prompt
        # Still the assembled platform prompt around the replaced instructions.
        assert len(prompt) > 20 * len("TRIAL-PROMPT-MARKER")
        assert load_agent_config(TARGET).system_prompt == "CONFIGURED-PROMPT-MARKER"
        assert runner.config.system_prompt == "CONFIGURED-PROMPT-MARKER"

    async def test_full_override_replaces_the_whole_assembled_prompt(
        self, as_father, admin_turn, start_agent, prompts_seen
    ):
        await start_agent([{"text": "ok"}])
        out = await test_agent(
            user_message="hi", system_prompt="ONLY-THIS-PROMPT", system_prompt_mode="full"
        )
        assert out["status"] == "completed", out
        # deepagents still appends its own tool/skill sections after it.
        assert prompts_seen[-1].startswith("ONLY-THIS-PROMPT")
        assert "CONFIGURED-PROMPT-MARKER" not in prompts_seen[-1]

    async def test_the_same_thread_can_alternate_live_and_override(
        self, as_father, admin_turn, start_agent, prompts_seen
    ):
        await start_agent([{"text": "a"}])
        first = await test_agent(user_message="one")
        second = await test_agent(
            user_message="two", thread_id=first["thread_id"], system_prompt="VARIANT-B"
        )
        assert second["status"] == "completed", second
        assert second["turn"] == 2
        assert "CONFIGURED-PROMPT-MARKER" in prompts_seen[0]
        assert "VARIANT-B" in prompts_seen[-1]

    async def test_reload_config_builds_from_disk(
        self, as_father, admin_turn, start_agent, prompts_seen
    ):
        await start_agent([{"text": "ok"}])
        seed_agent(system_prompt="EDITED-ON-DISK")  # the running copy predates this
        out = await test_agent(user_message="hi", reload_config=True)

        assert out["status"] == "completed", out
        assert out["ran_with"]["config"] == "reloaded from disk"
        assert "EDITED-ON-DISK" in prompts_seen[-1]

    async def test_an_agent_that_is_not_running_is_built_from_disk(
        self, as_father, admin_turn, runtime, monkeypatch
    ):
        seed_agent()
        set_script(monkeypatch, [{"text": "built fresh"}])
        out = await test_agent(user_message="hi")
        assert out["status"] == "completed", out
        assert out["output"] == "built fresh"
        assert out["ran_with"]["config"] == "built from disk (not running)"

    async def test_the_agent_under_test_cannot_reconfigure_itself(
        self, as_father, admin_turn, start_agent
    ):
        """Driven through the real graph: the agent under test holds the
        self_* tools by default and tries one; the gate refuses it."""
        await start_agent(
            [
                {"tool": "self_update_prompt", "args": {"system_prompt": "pwned"}},
                {"text": "tried"},
            ]
        )
        out = await test_agent(user_message="rewrite your prompt")

        assert out["status"] == "completed", out
        (call,) = out["tool_calls"]
        assert call["tool"] == "self_update_prompt"
        assert "a human" in call["result"]
        assert load_agent_config(TARGET).system_prompt == "CONFIGURED-PROMPT-MARKER"

    async def test_agentfather_calling_the_tool_gets_the_reply(
        self, admin_turn, start_agent, monkeypatch
    ):
        """The whole loop as an admin would trigger it: AgentFather's own turn
        calls olano_test_agent, the target runs on its own test thread, and
        AgentFather continues with the result."""
        await start_agent([{"text": "hi from the agent under test"}])
        father = await start_agent(
            [
                {"tool": "olano_test_agent", "args": {"agent": TARGET, "user_message": "say hi"}},
                {"text": "the test passed"},
            ],
            name=FATHER,
            tools=["olano_test_agent"],
        )
        token = current_agent.set(FATHER)
        try:
            reply = await father.ainvoke("please test the tester agent", thread_id="admin-chat")
        finally:
            current_agent.reset(token)

        assert reply == "the test passed"
        sessions = get_olano_paths().sessions_dir / TARGET
        (test_thread,) = [p for p in sessions.glob("*.jsonl") if p.stem.startswith(PREFIX)]
        rows = [json.loads(line) for line in test_thread.read_text().splitlines()]
        assert [r["content"] for r in rows if r["role"] == "assistant"] == [
            "hi from the agent under test"
        ]
        # AgentFather's thread holds only its own turn.
        father_rows = father.load_session("admin-chat")
        assert not any(r.get("content") == "say hi" for r in father_rows)
        assert not any(r.get("content") == "hi from the agent under test" for r in father_rows)

    async def test_a_failing_turn_reports_the_error_without_a_stale_trace(
        self, as_father, admin_turn, start_agent, monkeypatch
    ):
        runner = await start_agent([])

        # `ainvoke` is a wrapper over the stream path, so the graph fails
        # where it is actually driven: its `astream`.
        async def boom(*_a, **_k):
            raise RuntimeError("model fell over")
            yield  # pragma: no cover - makes this an async generator

        monkeypatch.setattr(runner.agent, "astream", boom)
        out = await test_agent(user_message="hi")
        assert out["status"] == "error"
        assert "model fell over" in out["error"]
        assert out["thread_id"].startswith(PREFIX)
        assert "tool_calls" not in out

    async def test_a_slow_turn_times_out(self, start_agent, monkeypatch):
        import asyncio

        runner = await start_agent([])

        async def slow(*_a, **_k):
            await asyncio.sleep(30)

        monkeypatch.setattr(_agent_test, "MIN_TIMEOUT_SECONDS", 0)
        monkeypatch.setattr(runner.agent, "ainvoke", slow)
        out = await _agent_test.run_agent_test(
            TARGET, caller=FATHER, user_message="hi", timeout_seconds=0
        )
        assert out["status"] == "timeout"
        assert "did not finish" in out["error"]


class TestDistillation:
    def test_test_threads_are_not_distilled_into_memory(self):
        from olano.memory.self_learning import SelfLearningService

        seed_agent()
        sessions = get_olano_paths().sessions_dir / TARGET
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / f"{PREFIX}abc.jsonl").write_text(
            json.dumps({"role": "user", "content": "my name is Testy McTest"}) + "\n"
        )
        (sessions / "real-chat.jsonl").write_text(
            json.dumps({"role": "user", "content": "real"}) + "\n"
        )

        service = SelfLearningService.__new__(SelfLearningService)
        service.config = load_agent_config(TARGET)
        service._processed_offsets = {}
        service._processed_byte_offsets = {}
        turns, pending = service._scan_session_turns()
        assert [t["content"] for t in turns] == ["real"]
        assert all(PREFIX not in key for key in pending)
