"""Tests for the central tool error boundary (``guard_tool``/``guard_tools``).

The boundary must turn any runtime exception a tool body raises into an
actionable tool *result* (so the model can recover) — except graph control-flow
signals, which must keep bubbling up so human-in-the-loop still works.
"""

from __future__ import annotations

import pytest
from langchain_core.tools import BaseTool, tool

from olano.tools._error_boundary import TOOL_ERROR_MARKER, guard_tool, guard_tools


@tool
def boom(x: str) -> str:
    """Always explodes with a generic error."""
    raise RuntimeError("kaboom in tool body")


@tool
def ok(x: str) -> str:
    """Echoes its input back."""
    return f"ok:{x}"


def test_raising_tool_returns_actionable_message_not_exception():
    wrapped = guard_tool(boom)
    out = wrapped.invoke({"x": "hi"})
    assert isinstance(out, str)
    assert out.startswith("⚠️ Tool 'boom' failed")
    assert "RuntimeError" in out and "kaboom in tool body" in out
    # It tells the model this is recoverable and offers options.
    assert "recoverable" in out.lower()
    assert "retry" in out.lower()


async def test_raising_tool_is_caught_on_async_path_too():
    wrapped = guard_tool(boom)
    out = await wrapped.ainvoke({"x": "hi"})
    assert isinstance(out, str)
    assert out.startswith("⚠️ Tool 'boom' failed")


def test_message_starts_with_the_shared_marker():
    """The runner's circuit breaker keys off this exact prefix — keep them in sync."""
    out = guard_tool(boom).invoke({"x": "hi"})
    assert out.startswith(TOOL_ERROR_MARKER)


def test_success_passes_through_unchanged():
    wrapped = guard_tool(ok)
    assert wrapped.invoke({"x": "hi"}) == "ok:hi"


def test_tool_identity_preserved():
    wrapped = guard_tool(ok)
    assert wrapped.name == "ok"
    assert "Echoes" in wrapped.description
    assert isinstance(wrapped, BaseTool)


def test_args_schema_preserved_so_the_model_sees_the_same_parameters():
    import json

    @tool
    def searchit(query: str, limit: int = 5) -> str:
        """Search for things."""
        return "ok"

    wrapped = guard_tool(searchit)
    assert wrapped.args.keys() == searchit.args.keys()
    # types/defaults survive too (not just the names)
    assert json.dumps(wrapped.args) == json.dumps(searchit.args)


def test_metadata_preserved():
    ok.metadata = {"trusted": False, "src": "mcp"}
    try:
        wrapped = guard_tool(ok)
        assert wrapped.metadata.get("trusted") is False
        assert wrapped.metadata.get("src") == "mcp"
    finally:
        ok.metadata = None


def test_control_flow_exceptions_bubble_up():
    """Graph interrupts/commands must NOT be swallowed into a tool message."""
    from langgraph.errors import GraphBubbleUp

    @tool
    def interrupts(x: str) -> str:
        """Raises a graph control-flow signal (as an interrupt would)."""
        raise GraphBubbleUp()

    wrapped = guard_tool(interrupts)
    with pytest.raises(GraphBubbleUp):
        wrapped.invoke({"x": "hi"})


def test_double_wrap_is_idempotent():
    wrapped = guard_tool(boom)
    assert guard_tool(wrapped) is wrapped


def test_non_basetool_passes_through():
    def plain():
        return 1

    assert guard_tool(plain) is plain
    assert guard_tool("not a tool") == "not a tool"


def test_guard_tools_preserves_order_and_wraps_each():
    tools = guard_tools([boom, ok])
    assert [t.name for t in tools] == ["boom", "ok"]
    assert tools[0].invoke({"x": "a"}).startswith("⚠️")
    assert tools[1].invoke({"x": "a"}) == "ok:a"


@pytest.mark.parametrize(
    ("message", "needle"),
    [
        ("HTTP 429 Too Many Requests", "rate-limited"),
        ("connection timed out after 30s", "timed out"),
        ("401 Unauthorized: invalid api key", "auth/permission"),
        ("no such file: /tmp/x.json", "wasn't found"),
        ("Connection reset by peer", "network error"),
        ("HTTP 400: validation_error, field is required", "validation / bad-request"),
        ("Unprocessable Entity: invalid enum value", "validation / bad-request"),
    ],
)
def test_recovery_hint_is_tailored_by_category(message: str, needle: str):
    @tool
    def failing(x: str) -> str:
        """Raises with a category-specific message."""
        raise RuntimeError(message)

    out = guard_tool(failing).invoke({"x": "hi"})
    assert needle in out


def test_notion_validation_error_steers_away_from_blind_retry():
    """A deterministic 400 (e.g. Notion 'No matches found') must NOT be sold as
    transient — the model should be told to fix its arguments, not retry."""
    from langchain_core.tools.base import ToolException

    # The exact shape the Notion MCP server returned in the incident.
    msg = (
        '{"name":"APIResponseError","code":"validation_error","status":400,'
        '"message":"No matches found for ## Production record"}'
    )

    @tool
    def notion_update_page(x: str) -> str:
        """Mimics the Notion MCP update-page 400 from the incident."""
        raise ToolException(msg)

    out = guard_tool(notion_update_page).invoke({"x": "hi"})
    assert out.startswith(TOOL_ERROR_MARKER)
    low = out.lower()
    assert "validation / bad-request" in low
    assert "do not retry unchanged" in low
    # It must not tell the model this may clear on its own.
    assert "may be transient" not in low


def test_auth_failure_on_a_connector_backed_tool_is_repaired_or_explained_not_relayed():
    """A 401 that escapes a connector toolkit's body goes through the refusal repair.

    With no Salesforce connection stored at all, nothing can be refreshed, so
    the boundary returns the [connector-auth] result that names the connect
    route instead of "check the credential". (The refresh-and-retry paths are
    covered in tests/test_connector_auth_recovery.py.)
    """

    @tool
    def sf_query(soql: str) -> str:
        """Mimics a Salesforce tool whose 401 escaped its own handler."""
        raise RuntimeError("401 Unauthorized: Session expired or invalid (INVALID_SESSION_ID)")

    out = guard_tool(sf_query).invoke({"soql": "SELECT Id FROM Account"})
    assert out.startswith("[connector-auth]")
    assert 'magic_link_connect_service("salesforce")' in out
    assert "No Salesforce connection is stored" in out
    assert "Do not stop at reporting the error" in out
    assert "auth/permission problem" not in out


def test_auth_failure_on_a_key_tool_still_offers_a_fix_from_the_chat():
    @tool
    def weather_lookup(city: str) -> str:
        """A keyed toolkit, no connector behind it."""
        raise RuntimeError("401 Unauthorized: invalid api key")

    out = guard_tool(weather_lookup).invoke({"city": "Lima"})
    assert out.startswith(TOOL_ERROR_MARKER)
    assert "auth/permission problem" in out
    assert "self_find_integration" in out
    assert "magic_link_request_secret" in out
