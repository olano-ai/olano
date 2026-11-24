"""Ollama must survive content blocks written by a different provider.

The bug these pin: `langchain_ollama`'s message converter understands only
`text` / `tool_use` / `image_url` / v1-data blocks and raises

    ValueError: Unsupported message content type. Must either have type 'text'
                or type 'image_url' with a string 'image_url' field.

on everything else. Olano's checkpointer deliberately preserves a thread across
a model switch, so an agent moved onto `ollama:*` replays the previous
provider's `thinking` / `tool_result` / `reasoning` blocks — and the thread is
then bricked, raising on every turn rather than once.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from olano.models.ollama_compat import chat_ollama_class, sanitize_content, sanitize_messages

# Content shapes a real olano thread holds. Each one raises inside the
# unpatched converter; `id` names where it comes from.
UNSUPPORTED_CONTENT: list[tuple[str, list]] = [
    (
        "anthropic_thinking",
        [
            {"type": "thinking", "thinking": "let me see", "signature": "sig"},
            {"type": "text", "text": "hi"},
        ],
    ),
    ("anthropic_redacted_thinking", [{"type": "redacted_thinking", "data": "xx"}]),
    ("openai_reasoning", [{"type": "reasoning", "reasoning": "…"}, {"type": "text", "text": "hi"}]),
    ("anthropic_tool_result", [{"type": "tool_result", "tool_use_id": "1", "content": "ok"}]),
    (
        "olano_attachment_image",
        [
            {"type": "text", "text": "what is this?"},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
            },
        ],
    ),
    ("v1_tool_call", [{"type": "tool_call", "name": "ls", "args": {}, "id": "1"}]),
    ("v1_non_standard", [{"type": "non_standard", "value": {"kind": "whatever"}}]),
    (
        "server_tool_use",
        [{"type": "server_tool_use", "id": "1", "name": "web_search", "input": {}}],
    ),
    ("image_url_bad_payload", [{"type": "image_url", "image_url": {"detail": "high"}}]),
]


@pytest.fixture
def model():
    return chat_ollama_class()(model="llama3.2")


@pytest.fixture
def raw_model():
    from langchain_ollama import ChatOllama

    return ChatOllama(model="llama3.2")


@pytest.mark.parametrize(
    "label,content", UNSUPPORTED_CONTENT, ids=[c[0] for c in UNSUPPORTED_CONTENT]
)
def test_unpatched_converter_raises(raw_model, label, content):
    """Guard the premise: without the shim every one of these is fatal.

    If upstream ever widens its vocabulary this fails loudly, which is the
    signal to re-scope the shim rather than let it rot.
    """
    with pytest.raises(ValueError, match="Unsupported message content type|Only string image_url"):
        raw_model._convert_messages_to_ollama_messages([AIMessage(content=content)])


@pytest.mark.parametrize(
    "label,content", UNSUPPORTED_CONTENT, ids=[c[0] for c in UNSUPPORTED_CONTENT]
)
def test_patched_converter_accepts(model, label, content):
    converted = model._convert_messages_to_ollama_messages([AIMessage(content=content)])
    assert len(converted) == 1


def test_thinking_is_dropped_and_text_survives(model):
    """Reasoning is not coerced into text — ollama takes none back on the wire."""
    converted = model._convert_messages_to_ollama_messages(
        [
            AIMessage(
                content=[
                    {"type": "thinking", "thinking": "private scratchpad", "signature": "s"},
                    {"type": "text", "text": "the answer is 42"},
                ]
            )
        ]
    )
    assert "the answer is 42" in converted[0]["content"]
    assert "private scratchpad" not in converted[0]["content"]


def test_anthropic_image_block_becomes_an_ollama_image(model):
    """`runtime/api.py` composes attachments Anthropic-style; keep the pixels."""
    converted = model._convert_messages_to_ollama_messages(
        [
            HumanMessage(
                content=[
                    {"type": "text", "text": "what is this?"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
                    },
                ]
            )
        ]
    )
    assert converted[0]["images"] == ["QUJD"]
    assert "what is this?" in converted[0]["content"]


def test_tool_result_degrades_to_text(model):
    converted = model._convert_messages_to_ollama_messages(
        [HumanMessage(content=[{"type": "tool_result", "tool_use_id": "1", "content": "exit 0"}])]
    )
    assert "exit 0" in converted[0]["content"]


def test_tool_calls_survive_a_content_only_rewrite(model):
    """An AIMessage whose content is pure reasoning still carries its tool call."""
    message = AIMessage(
        content=[{"type": "thinking", "thinking": "…"}],
        tool_calls=[{"name": "ls", "args": {"path": "."}, "id": "call_1", "type": "tool_call"}],
    )
    converted = model._convert_messages_to_ollama_messages([message])
    assert converted[0]["tool_calls"]
    assert converted[0]["content"] == ""


def test_string_content_is_untouched(model):
    """The common path allocates nothing — identity, not just equality."""
    messages = [SystemMessage(content="be helpful"), HumanMessage(content="hi")]
    assert sanitize_messages(messages) == messages
    for original, kept in zip(messages, sanitize_messages(messages)):
        assert kept is original


def test_supported_blocks_are_not_copied():
    content = [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": "http://x/y.png"}]
    sanitized, dropped = sanitize_content(content)
    assert sanitized is content
    assert dropped == 0


def test_v1_ai_message_is_left_to_upstream():
    """langchain_ollama unpacks its own v1 output; don't fight its converter."""
    message = AIMessage(
        content=[{"type": "reasoning", "reasoning": "…"}, {"type": "text", "text": "hi"}],
        response_metadata={"output_version": "v1", "model_provider": "ollama"},
    )
    assert sanitize_messages([message])[0] is message


def test_tool_message_list_content_still_works(model):
    converted = model._convert_messages_to_ollama_messages(
        [ToolMessage(content=[{"type": "text", "text": "ok"}], tool_call_id="1")]
    )
    assert converted[0]["role"] == "tool"
    assert "ok" in converted[0]["content"]


def test_switched_thread_replays_end_to_end(model):
    """The reported failure: a whole Anthropic-authored thread fed to ollama."""
    thread = [
        SystemMessage(content="You are lucia."),
        HumanMessage(content="Draft the partnership models."),
        AIMessage(
            content=[
                {"type": "thinking", "thinking": "consider reseller tiers", "signature": "s"},
                {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "x.md"}},
            ],
            tool_calls=[
                {"name": "read_file", "args": {"path": "x.md"}, "id": "tu_1", "type": "tool_call"}
            ],
        ),
        ToolMessage(content="file contents", tool_call_id="tu_1"),
        AIMessage(content=[{"type": "text", "text": "Here are three models."}]),
        HumanMessage(content="Now for Ben."),
    ]
    converted = model._convert_messages_to_ollama_messages(thread)
    assert len(converted) == len(thread)
    assert converted[-1]["content"] == "Now for Ben."


def test_build_model_returns_the_compat_class(monkeypatch):
    from olano.models import build_model

    model = build_model("ollama:llama3.2")
    assert type(model).__name__ == "_ChatOllamaCompat"
    with pytest.raises(ValueError, match="Unsupported message content type"):
        type(model).__mro__[1]._convert_messages_to_ollama_messages(
            model, [AIMessage(content=[{"type": "thinking", "thinking": "x"}])]
        )
    assert model._convert_messages_to_ollama_messages(
        [AIMessage(content=[{"type": "thinking", "thinking": "x"}])]
    )
