"""What an agent is told when a tool call fails, and when the breaker is near.

Seen live: an agent editing a module and re-running the project's unittest
suite got cut off by the tool-error circuit breaker on its third red run. The
error had reached it every time, but the inline preview of the 41-line report
was a head/tail cut whose "[... 13 lines omitted ...]" gap held the first
``AttributeError`` line, nothing warned that the next identical failure would
end the turn, and the two file edits between the runs did not count as a
change of situation.

Three fixes, each pinned here:

- a failing command's preview keeps the diagnostic lines (``shell_output``);
- the failing result itself warns when one more identical failure ends the
  turn (``core/tool_failures`` + the security middleware);
- a successful ``edit_file`` / ``write_file`` resets the breaker's count
  (``runner``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolCallRequest

from olano.config.agent_config import AgentConfig
from olano.core.runner import AgentRunner, _repeated_failure_response
from olano.core.tool_failures import (
    REPEAT_FAILURE_MARKER,
    WRITE_TOOL_NAMES,
    TurnToolFailures,
    args_signature_text,
    bind_turn_tool_failures,
    is_failed_tool_result,
    strip_repeat_failure_notice,
    tool_call_signature,
)
from olano.security import deepagents_middleware as mw
from olano.security.shell_output import (
    FAILURE_HINT,
    FAILURE_PREVIEW_MAX_CHARS,
    INLINE_MAX_LINES,
    PREVIEW_LINE_MAX_CHARS,
    SUCCESS_HINT,
    summarize_shell_output,
)

# ---------------------------------------------------------------------------
# Preview of a failing command
# ---------------------------------------------------------------------------

MISSING_CLAIM = "AttributeError: 'ReviewTests' object has no attribute 'claim'"


def _unittest_stderr(errors: int = 4, *, passing: int = 0) -> str:
    """A unittest report shaped like the live one: 450 dots, N tracebacks.

    ``passing`` adds verbose ``... ok`` lines after the dots, enough to push the
    report past the inline limits (the live 41-line report is now inline).
    """
    lines = ["." * 77 + "E..E.....E....E" + "." * 350, ""]
    lines += [
        f"test_ok_{n} (test_budget_review.ReviewTests.test_ok_{n}) ... ok" for n in range(passing)
    ]
    for n in range(errors):
        lines += [
            "=" * 70,
            f"ERROR: test_case_{n} (test_budget_review.ReviewTests.test_case_{n})",
            "-" * 70,
            "Traceback (most recent call last):",
            f'  File "/workspace/campaign/runtime/test_budget_review.py", line {100 + n}, in test_case_{n}',
            f"    self.claim('key{n}', maximum=1, unit='apollo:credits')",
            "    ^^^^^^^^^^",
            MISSING_CLAIM,
            "",
        ]
    lines += ["-" * 70, "Ran 345 tests in 1.937s", "", "FAILED (errors=4)"]
    return "\n".join(lines)


def _summarize(tmp_path: Path, *, stdout: str = "", stderr: str = "", returncode: int):
    return summarize_shell_output(
        tool_name="execute",
        tool_call_id="call_1",
        workspace_root=tmp_path,
        command="python -m unittest discover",
        runtime_command="python -m unittest discover",
        timeout=120,
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
    )


def test_live_failing_report_is_now_returned_whole(tmp_path: Path) -> None:
    """The live 41-line report fits the inline limits: no log, nothing cut."""
    stderr = _unittest_stderr()
    summary = _summarize(tmp_path, stderr=stderr, returncode=1)
    assert summary.artifact is None and not summary.large_output
    assert summary.content == stderr
    assert summary.content.count(MISSING_CLAIM) == 4


def test_failing_command_preview_keeps_every_exception_line(tmp_path: Path) -> None:
    stderr = _unittest_stderr(passing=250)
    assert len(stderr.splitlines()) > INLINE_MAX_LINES  # goes to a log

    summary = _summarize(tmp_path, stderr=stderr, returncode=1)

    content = summary.content
    assert content.startswith("Command failed with exit code 1.")
    assert FAILURE_HINT in content
    # All four errors are named, with the exception and the failing call.
    assert content.count(MISSING_CLAIM) == 4
    assert content.count("self.claim(") == 4
    assert "ERROR: test_case_0" in content and "ERROR: test_case_3" in content
    # The run summary survives too.
    assert "Ran 345 tests" in content and "FAILED (errors=4)" in content
    # The 450-dot progress line no longer eats the budget.
    assert all(len(line) <= PREVIEW_LINE_MAX_CHARS for line in content.splitlines())
    assert "…" in content
    # And the full report is on disk, untouched.
    assert summary.artifact is not None
    assert stderr in Path(summary.artifact["log_path"]).read_text(encoding="utf-8")


def test_head_tail_cut_would_have_dropped_the_first_exception(tmp_path: Path) -> None:
    """The regression this guards: the same report under the positional cut."""
    from olano.security.shell_output import _shell_preview_block

    positional = _shell_preview_block("stderr", _unittest_stderr(), diagnostic=False)
    assert positional.count(MISSING_CLAIM) < 4
    diagnostic = _shell_preview_block(
        "stderr", _unittest_stderr(), diagnostic=True, max_chars=FAILURE_PREVIEW_MAX_CHARS
    )
    assert diagnostic.count(MISSING_CLAIM) == 4


def test_successful_large_output_keeps_the_positional_preview(tmp_path: Path) -> None:
    stdout = "\n".join(f"line {i}" for i in range(400))
    summary = _summarize(tmp_path, stdout=stdout, returncode=0)
    assert "Command completed with large output." in summary.content
    assert SUCCESS_HINT in summary.content
    assert "[... 372 lines omitted ...]" in summary.content
    assert "line 0" in summary.content and "line 399" in summary.content


def test_failure_without_diagnostic_lines_falls_back_to_head_and_tail(tmp_path: Path) -> None:
    stdout = "\n".join(f"line {i}" for i in range(400))
    summary = _summarize(tmp_path, stdout=stdout, returncode=1)
    assert FAILURE_HINT in summary.content
    assert "[... 372 lines omitted ...]" in summary.content


def test_diagnostic_preview_over_budget_drops_the_middle_first(tmp_path: Path) -> None:
    stderr = "\n".join(f"ERROR: module_{i:03d} failed to compile" for i in range(400))
    summary = _summarize(tmp_path, stderr=stderr, returncode=2)
    preview = summary.content.split("stderr preview:\n", 1)[1]
    assert len(preview) <= FAILURE_PREVIEW_MAX_CHARS
    assert "ERROR: module_000" in preview
    assert "ERROR: module_399" in preview
    assert "lines omitted" in preview


def test_pytest_and_node_shapes_are_recognised(tmp_path: Path) -> None:
    from olano.security.shell_output import _diagnostic_indexes

    lines = [
        "collected 3 items",
        "E       assert 1 == 2",
        "    at Object.<anonymous> (/app/test.js:12:5)",
        "npm ERR! code ENOENT",
        "sh: 1: foo: not found",
        "just a normal line",
    ]
    kept = _diagnostic_indexes(lines)
    assert kept == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


def test_notice_arrives_one_failure_before_the_limit() -> None:
    ledger = TurnToolFailures(limit=3)
    sig = tool_call_signature("execute", '{"command": "pytest"}')
    assert ledger.notice("execute", ledger.record_failure(sig)) is None
    second = ledger.notice("execute", ledger.record_failure(sig))
    assert second is not None
    assert second.startswith(REPEAT_FAILURE_MARKER)
    assert "failed 2 times" in second
    assert "One more identical failing call ends the turn" in second
    # A different call is a different count.
    assert ledger.notice("execute", ledger.record_failure("execute\x00other")) is None


def test_no_notice_when_the_breaker_is_off_or_fires_first() -> None:
    assert TurnToolFailures(limit=0).notice("x", 5) is None
    assert TurnToolFailures(limit=1).notice("x", 1) is None
    assert TurnToolFailures(limit=2).notice("x", 1) is not None


def test_a_successful_write_resets_the_ledger_and_a_read_does_not() -> None:
    ledger = TurnToolFailures(limit=3)
    ledger.record_failure("a")
    ledger.record_failure("a")
    ledger.record_success("read_file")
    assert ledger.failures == {"a": 2}
    ledger.record_success("edit_file")
    assert ledger.failures == {}


def test_signature_and_write_set_match_the_other_side() -> None:
    args = {"command": "python -m unittest", "timeout": 120}
    # The runner serialises the registry entry with json.dumps; so do we.
    assert args_signature_text(args) == json.dumps(args)
    assert args_signature_text({}) == ""
    assert args_signature_text("raw") == "raw"
    assert WRITE_TOOL_NAMES == mw._WRITE_FILE_TOOL_NAMES


def test_failed_result_definition() -> None:
    from olano.tools._error_boundary import TOOL_ERROR_MARKER

    assert is_failed_tool_result("error", "anything")
    assert is_failed_tool_result("success", "Error: nope")
    assert is_failed_tool_result(None, "[blocked] no")
    assert is_failed_tool_result(None, "[connector-auth] expired")
    assert is_failed_tool_result("success", f"{TOOL_ERROR_MARKER}x' failed")
    assert not is_failed_tool_result("success", "Command completed with large output.")


def test_strip_notice() -> None:
    body = "Error: boom"
    assert strip_repeat_failure_notice(f"{body}\n\n{REPEAT_FAILURE_MARKER} ...") == body
    assert strip_repeat_failure_notice(body) == body


# ---------------------------------------------------------------------------
# The middleware writes the notice on the result the model reads
# ---------------------------------------------------------------------------


@pytest.fixture
def shell_config(tmp_path: Path) -> AgentConfig:
    from olano.config.backend_config import LocalShellBackendConfig

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
    (workspace / "memories").mkdir()
    return AgentConfig(
        name="feedback-agent",
        model="anthropic:claude-haiku-4-5-20251001",
        system_prompt="feedback",
        workspace=str(workspace),
        backend=LocalShellBackendConfig(root_dir=str(workspace)),
        memory={"markdown": False, "rag": False, "self_learning": False},
        tool_error_retry_limit=3,
    )


def _request(name: str, args: dict[str, Any], call_id: str) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args, "id": call_id},
        tool=None,
        state={},
        runtime=SimpleNamespace(),
    )


async def test_second_identical_shell_failure_carries_the_notice(
    shell_config: AgentConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olano.security.shell_guard import ShellResult

    monkeypatch.setattr(
        "olano.security.shell_guard.run_local",
        lambda *a, **k: ShellResult(stdout="", stderr="boom", returncode=1),
    )
    middleware = mw.OlanoSecurityMiddleware(
        config=shell_config, workspace_root=Path(shell_config.workspace)
    )
    args = {"command": "python -m unittest", "timeout": 60}

    with bind_turn_tool_failures(TurnToolFailures(limit=3)):
        first = await middleware.awrap_tool_call(_request("execute", args, "c1"), AsyncMock())
        second = await middleware.awrap_tool_call(_request("execute", args, "c2"), AsyncMock())
        other = await middleware.awrap_tool_call(
            _request("execute", {"command": "python -m unittest test_x", "timeout": 60}, "c3"),
            AsyncMock(),
        )

    assert isinstance(first, ToolMessage) and first.status == "error"
    assert REPEAT_FAILURE_MARKER not in str(first.content)
    assert str(second.content).startswith("boom")
    assert REPEAT_FAILURE_MARKER in str(second.content)
    assert "This exact `execute` call has now failed 2 times" in str(second.content)
    assert second.status == "error"
    assert REPEAT_FAILURE_MARKER not in str(other.content)


async def test_no_ledger_bound_means_no_notice(
    shell_config: AgentConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from olano.security.shell_guard import ShellResult

    monkeypatch.setattr(
        "olano.security.shell_guard.run_local",
        lambda *a, **k: ShellResult(stdout="", stderr="boom", returncode=1),
    )
    middleware = mw.OlanoSecurityMiddleware(
        config=shell_config, workspace_root=Path(shell_config.workspace)
    )
    for n in range(4):
        result = await middleware.awrap_tool_call(
            _request("execute", {"command": "false"}, f"c{n}"), AsyncMock()
        )
        assert REPEAT_FAILURE_MARKER not in str(result.content)


async def test_generic_tool_failure_notice_and_a_write_resets_it(
    shell_config: AgentConfig,
) -> None:
    middleware = mw.OlanoSecurityMiddleware(
        config=shell_config, workspace_root=Path(shell_config.workspace)
    )

    async def failing(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content="Error: nothing at that key",
            tool_call_id=str(request.tool_call["id"]),
            name="lookup",
            status="error",
        )

    async def edited(request: ToolCallRequest) -> ToolMessage:
        return ToolMessage(
            content="Successfully replaced 1 instance(s)",
            tool_call_id=str(request.tool_call["id"]),
            name="edit_file",
        )

    args = {"key": "budget"}
    edit_args = {
        "file_path": str(Path(shell_config.workspace) / "notes.md"),
        "old_string": "a",
        "new_string": "b",
    }
    with bind_turn_tool_failures(TurnToolFailures(limit=3)):
        r1 = await middleware.awrap_tool_call(_request("lookup", args, "l1"), failing)
        r2 = await middleware.awrap_tool_call(_request("lookup", args, "l2"), failing)
        edit = await middleware.awrap_tool_call(_request("edit_file", edit_args, "e1"), edited)
        r3 = await middleware.awrap_tool_call(_request("lookup", args, "l3"), failing)

    assert REPEAT_FAILURE_MARKER not in str(r1.content)
    assert REPEAT_FAILURE_MARKER in str(r2.content)
    assert "Successfully replaced" in str(edit.content)
    # The edit reset the count: the third failure is a first failure again.
    assert REPEAT_FAILURE_MARKER not in str(r3.content)


# ---------------------------------------------------------------------------
# The runner: a successful edit resets the breaker; the stop message is clean
# ---------------------------------------------------------------------------


def _exec_failure(call_id: str, *, content: str = "Error: bash: claude: command not found"):
    return [
        AIMessage(
            content="",
            tool_calls=[
                {"id": call_id, "name": "execute", "args": {"command": "claude --version"}}
            ],
        ),
        ToolMessage(content=content, tool_call_id=call_id, name="execute", status="error"),
    ]


def _edit_success(call_id: str):
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": call_id,
                    "name": "edit_file",
                    "args": {"file_path": "/w/a.py", "old_string": "x", "new_string": "y"},
                }
            ],
        ),
        ToolMessage(
            content="Successfully replaced 1 instance(s)", tool_call_id=call_id, name="edit_file"
        ),
    ]


def _runner_over(messages: list[Any], config: AgentConfig) -> AgentRunner:
    async def fake_astream(*args: Any, **kwargs: Any):
        for msg in messages:
            yield msg

    agent = MagicMock()
    agent.astream = fake_astream
    agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    agent.ainvoke = AsyncMock(return_value={"messages": []})
    return AgentRunner(agent, config)


async def _collect(runner: AgentRunner) -> list[tuple[str, Any]]:
    return [ev async for ev in runner.astream_events("go", thread_id=str(uuid.uuid4()))]


def _text(events: list[tuple[str, Any]]) -> str:
    return "".join(p for k, p in events if k == "text")


async def test_a_successful_edit_resets_the_breaker_count(base_config: AgentConfig) -> None:
    config = base_config.model_copy(update={"tool_error_retry_limit": 3})
    script = [
        *_exec_failure("c0"),
        *_exec_failure("c1"),
        *_edit_success("e0"),
        *_exec_failure("c2"),
        *_exec_failure("c3"),
        AIMessage(content="Fixed it on the fifth run."),
    ]
    events = await _collect(_runner_over(script, config))

    assert "I tried that" not in _text(events)
    assert "Fixed it on the fifth run." in _text(events)
    assert len([p for k, p in events if k == "tool_result"]) == 5


async def test_without_an_edit_the_same_run_still_trips(base_config: AgentConfig) -> None:
    config = base_config.model_copy(update={"tool_error_retry_limit": 3})
    script = [
        *_exec_failure("c0"),
        *_exec_failure("c1"),
        *_exec_failure("c2"),
        AIMessage(content="never reached"),
    ]
    events = await _collect(_runner_over(script, config))
    assert "I tried that 3 times" in _text(events)
    assert "never reached" not in _text(events)


async def test_a_read_between_failures_does_not_reset(base_config: AgentConfig) -> None:
    config = base_config.model_copy(update={"tool_error_retry_limit": 3})
    read = [
        AIMessage(
            content="",
            tool_calls=[{"id": "r0", "name": "read_file", "args": {"file_path": "/w/log"}}],
        ),
        ToolMessage(content="log contents", tool_call_id="r0", name="read_file"),
    ]
    script = [*_exec_failure("c0"), *read, *_exec_failure("c1"), *_exec_failure("c2")]
    events = await _collect(_runner_over(script, config))
    assert "I tried that 3 times" in _text(events)


def test_stop_message_does_not_quote_the_advance_notice() -> None:
    content = (
        "Error: bash: claude: command not found\n\n"
        f"{REPEAT_FAILURE_MARKER} This exact `execute` call has now failed 2 times..."
    )
    text = _repeated_failure_response("execute", content, 3)
    assert text.startswith("I tried that 3 times and it kept failing because ")
    assert REPEAT_FAILURE_MARKER not in text
    assert "One more identical" not in text


# ---------------------------------------------------------------------------
# End to end on a real graph: the model reads the notice before the stop
# ---------------------------------------------------------------------------


@tool
def lookup(key: str) -> str:
    """Look a key up."""
    return f"value of {key}"


class _ScriptedModel(BaseChatModel):
    script: Iterator[AIMessage]
    prompts: list[list[BaseMessage]]

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self.prompts.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=next(self.script))])

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedModel:
        return self


def _bad_lookup(step: int) -> AIMessage:
    # ``{}`` fails argument validation inside the tool node: a real, in-graph
    # failing result, the same shape the runner's breaker tests use.
    return AIMessage(
        content="",
        tool_calls=[{"name": "lookup", "args": {}, "id": f"s{step}", "type": "tool_call"}],
    )


async def test_model_reads_the_notice_on_the_step_before_the_breaker(
    base_config: AgentConfig,
) -> None:
    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import InMemorySaver

    config = base_config.model_copy(update={"tool_error_retry_limit": 3})
    model = _ScriptedModel(
        script=iter([_bad_lookup(1), _bad_lookup(2), _bad_lookup(3), AIMessage(content="never")]),
        prompts=[],
    )
    graph = create_deep_agent(
        model=model,
        tools=[lookup],
        middleware=mw.build_security_middleware(
            config=config, workspace_root=Path(config.workspace)
        ),
        checkpointer=InMemorySaver(),
    )
    runner = AgentRunner(graph, config)

    events = await _collect(runner)

    assert "I tried that 3 times" in _text(events)
    # Step 2's prompt holds the first failure, plain; step 3's holds the second,
    # with the warning. The model had the notice in hand when it chose to repeat.
    first_result = [m for m in model.prompts[1] if isinstance(m, ToolMessage)][-1]
    second_result = [m for m in model.prompts[2] if isinstance(m, ToolMessage)][-1]
    assert "Field required" in str(first_result.content)
    assert REPEAT_FAILURE_MARKER not in str(first_result.content)
    assert "Field required" in str(second_result.content)
    assert REPEAT_FAILURE_MARKER in str(second_result.content)
    assert "failed 2 times" in str(second_result.content)
    # The dashboard saw the same text the model did.
    results = [p["content"] for k, p in events if k == "tool_result"]
    assert REPEAT_FAILURE_MARKER not in results[0]
    assert REPEAT_FAILURE_MARKER in results[1]
