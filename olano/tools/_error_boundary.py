"""Central error boundary that stops a raising tool from crashing the agent turn.

Why this exists
---------------
LangGraph's ``ToolNode`` only turns *argument-validation* failures
(``ToolInvocationError``) into a tool message the model can read. Its default
handler **re-raises every other exception** a tool body throws — a network
timeout, an API 401, a ``FileNotFoundError``, a ``KeyError`` — straight out of
the graph, where olano surfaces it as a fatal ``AgentError`` in chat. The
model never sees the failure, so it can't retry, adjust its arguments, try a
different tool, or explain the problem to the user.

``guard_tool`` wraps a tool so any runtime exception in its body becomes a
concise, *actionable* tool result instead: **what** failed and **what the model
can do about it**. Graph control-flow signals (interrupts / commands used for
human-in-the-loop) are re-raised untouched so HITL keeps working, and
cancellation (``asyncio.CancelledError``, a ``BaseException``) is never caught.

This is applied once, centrally, where tools are handed to
``create_deep_agent`` (``builder._create_agent_graph`` and the sub-agent
builders) so it covers every toolkit, MCP tool, and future tool uniformly —
without per-tool boilerplate. Individual tools that already return their own
friendly error strings keep doing so; the boundary only catches what escapes.

It is also where a **refused sign-in** is repaired rather than relayed. A tool
that returns ``[tool-error] github_list_repos: 401 Bad credentials`` (or raises
the same) has told us the saved connection is dead while the Connections card
still says *Connected*; the boundary hands the outcome to
``tools/_connector_auth.py``, which knows the service behind the tool (a
connector toolkit, Google, an OAuth MCP server), refreshes its token, retries
the call once, and otherwise marks the connection and returns the
``[connector-auth]`` result that tells the model to *offer* the reconnect.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

logger = logging.getLogger(__name__)

# Marker so a tool is never double-wrapped (idempotent, like the security wrap).
_WRAPPED_FLAG = "_lionclaw_error_boundary_wrapped"

# Stable prefix every boundary message starts with. The runner's tool-error
# circuit breaker (``AgentRunner.astream_events``) keys off this to recognise a
# guarded failure — a boundary result carries ``status="success"`` (it IS a
# normal tool message), so without the marker a tool that raises identically
# forever would never trip the "unproductive loop" stop. Keep the two in sync.
TOOL_ERROR_MARKER = "⚠️ Tool '"

# Graph control-flow signals MUST bubble up to the ToolNode, never be turned into
# a tool message — otherwise interrupts / human-in-the-loop / parent commands
# silently break. ``GraphInterrupt`` subclasses ``GraphBubbleUp`` (both are
# ``Exception`` subclasses, so a bare ``except Exception`` would swallow them).
try:  # pragma: no cover - import guarded against langgraph internals moving
    from langgraph.errors import GraphBubbleUp, ParentCommand

    _CONTROL_FLOW: tuple[type[BaseException], ...] = (GraphBubbleUp, ParentCommand)
except Exception:  # pragma: no cover
    _CONTROL_FLOW = ()


def _clip(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _recovery_hint(name: str, low: str) -> str:
    """A short, model-facing suggestion for how to recover, by rough category."""
    if any(k in low for k in ("rate limit", "ratelimit", "429", "too many requests")):
        return (
            "You're being rate-limited. Wait a few seconds before retrying, and "
            "space out further calls to this tool."
        )
    if any(k in low for k in ("timed out", "timeout")):
        return (
            "The call timed out. Retry once; if it keeps timing out, make a smaller "
            "request or try a different approach."
        )
    if any(
        k in low
        for k in (
            "401",
            "403",
            "unauthorized",
            "forbidden",
            "invalid api key",
            "api key",
            "permission denied",
            "authentication",
            "not authenticated",
            "access denied",
        )
    ):
        # A connector-backed tool never reaches this hint with a refused
        # sign-in: `_repair` above already refreshed, retried or explained it.
        return (
            f"This looks like an auth/permission problem for '{name}'. The credential "
            "is likely missing, wrong, or lacks the needed scope — don't retry "
            "blindly. Tell the user which key or connection is at fault AND offer to fix "
            "it from the chat: self_find_integration names the connect route for the "
            "service, magic_link_connect_service renews a sign-in, and "
            "magic_link_request_secret collects a replacement key."
        )
    if any(k in low for k in ("quota", "insufficient", "billing", "payment required")):
        return (
            "A quota or billing limit was hit. This won't clear on retry — tell the "
            "user the account needs attention."
        )
    if any(
        k in low
        for k in ("not found", "404", "no such file", "does not exist", "no such", "notfound")
    ):
        return (
            "The target wasn't found. Re-check the id / path / name you passed; list "
            "or search first if you're unsure it exists."
        )
    if any(
        k in low
        for k in ("connection", "network", "dns", "unreachable", "ssl", "econnreset", "socket")
    ):
        return (
            "This is a network error. Retry once; if it persists the remote service "
            "may be down — let the user know rather than looping."
        )
    # Kept last of the specific categories: a deterministic client-side rejection
    # (400/422/validation/"no matches found"/"only one … allowed"). Checked after
    # network so an "invalid SSL certificate" still reads as a network error, and
    # after auth/not-found so those keep their tailored hints.
    if any(
        k in low
        for k in (
            "validation",
            "bad request",
            "400",
            "422",
            "unprocessable",
            "no matches found",
            "no match found",
            "malformed",
            "invalid",
        )
    ):
        return (
            "The request was rejected as invalid (a validation / bad-request error), "
            "not a transient failure — retrying the same arguments will fail "
            "identically. Read the error detail for the field or value it objects to, "
            "then fix that specific argument (e.g. split one call per item if the API "
            "allows only one). If you were editing or replacing existing content, "
            "re-read the target's current state first so your input matches it exactly. "
            "Do not retry unchanged."
        )
    return (
        "This may be transient. You can retry, adjust the arguments, try a different "
        "tool, or explain the problem to the user and ask how to proceed."
    )


def _error_message(name: str, exc: Exception) -> str:
    etype = type(exc).__name__
    detail = _clip(str(exc)) or etype
    hint = _recovery_hint(name, f"{etype} {detail}".lower())
    return (
        f"{TOOL_ERROR_MARKER}{name}' failed — {etype}: {detail}\n"
        f"This is recoverable, not a crash. {hint}"
    )


def guard_tool(tool: Any) -> Any:
    """Return a tool whose runtime exceptions become actionable tool results.

    Non-``BaseTool`` inputs and already-guarded tools are returned unchanged.
    On success the underlying tool's result is passed through verbatim (including
    ``Command`` state updates); only an exception is converted to a message.
    """
    if getattr(tool, _WRAPPED_FLAG, False):
        return tool
    if not isinstance(tool, BaseTool) or not hasattr(tool, "invoke"):
        return tool

    name = getattr(tool, "name", getattr(tool, "__name__", "tool"))
    metadata = dict(getattr(tool, "metadata", {}) or {})

    def _handle(exc: Exception) -> str:
        logger.warning(
            "tool %r raised %s — returning recoverable message: %s",
            name,
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return _error_message(name, exc)

    def _sync(**kwargs: Any) -> Any:
        from olano.tools._connector_auth import NOT_A_REFUSAL, repair_refusal_sync

        def _again() -> Any:
            return tool.invoke(kwargs)

        try:
            result = tool.invoke(kwargs)
        except _CONTROL_FLOW:
            raise
        except Exception as exc:  # deliberate catch-all: this IS the boundary
            try:
                repaired = repair_refusal_sync(name, metadata, exc=exc, retry=_again)
            except _CONTROL_FLOW:
                raise
            except Exception as exc2:  # the retry failed some other way
                return _handle(exc2)
            return _handle(exc) if repaired is NOT_A_REFUSAL else repaired
        try:
            repaired = repair_refusal_sync(name, metadata, result=result, retry=_again)
        except _CONTROL_FLOW:
            raise
        except Exception as exc2:
            return _handle(exc2)
        return result if repaired is NOT_A_REFUSAL else repaired

    async def _async(**kwargs: Any) -> Any:
        from olano.tools._connector_auth import NOT_A_REFUSAL, repair_refusal_async

        async def _again() -> Any:
            return await tool.ainvoke(kwargs)

        try:
            result = await tool.ainvoke(kwargs)
        except _CONTROL_FLOW:
            raise
        except Exception as exc:  # deliberate catch-all: this IS the boundary
            try:
                repaired = await repair_refusal_async(name, metadata, exc=exc, retry=_again)
            except _CONTROL_FLOW:
                raise
            except Exception as exc2:  # the retry failed some other way
                return _handle(exc2)
            return _handle(exc) if repaired is NOT_A_REFUSAL else repaired
        try:
            repaired = await repair_refusal_async(name, metadata, result=result, retry=_again)
        except _CONTROL_FLOW:
            raise
        except Exception as exc2:
            return _handle(exc2)
        return result if repaired is NOT_A_REFUSAL else repaired

    wrapped = StructuredTool.from_function(
        func=_sync,
        coroutine=_async,
        name=name,
        description=getattr(tool, "description", "") or "",
        args_schema=getattr(tool, "args_schema", None),
        return_direct=getattr(tool, "return_direct", False),
    )
    # Preserve metadata (e.g. the untrusted/`trusted=false` marking on MCP tools).
    wrapped.metadata = dict(getattr(tool, "metadata", {}) or {})
    object.__setattr__(wrapped, _WRAPPED_FLAG, True)
    return wrapped


def guard_tools(tools: list[Any]) -> list[Any]:
    """Wrap every tool in ``tools`` with the error boundary (order preserved)."""
    return [guard_tool(t) for t in tools]
