"""Enforce ``disabled_tools`` at the model-bind boundary.

``config.disabled_tools`` has always removed the tools olano itself
assembles: ``builder._build_extra_tools`` filters its merge (config extras,
memory/RAG/graph/notes, ``workspace_context``, ``whoami``), and the
``write_todos`` / ``compact_conversation`` middlewares simply aren't wired
when their name is disabled. But the tools deepagents attaches on its own —
the backend filesystem/shell primitives (``read_file``/``write_file``/
``edit_file``/``ls``/``glob``/``grep``/``execute``) and the subagent ``task``
tool — are added inside ``create_deep_agent`` *after* the builder hands its
list over, so unchecking them in the dashboard used to be impossible (the
internal-tools catalog showed them locked, "for transparency, not control").

This middleware closes that gap. On every model call it filters the bound
tool list by name, so a disabled tool is never offered to the model no matter
which layer attached it. The tool object stays registered in the graph's
ToolNode (removing it there would break deepagents' wiring); what disappears
is the model's ability to call it — which is what "disabled" means for an
LLM agent. Applies to the main agent's model requests only: subagents keep
their own toolsets (a per-subagent ``disabled_tools`` already exists in the
subagent schema).
"""

from __future__ import annotations

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware

logger = logging.getLogger(__name__)


def _tool_name(tool: Any) -> str:
    """Best-effort name for a bound tool: BaseTool, plain dict schema, or an
    OpenAI-style ``{"type": "function", "function": {"name": ...}}`` wrapper."""
    name = getattr(tool, "name", None)
    if isinstance(name, str) and name:
        return name
    if isinstance(tool, dict):
        raw = tool.get("name")
        if isinstance(raw, str) and raw:
            return raw
        fn = tool.get("function")
        if isinstance(fn, dict):
            fn_name = fn.get("name")
            if isinstance(fn_name, str):
                return fn_name
    return ""


class DisabledToolsMiddleware(AgentMiddleware):
    """Drop ``disabled_tools`` names from every model request's tool list."""

    def __init__(self, disabled: set[str]) -> None:
        super().__init__()
        self._disabled = set(disabled)

    def _filter(self, request: Any) -> Any:
        tools = list(request.tools or [])
        kept = [t for t in tools if _tool_name(t) not in self._disabled]
        if len(kept) == len(tools):
            return request
        return request.override(tools=kept)

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(self._filter(request))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(self._filter(request))


def build_disabled_tools_middleware(disabled: set[str] | list[str]) -> list[AgentMiddleware]:
    """The enforcement middleware, or nothing when no tool is disabled.

    The empty-set fast path keeps the common case (no opt-outs) at zero
    per-model-call overhead.
    """
    names = {n for n in disabled if n}
    if not names:
        return []
    return [DisabledToolsMiddleware(names)]
