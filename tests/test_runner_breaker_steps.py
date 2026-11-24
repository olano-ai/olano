"""The tool-error circuit breaker counts model steps and saves what it cut off.

Two defects, both seen live when a parser bug sent three parallel ``read_file``
calls with ``{}`` as arguments:

- The breaker counted tool *results*, so three identical parallel failures from
  one model step tripped a limit of 3 before the model ever saw the error, and
  the stop message claimed "I tried that 3 times".
- It stops reading the graph mid-step, before LangGraph commits that step, so
  the errors never reached the checkpoint. On the next turn deepagents patched
  the calls as "cancelled - another message came in" and the model, never told
  what failed, repeated them.

And one found while fixing them: whenever deepagents patches a dangling call
(the breaker before this fix, or a user pressing Stop mid-step), its node
re-emits the whole history, and the runner showed the previous turn's text and
tool calls again as this turn's output.

The graph tests run a real ``create_deep_agent`` graph over an in-memory
checkpointer; only the model is fake.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from olano.config.agent_config import AgentConfig
from olano.core.runner import AgentRunner

# What deepagents' PatchToolCallsMiddleware writes for a call with no result:
# the first spelling through 0.7.11, the second since 0.7.23.
CANCELLED = (
    "was cancelled - another message came in",
    "did not complete - no result was recorded",
)


@tool
def read_file(file_path: str) -> str:
    """Read a file."""
    return f"contents of {file_path}"


class _ScriptedModel(BaseChatModel):
    """Replies from a script and records every prompt it was given."""

    script: Iterator[AIMessage]
    prompts: list[list[BaseMessage]]

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.prompts.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=next(self.script))])

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedModel:
        return self


def _parallel_empty_reads(step: int) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "read_file", "args": {}, "id": f"s{step}c{i}", "type": "tool_call"}
            for i in range(3)
        ],
    )


def _graph(script: list[AIMessage]) -> tuple[Any, _ScriptedModel]:
    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import InMemorySaver

    model = _ScriptedModel(script=iter(script), prompts=[])
    graph = create_deep_agent(model=model, tools=[read_file], checkpointer=InMemorySaver())
    return graph, model


async def _turn(runner: AgentRunner, message: str, thread_id: str) -> list[tuple[str, Any]]:
    return [event async for event in runner.astream_events(message, thread_id=thread_id)]


def _text(events: list[tuple[str, Any]]) -> str:
    return "".join(p for k, p in events if k == "text")


async def test_identical_parallel_failures_in_one_step_are_one_attempt(
    base_config: AgentConfig,
) -> None:
    config = base_config.model_copy(update={"tool_error_retry_limit": 3})
    graph, model = _graph([_parallel_empty_reads(1), AIMessage(content="Reading them one by one.")])
    runner = AgentRunner(graph, config)

    events = await _turn(runner, "read the reports", str(uuid.uuid4()))

    assert "I tried that" not in _text(events)
    assert "Reading them one by one." in _text(events)
    # The model got its second call, and saw why the first one failed.
    assert len(model.prompts) == 2
    errors = [m for m in model.prompts[1] if isinstance(m, ToolMessage)]
    assert len(errors) == 3
    assert all("Field required" in str(m.content) for m in errors)


async def test_breaker_counts_steps_and_next_turn_sees_the_real_errors(
    base_config: AgentConfig,
) -> None:
    config = base_config.model_copy(update={"tool_error_retry_limit": 2})
    graph, model = _graph(
        [
            _parallel_empty_reads(1),
            _parallel_empty_reads(2),
            AIMessage(content="never reached in turn one"),
        ]
    )
    runner = AgentRunner(graph, config)
    thread_id = str(uuid.uuid4())

    first = await _turn(runner, "read the reports", thread_id)
    assert "I tried that 2 times" in _text(first)
    assert "never reached" not in _text(first)
    assert len(model.prompts) == 2

    await _turn(runner, "why did that fail?", thread_id)
    assert len(model.prompts) == 3

    history = model.prompts[-1]
    tool_results = [str(m.content) for m in history if isinstance(m, ToolMessage)]
    assert len(tool_results) == 6
    assert not any(marker in content for marker in CANCELLED for content in tool_results)
    assert all("Field required" in content for content in tool_results)
    # The stop message is in the model's context too, right after the errors.
    stop_index = next(
        i
        for i, m in enumerate(history)
        if isinstance(m, AIMessage) and "I tried that 2 times" in str(m.content)
    )
    assert isinstance(history[stop_index - 1], ToolMessage)


async def test_a_failed_save_does_not_break_the_turn(base_config: AgentConfig) -> None:
    config = base_config.model_copy(update={"tool_error_retry_limit": 2})

    async def fake_astream(*args: Any, **kwargs: Any):
        for step in range(2):
            yield AIMessage(
                content="",
                id=f"run-{step}",
                tool_calls=[{"id": f"c{step}", "name": "read_file", "args": {}}],
            )
            yield ToolMessage(
                content="Error: file_path: Field required",
                tool_call_id=f"c{step}",
                name="read_file",
                status="error",
            )

    agent = MagicMock()
    agent.astream = fake_astream
    agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    agent.aupdate_state = AsyncMock(side_effect=RuntimeError("checkpointer down"))
    runner = AgentRunner(agent, config)

    events = await _turn(runner, "go", str(uuid.uuid4()))

    assert "I tried that 2 times" in _text(events)
    agent.aupdate_state.assert_awaited_once()
    _, update = agent.aupdate_state.await_args.args
    assert [type(m).__name__ for m in update["messages"]] == ["ToolMessage", "AIMessage"]
    assert agent.aupdate_state.await_args.kwargs == {"as_node": "model"}


async def test_patched_history_is_not_replayed_as_new_output(base_config: AgentConfig) -> None:
    graph, _ = _graph(
        [
            AIMessage(
                content="Checking the files.",
                tool_calls=[
                    {"name": "read_file", "args": {"file_path": "a"}, "id": "c0"},
                    {"name": "read_file", "args": {"file_path": "b"}, "id": "c1"},
                ],
            ),
            AIMessage(content="Fresh answer."),
        ]
    )
    runner = AgentRunner(graph, base_config)
    thread_id = str(uuid.uuid4())

    # Turn one stops mid-step (as a Stop press does), leaving dangling calls.
    stream = graph.astream(
        {"messages": [{"role": "user", "content": "go"}]},
        config=runner._invoke_config(thread_id),
        stream_mode=["messages", "updates"],
        subgraphs=True,
    )
    async for _ns, mode, data in stream:
        if mode == "messages" and isinstance(data[0], ToolMessage):
            break
    await stream.aclose()

    events = await _turn(runner, "again", thread_id)

    assert _text(events).strip() == "Fresh answer."
    assert [k for k, _ in events if k in ("tool_call", "tool_result")] == []


def test_history_patch_node_name_matches_deepagents() -> None:
    from deepagents.middleware.patch_tool_calls import PatchToolCallsMiddleware

    from olano.core.runner import _HISTORY_PATCH_NODE

    assert f"{PatchToolCallsMiddleware().name}.before_agent" == _HISTORY_PATCH_NODE
