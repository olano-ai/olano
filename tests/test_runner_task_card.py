"""The `task` (subagent) card never ends up with an empty description.

The runner announces a `task` call early, from the streamed tool-call chunks,
so the dashboard shows the subagent starting rather than nothing until it
returns. When the model streams ``subagent_type`` before ``description`` the
early read has no description yet; it used to be what the card showed for the
whole run AND what the persisted row kept, because the ToolMessage never
re-announced an announced call. These pin the correction: a second frame once
the JSON closes, one persisted row with the whole brief, and no redundant
frame when the early read was already right.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from langchain_core.messages import AIMessageChunk, ToolMessage

from olano.config.agent_config import AgentConfig
from olano.core.runner import AgentRunner, _task_card_args

CALL_ID = "call-task-1"
BRIEF = 'Check how daycap is enforced in outreach/limits.py and say "per platform" or not.'
FULL = {"subagent_type": "general-purpose", "description": BRIEF}


def _chunk(args: str, *, first: bool = False) -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        tool_call_chunks=[
            {
                "name": "task" if first else None,
                "args": args,
                "id": CALL_ID if first else None,
                "index": 0,
                "type": "tool_call_chunk",
            }
        ],
    )


def _runner(config: AgentConfig, deltas: list[str]) -> AgentRunner:
    """A runner whose graph streams `deltas` as one `task` call, then its result."""

    async def fake_astream(*args: Any, **kwargs: Any):
        for i, delta in enumerate(deltas):
            yield _chunk(delta, first=(i == 0))
        yield ToolMessage(content="subagent done", tool_call_id=CALL_ID, name="task")

    agent = MagicMock()
    agent.astream = fake_astream
    agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    agent.ainvoke = AsyncMock(return_value={"messages": []})
    return AgentRunner(agent, config)


async def _collect(runner: AgentRunner, thread_id: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    async for ev in runner.astream_events("go", thread_id=thread_id):
        events.append(ev)
    return events


def _task_calls(events: list[tuple[str, Any]]) -> list[dict[str, str]]:
    return [
        json.loads(payload["args"])
        for kind, payload in events
        if kind == "tool_call" and payload["name"] == "task"
    ]


def _task_rows(runner: AgentRunner, thread_id: str) -> list[dict[str, Any]]:
    return [
        e
        for e in runner.load_session(thread_id)
        if e.get("role") == "tool_call" and e.get("name") == "task"
    ]


async def test_subagent_type_first_card_is_corrected_when_the_json_closes(
    base_config: AgentConfig,
) -> None:
    runner = _runner(
        base_config,
        [
            "",
            '{"subagent_type": "general-purpose", ',
            '"description": "Check how daycap is enforced in outreach/limits.py ',
            'and say \\"per platform\\" or not."}',
        ],
    )
    thread_id = str(uuid.uuid4())
    events = await _collect(runner, thread_id)

    calls = _task_calls(events)
    # Announced early with the description still to come, then corrected.
    assert calls[0] == {"subagent_type": "general-purpose", "description": ""}
    assert calls[-1] == FULL
    assert len(calls) == 2
    ids = {p["id"] for k, p in events if k == "tool_call" and p["name"] == "task"}
    assert ids == {CALL_ID}  # the client upserts by id: one card, args replaced

    # Exactly one persisted row (analytics count rows), carrying the whole brief
    # as parseable `args`, so a reload draws what the live card drew.
    rows = _task_rows(runner, thread_id)
    assert len(rows) == 1
    assert json.loads(rows[0]["args"]) == FULL
    assert rows[0]["content"].startswith("task: ")

    # The result still pairs with the call.
    results = [p for k, p in events if k == "tool_result"]
    assert results == [{"id": CALL_ID, "name": "task", "content": "subagent done"}]


async def test_description_first_announces_once(base_config: AgentConfig) -> None:
    runner = _runner(
        base_config,
        [
            "",
            '{"description": "Summarize README.md", ',
            '"subagent_type": "general-purpose"}',
        ],
    )
    thread_id = str(uuid.uuid4())
    events = await _collect(runner, thread_id)

    calls = _task_calls(events)
    # The early read was already complete: no redundant correction frame.
    assert calls == [{"subagent_type": "general-purpose", "description": "Summarize README.md"}]
    rows = _task_rows(runner, thread_id)
    assert len(rows) == 1
    assert json.loads(rows[0]["args"]) == calls[0]


async def test_stream_that_never_closes_takes_the_middleware_args(
    base_config: AgentConfig, monkeypatch
) -> None:
    """The JSON never closed on the wire: the ToolMessage writes the row and
    corrects the card from the args the middleware captured pre-execution."""
    from olano.security import deepagents_middleware as mw

    runner = _runner(
        base_config, ["", '{"subagent_type": "general-purpose", "description": "Check daycap']
    )
    thread_id = str(uuid.uuid4())
    mw._TOOL_ARGS_REGISTRY[CALL_ID] = {"name": "task", "args": dict(FULL)}
    try:
        events = await _collect(runner, thread_id)
    finally:
        mw._TOOL_ARGS_REGISTRY.pop(CALL_ID, None)

    calls = _task_calls(events)
    assert calls[0] == {"subagent_type": "general-purpose", "description": ""}
    assert calls[-1] == FULL
    rows = _task_rows(runner, thread_id)
    assert len(rows) == 1
    assert json.loads(rows[0]["args"]) == FULL


def test_task_card_args_reads_partial_then_complete() -> None:
    assert _task_card_args("") is None
    assert _task_card_args('{"description": "Che') is None
    assert _task_card_args('{"subagent_type": "gp", "description": "Che') == (
        {"subagent_type": "gp", "description": ""},
        False,
    )
    assert _task_card_args('{"description": "Check", "subagent_type": "gp"') == (
        {"subagent_type": "gp", "description": "Check"},
        False,
    )
    # Escaped quotes defeat the partial read but not the complete one.
    full = json.dumps({"subagent_type": "gp", "description": 'say "hi" twice'})
    assert _task_card_args(full) == ({"subagent_type": "gp", "description": 'say "hi" twice'}, True)
