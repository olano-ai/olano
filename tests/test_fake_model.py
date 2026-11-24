"""Tests for the ``fake:`` provider — the deterministic offline chat model.

Covers the model contract (invoke/stream/bind_tools/usage), the scripted
tool-call flow, and the one thing that makes it valuable: a full
``build_agent → AgentRunner`` turn with zero API keys.
"""

from __future__ import annotations

import json

from langchain_core.messages import AIMessage, HumanMessage

from olano.models import build_model
from olano.models.fake import FakeChatModel


def test_build_model_routes_fake_prefix():
    model = build_model("fake:instant")
    assert isinstance(model, FakeChatModel)
    assert model.ttft_ms == 0.0
    # Unknown kwargs (agent model_kwargs, reasoning) must not break the build.
    assert isinstance(build_model("fake:instant", temperature=0.2), FakeChatModel)


def test_latency_profiles():
    assert build_model("fake:fast").ttft_ms == 150.0
    assert build_model("fake:realistic").token_delay_ms == 12.0
    # Unknown profile name degrades to instant.
    assert build_model("fake:whatever").ttft_ms == 0.0


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("LIONCLAW_FAKE_TTFT_MS", "5")
    monkeypatch.setenv("LIONCLAW_FAKE_TOKEN_DELAY_MS", "1")
    monkeypatch.setenv("LIONCLAW_FAKE_TOKENS", "3")
    model = build_model("fake:realistic")
    assert (model.ttft_ms, model.token_delay_ms, model.n_tokens) == (5.0, 1.0, 3)


def test_invoke_returns_usage_and_content():
    model = build_model("fake:instant")
    result = model.invoke([HumanMessage(content="hello")])
    assert isinstance(result, AIMessage)
    assert result.content.startswith("Fake response #0")
    assert result.usage_metadata is not None
    assert result.usage_metadata["output_tokens"] > 0


async def test_astream_yields_tokens(monkeypatch):
    monkeypatch.setenv("LIONCLAW_FAKE_TOKENS", "5")
    model = build_model("fake:instant")
    chunks = [c async for c in model.astream([HumanMessage(content="hi")])]
    text = "".join(str(c.content) for c in chunks)
    assert text.startswith("Fake response #0")
    # Exactly the configured 5 content tokens; the rest are stream bookkeeping
    # (final usage chunk, plus whatever langchain-core appends at stream end).
    assert sum(1 for c in chunks if c.content) == 5
    assert any(getattr(c, "usage_metadata", None) for c in chunks)


def test_scripted_tool_call_then_text(monkeypatch):
    script = [{"tool": "ls", "args": {"path": "."}}, {"text": "all done"}]
    monkeypatch.setenv("LIONCLAW_FAKE_MODEL_SCRIPT", json.dumps(script))
    model = build_model("fake:instant")

    first = model.invoke([HumanMessage(content="list files")])
    assert first.tool_calls and first.tool_calls[0]["name"] == "ls"
    assert first.tool_calls[0]["args"] == {"path": "."}

    second = model.invoke([HumanMessage(content="and now?")])
    assert second.content == "all done"


def test_bind_tools_accepts_and_preserves_streaming():
    from langchain_core.tools import tool

    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    model = build_model("fake:instant")
    bound = model.bind_tools([add])
    result = bound.invoke([HumanMessage(content="hi")])
    assert isinstance(result, AIMessage)


async def test_full_agent_turn_offline(base_config, monkeypatch):
    """The whole pipeline — build_agent_async → AgentRunner.astream_events —
    must produce text and usage events with no API key configured."""
    monkeypatch.setenv("LIONCLAW_FAKE_TOKENS", "8")
    base_config.model = "fake:instant"

    from olano.core.builder import build_agent_async
    from olano.core.runner import AgentRunner

    agent = await build_agent_async(base_config)
    runner = AgentRunner(agent, base_config)

    events: dict[str, int] = {}
    text = ""
    async for etype, payload in runner.astream_events("hello", thread_id="fake-e2e"):
        events[etype] = events.get(etype, 0) + 1
        if etype == "text":
            text += payload
    assert "Fake response #0" in text
    assert events.get("usage", 0) >= 1
