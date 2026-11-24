"""The security middleware's hooks write one message, never the conversation.

LangGraph persists a hook's return value as that step's write, and deepagents
stores ``messages`` as a delta channel that replays every write since the last
snapshot on each load. The hooks used to return the whole message list whenever
they rewrote one message, so on a thread with months of history every step
wrote the entire conversation (42 MB per step on a managed box's peer thread)
and every load replayed up to 50 of those.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from olano.config.agent_config import AgentConfig
from olano.security.deepagents_middleware import OlanoSecurityMiddleware
from olano.security.request_context import push_request_context


def _config(**security: object) -> AgentConfig:
    base = {
        "input_scan": False,
        "output_scan": False,
        "credential_scrub": False,
        "pii_scan_input": False,
        "pii_scan_output": False,
        "profile": "developer",
    }
    base.update(security)
    return AgentConfig(name="write-size", model="fake:instant", security=base)


def _history(n: int) -> list:
    out: list = []
    for i in range(n):
        out.append(HumanMessage(content=f"question {i}", id=f"h{i}"))
        out.append(AIMessage(content=f"answer {i}", id=f"a{i}"))
        out.append(ToolMessage(content="ok", tool_call_id=f"c{i}", id=f"t{i}"))
    return out


def test_input_redaction_writes_only_the_rewritten_human_message() -> None:
    mw = OlanoSecurityMiddleware(
        config=_config(pii_scan_input=True, pii_action="redact"), workspace_root=Path()
    )
    messages = [*_history(40), HumanMessage(content="reach me at foo@bar.com", id="last")]
    with push_request_context(source="api", channel="api", user_id="u"):
        update = mw.before_model({"messages": messages}, None)
    assert update is not None
    assert len(update["messages"]) == 1
    (replaced,) = update["messages"]
    assert replaced.id == "last"
    assert "foo@bar.com" not in replaced.content
    # The state itself is untouched; the reducer does the in-place merge by id.
    assert messages[-1].content == "reach me at foo@bar.com"


def test_output_scrub_writes_only_the_rewritten_ai_message() -> None:
    mw = OlanoSecurityMiddleware(config=_config(credential_scrub=True), workspace_root=Path())
    secret = "sk-" + "a" * 40
    messages = [*_history(40), AIMessage(content=f"the key is {secret}", id="reply")]
    with push_request_context(source="api", channel="api", user_id="u"):
        update = mw.after_model({"messages": messages}, None)
    assert update is not None
    assert len(update["messages"]) == 1
    assert update["messages"][0].id == "reply"
    assert secret not in update["messages"][0].content


async def test_async_hooks_write_one_message_too() -> None:
    mw = OlanoSecurityMiddleware(
        config=_config(pii_scan_input=True, pii_scan_output=True, pii_action="redact"),
        workspace_root=Path(),
    )
    messages = [*_history(10), HumanMessage(content="foo@bar.com wrote", id="last")]
    with push_request_context(source="api", channel="api", user_id="u"):
        update = await mw.abefore_model({"messages": messages}, None)
    assert update is not None and [m.id for m in update["messages"]] == ["last"]

    messages = [*_history(10), AIMessage(content="contact foo@bar.com", id="reply")]
    with push_request_context(source="api", channel="api", user_id="u"):
        update = await mw.aafter_model({"messages": messages}, None)
    assert update is not None and [m.id for m in update["messages"]] == ["reply"]


def test_nothing_to_rewrite_writes_nothing() -> None:
    mw = OlanoSecurityMiddleware(
        config=_config(pii_scan_input=True, pii_scan_output=True, credential_scrub=True),
        workspace_root=Path(),
    )
    messages = _history(5)
    with push_request_context(source="api", channel="api", user_id="u"):
        assert mw.before_model({"messages": messages}, None) is None
        assert mw.after_model({"messages": messages}, None) is None
        assert mw.before_model({"messages": []}, None) is None


def test_message_without_id_falls_back_to_the_full_list() -> None:
    """A message with no id cannot be merged in place, so the whole list goes:
    only a hand-built state has one (LangGraph stamps every stored message)."""
    mw = OlanoSecurityMiddleware(
        config=_config(pii_scan_input=True, pii_action="redact"), workspace_root=Path()
    )
    messages = [AIMessage(content="hi", id="a0"), HumanMessage(content="foo@bar.com")]
    with push_request_context(source="api", channel="api", user_id="u"):
        update = mw.before_model({"messages": messages}, None)
    assert update is not None
    assert len(update["messages"]) == 2
    assert update["messages"][0] is messages[0]
    assert "foo@bar.com" not in update["messages"][1].content


@pytest.mark.parametrize("hook", ["before_model", "after_model"])
def test_reducer_merges_the_one_message_update_in_place(hook: str) -> None:
    """What the graph does with the update: deepagents' reducer replaces by id."""
    from deepagents._messages_reducer import _messages_delta_reducer

    mw = OlanoSecurityMiddleware(
        config=_config(pii_scan_input=True, pii_scan_output=True, pii_action="redact"),
        workspace_root=Path(),
    )
    messages = _history(3)
    if hook == "before_model":
        messages.append(HumanMessage(content="mail foo@bar.com", id="x"))
    else:
        messages.append(AIMessage(content="mail foo@bar.com", id="x"))
    with push_request_context(source="api", channel="api", user_id="u"):
        update = getattr(mw, hook)({"messages": messages}, None)
    merged = _messages_delta_reducer(messages, [update["messages"]])
    assert len(merged) == len(messages)
    assert merged[-1].id == "x"
    assert "foo@bar.com" not in merged[-1].content
    assert [m.id for m in merged[:-1]] == [m.id for m in messages[:-1]]
