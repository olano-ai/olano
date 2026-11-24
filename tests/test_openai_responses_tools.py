"""Function tools on the OpenAI Responses API default to ``strict: false``.

The Responses API reads a missing ``strict`` as strict, which makes every
optional property required. Lusha's ``contacts_search`` (optional fields that
are mutually exclusive) then got placeholder values beside ``contacts`` and
refused the mixed call. Both OpenAI-backed providers take the same subclass.
"""

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool

from olano.models import _default_tools_non_strict, build_model

TOOL = StructuredTool(
    name="contacts_search",
    description="Look up contacts.",
    args_schema={"type": "object", "properties": {"email": {"type": "string"}}},
    func=lambda **_: "ok",
)


def _sent_strict(bound) -> object:
    payload = bound.bound._get_request_payload([HumanMessage(content="hi")], **bound.kwargs)
    return payload["tools"][0].get("strict", "<absent>")


def test_openai_provider_sends_tools_non_strict_unless_chosen(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    model = build_model("openai:gpt-5")
    assert model.use_responses_api is True
    assert _sent_strict(model.bind_tools([TOOL])) is False
    assert _sent_strict(model.bind_tools([TOOL], strict=True)) is True


def test_chat_completions_tool_shape_is_left_alone():
    # Chat Completions nests the definition under ``function`` and already
    # defaults to non-strict; a stray top-level key there would be rejected.
    tool = {"type": "function", "function": {"name": "x", "parameters": {}}}
    payload = {"tools": [tool]}
    _default_tools_non_strict(payload)
    assert "strict" not in tool
    assert "strict" not in tool["function"]
