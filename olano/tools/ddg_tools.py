"""DuckDuckGo search toolkit.

Free, no API key required. Useful as a fallback when Tavily quota is exhausted
or for cheap/quick lookups. Returns title + URL + snippet per result.
"""

from __future__ import annotations

import logging
from typing import Annotated

from langchain_core.tools import tool

from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)


@tool
def ddg_search(
    query: Annotated[str, "Search query"],
    max_results: Annotated[int, "Number of results (1-25)"] = 10,
    region: Annotated[str, "Region code (e.g. 'us-en', 'wt-wt' for worldwide)"] = "wt-wt",
    timelimit: Annotated[
        str, "Time filter: '' (any), 'd' (day), 'w' (week), 'm' (month), 'y' (year)"
    ] = "",
) -> str:
    """Free, no-API-key web search via DuckDuckGo."""
    ddgs_mod, err = optional_import("ddgs", purpose="DuckDuckGo search")
    if err:
        # Fall back to the older package name
        ddgs_mod, err2 = optional_import("duckduckgo_search", purpose="DuckDuckGo search")
        if err2:
            return err
    assert ddgs_mod is not None
    try:
        DDGS = ddgs_mod.DDGS
        max_results = max(1, min(max_results, 25))
        with DDGS() as ddgs:
            results = list(
                ddgs.text(
                    query,
                    region=region,
                    timelimit=timelimit or None,
                    max_results=max_results,
                )
            )
        if not results:
            return f"No DuckDuckGo results for '{query}'."
        lines = [f"DuckDuckGo '{query}' ({len(results)}):"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "?")
            url = r.get("href") or r.get("url") or ""
            body = (r.get("body") or "")[:220].replace("\n", " ")
            lines.append(f"[{i}] {title}\n    URL: {url}\n    {body}")
        return "\n\n".join(lines)
    except Exception as exc:
        return f"[tool-error] ddg_search: {exc}"


@tool
def ddg_news(
    query: Annotated[str, "News search query"],
    max_results: Annotated[int, "Number of results (1-25)"] = 10,
    timelimit: Annotated[str, "'' (any), 'd' (day), 'w' (week), 'm' (month)"] = "",
) -> str:
    """News-specific DuckDuckGo search returning recent articles with source + date."""
    ddgs_mod, err = optional_import("ddgs", purpose="DuckDuckGo news search")
    if err:
        ddgs_mod, err2 = optional_import("duckduckgo_search", purpose="DuckDuckGo news search")
        if err2:
            return err
    assert ddgs_mod is not None
    try:
        DDGS = ddgs_mod.DDGS
        max_results = max(1, min(max_results, 25))
        with DDGS() as ddgs:
            results = list(ddgs.news(query, timelimit=timelimit or None, max_results=max_results))
        if not results:
            return f"No DuckDuckGo news for '{query}'."
        lines = [f"DuckDuckGo news '{query}' ({len(results)}):"]
        for i, r in enumerate(results, 1):
            date = (r.get("date") or "")[:19]
            source = r.get("source", "?")
            title = r.get("title", "?")
            url = r.get("url", "")
            body = (r.get("body") or "")[:200].replace("\n", " ")
            lines.append(f"[{i}] [{date}] {source} | {title}\n    URL: {url}\n    {body}")
        return "\n\n".join(lines)
    except Exception as exc:
        return f"[tool-error] ddg_news: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
