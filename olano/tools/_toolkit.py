"""Standard toolkit API.

Every per-service toolkit module in ``olano/tools/`` exposes a uniform
import surface:

    >>> from olano.tools import yahoo_tools
    >>> yahoo_tools.get_tools()  # all tools in this toolkit
    >>> yahoo_tools.get_tools(["yahoo_get_quote"])
    >>> yahoo_tools.get_tool("yahoo_get_quote")

The convention is one line at the bottom of each toolkit file:

    get_tools, get_tool = make_toolkit_api(globals())

``make_toolkit_api`` walks the module globals once, snapshots every
``BaseTool`` instance (the object ``@langchain_core.tools.tool`` produces),
and returns the two helper closures bound to that snapshot.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool


def make_toolkit_api(
    module_globals: dict[str, Any],
) -> tuple[Callable[..., list[Any]], Callable[[str], Any]]:
    """Return ``(get_tools, get_tool)`` bound to the BaseTool instances in scope."""
    tools: list[Any] = [v for v in module_globals.values() if isinstance(v, BaseTool)]
    by_name: dict[str, Any] = {t.name: t for t in tools}

    def get_tools(tool_names: list[str] | None = None) -> list[Any]:
        """Return tools. If ``tool_names`` is None, returns every tool in the toolkit."""
        if tool_names is None:
            return list(tools)
        return [by_name[n] for n in tool_names if n in by_name]

    def get_tool(toolname: str) -> Any | None:
        """Return a single tool by name, or None if it's not in this toolkit."""
        return by_name.get(toolname)

    return get_tools, get_tool
