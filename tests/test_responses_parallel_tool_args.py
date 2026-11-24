"""Parallel tool calls keep their arguments on the Responses API stream.

langchain-openai indexes streamed blocks by bumping a counter whenever the
event's ``output_index`` changes. A backend that announces every parallel call
first and streams the arguments after therefore produced calls with ``{}`` as
arguments (each failing ``Field required``) plus nameless calls carrying the
real ones. ``models._patch_responses_parallel_tool_args`` routes each delta to
its call by item id.

These tests drive a real ``ChatOpenAI`` Responses client over an
``httpx.MockTransport`` serving a server-sent-event body, so the OpenAI SDK
parses genuine event objects and langchain's own stream loop runs unchanged.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage

from olano.models import _responses_chat_openai_class

_CALLS = [
    ("read_file", {"file_path": "/ws/reports/report.md", "limit": 1000}),
    ("document_to_text", {"file_path": "/ws/campaign/README.md"}),
    ("ls", {"path": "/ws/campaign"}),
]


def _function_item(i: int, *, arguments: str = "", status: str = "in_progress") -> dict:
    name, _ = _CALLS[i]
    return {
        "type": "function_call",
        "id": f"fc_{i}",
        "call_id": f"call_{i}",
        "name": name,
        "arguments": arguments,
        "status": status,
    }


def _added(i: int) -> dict:
    return {"type": "response.output_item.added", "output_index": i, "item": _function_item(i)}


def _deltas(i: int) -> list[dict]:
    raw = json.dumps(_CALLS[i][1])
    mid = len(raw) // 2
    return [
        {
            "type": "response.function_call_arguments.delta",
            "output_index": i,
            "item_id": f"fc_{i}",
            "delta": part,
        }
        for part in (raw[:mid], raw[mid:])
    ]


def _done(i: int) -> dict:
    return {
        "type": "response.output_item.done",
        "output_index": i,
        "item": _function_item(i, arguments=json.dumps(_CALLS[i][1]), status="completed"),
    }


def _response(status: str) -> dict:
    output = (
        [
            _function_item(i, arguments=json.dumps(_CALLS[i][1]), status="completed")
            for i in range(3)
        ]
        if status == "completed"
        else []
    )
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": "gpt-test",
        "status": status,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


def _sse(events: list[dict]) -> bytes:
    body = [{"type": "response.created", "response": _response("in_progress")}]
    body += events
    body.append({"type": "response.completed", "response": _response("completed")})
    lines = []
    for seq, event in enumerate(body):
        event = {**event, "sequence_number": seq}
        lines.append(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n")
    return "".join(lines).encode()


def _sequential() -> list[dict]:
    events: list[dict] = []
    for i in range(3):
        events += [_added(i), *_deltas(i), _done(i)]
    return events


def _announce_first() -> list[dict]:
    """What the Codex backend sent: every call announced before any argument."""
    events: list[dict] = [_added(i) for i in range(3)]
    for i in range(3):
        events += _deltas(i)
    events += [_done(i) for i in range(3)]
    return events


def _interleaved() -> list[dict]:
    events: list[dict] = [_added(i) for i in range(3)]
    first, second = zip(*(_deltas(i) for i in range(3)), strict=True)
    events += [*first, *second]
    events += [_done(i) for i in range(3)]
    return events


def _no_deltas() -> list[dict]:
    """Arguments arrive only on each item's ``done`` event."""
    events: list[dict] = [_added(i) for i in range(3)]
    events += [_done(i) for i in range(3)]
    return events


def _model(events: list[dict], *, use_async: bool) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=_sse(events)
        )

    transport = httpx.MockTransport(handler)
    client_kw: dict[str, Any] = (
        {"http_async_client": httpx.AsyncClient(transport=transport)}
        if use_async
        else {"http_client": httpx.Client(transport=transport)}
    )
    return _responses_chat_openai_class()(
        model="gpt-test",
        api_key="sk-test",
        base_url="http://responses.test/v1",
        use_responses_api=True,
        streaming=True,
        **client_kw,
    )


def _assert_three_complete_calls(message: AIMessageChunk) -> None:
    calls = [(tc["name"], tc["args"], tc["id"]) for tc in message.tool_calls]
    assert calls == [(name, args, f"call_{i}") for i, (name, args) in enumerate(_CALLS)]
    assert message.invalid_tool_calls == []


@pytest.mark.parametrize(
    "events",
    [_sequential(), _announce_first(), _interleaved(), _no_deltas()],
    ids=["sequential", "announce-first", "interleaved", "no-deltas"],
)
def test_stream_keeps_every_parallel_calls_arguments(events: list[dict]) -> None:
    message: AIMessageChunk | None = None
    for chunk in _model(events, use_async=False).stream([HumanMessage("go")]):
        message = chunk if message is None else message + chunk
    assert message is not None
    _assert_three_complete_calls(message)


async def test_astream_keeps_every_parallel_calls_arguments() -> None:
    message: AIMessageChunk | None = None
    async for chunk in _model(_announce_first(), use_async=True).astream([HumanMessage("go")]):
        message = chunk if message is None else message + chunk
    assert message is not None
    _assert_three_complete_calls(message)


def test_patch_is_idempotent_in_any_order_with_the_usage_salvage() -> None:
    import langchain_openai.chat_models.base as lc_base

    from olano.models import (
        _patch_responses_parallel_tool_args,
        _patch_responses_terminal_usage_salvage,
    )

    original = lc_base._convert_responses_chunk_to_generation_chunk
    try:
        _patch_responses_parallel_tool_args()
        _patch_responses_terminal_usage_salvage()
        installed = lc_base._convert_responses_chunk_to_generation_chunk
        _patch_responses_parallel_tool_args()
        _patch_responses_terminal_usage_salvage()
        assert lc_base._convert_responses_chunk_to_generation_chunk is installed
    finally:
        lc_base._convert_responses_chunk_to_generation_chunk = original
