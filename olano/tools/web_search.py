"""Web search and extraction tools.

Managed deployments search through the **Olano web service** (Parallel, via
``POST {cloud}/api/v1/services/search`` — billed in olano credits, no search
API key on this box); everyone else uses Tavily with their own key. A BYOK
Tavily key on a managed box wins: the customer choosing to pay a provider
directly is always honored.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, cast

from langchain_core.tools import tool

from olano.tools._credentials import get_credential

# The Tavily client class, resolved on first use rather than at import. This is
# a universal default tool, so the module loads into every agent build — and
# ``tavily/__init__`` eagerly imports its ``hybrid_rag`` extra, which drags in
# cohere + tokenizers + fastavro (~7.8 MB RSS) for a BYOK path a managed box
# never takes. Kept as a module attribute (not just a closure) because tests
# patch ``olano.tools.web_search.TavilyClient`` to stub the network.
TavilyClient: Any = None
_TAVILY_IMPORT_FAILED = False


def _tavily_client_class() -> Any:
    """Return the Tavily client class, importing it on first call; None if absent."""
    global TavilyClient, _TAVILY_IMPORT_FAILED
    if TavilyClient is not None:
        return TavilyClient
    if _TAVILY_IMPORT_FAILED:
        return None
    try:
        from tavily import TavilyClient as _client
    except ImportError:
        _TAVILY_IMPORT_FAILED = True
        return None
    TavilyClient = _client
    return TavilyClient


logger = logging.getLogger(__name__)

_WEB_SEARCH_DISABLED = (
    "Error: web search is turned off for this box (dashboard Config → Media & "
    "search). Set it to Auto, Olano, or Tavily to enable it."
)
_OLANO_WEB_UNAVAILABLE = (
    "Error: the Olano web search service is not available on this deployment "
    "(not connected to cloud.olano.ai, or not entitled). Choose Tavily in "
    "Config → Media & search and set TAVILY_API_KEY, or set it to Auto."
)


def _web_search_source() -> str:
    """The operator's web-search source: "" (auto) | olano | tavily | disabled.

    The dashboard Config → Media & search selection. Auto keeps the historical
    rule (BYOK Tavily wins, else the managed service). Never raises.
    """
    try:
        from olano.config.system_settings import load_web_search_settings

        return (load_web_search_settings().provider or "").strip().lower()
    except Exception:  # pragma: no cover - defensive
        return ""


def _record_service(
    service: str, *, provider: str, managed: bool, data: dict[str, Any] | None = None
) -> None:
    """Mirror one web-service call into the analytics service ledger.

    Web search/fetch spend olano credits without producing a token, so the LLM
    usage ledger never sees them and Insights would otherwise under-report a
    credits account's real burn. ``credits_charged`` is the cloud's own figure
    (absent on the BYOK path, where Olano sells nothing).
    """
    try:
        from olano.analytics.service_ledger import record_service_event

        charged = (data or {}).get("credits_charged")
        record_service_event(
            service,
            provider=provider,
            managed=managed,
            credits=float(charged) if isinstance(charged, int | float) else None,
        )
    except Exception:  # pragma: no cover - accounting must never break a search
        logger.debug("service ledger: web call not recorded", exc_info=True)


def _managed_search(query: str, max_results: int) -> str | None:
    """Search via the Olano service, or None to fall through to Tavily.

    Only a *service-level* refusal falls through silently; an exhausted credit
    balance surfaces as an actionable message (falling back would hide that
    the deployment stopped paying for a metered feature it relies on).
    """
    from olano.deployment.services_client import (
        ServiceUnavailable,
        service_search,
        services_available,
    )

    if not services_available():
        return None
    try:
        data = service_search(query=query, max_results=max_results)
    except ServiceUnavailable as exc:
        if exc.code == "credits_exhausted":
            return f"Error: {exc}"
        logger.debug("managed web search unavailable (%s); falling back", exc.code)
        return None

    _record_service("web_search", provider="olano", managed=True, data=data)
    results = data.get("results") or []
    if not results:
        return "No results found."
    lines: list[str] = []
    for i, r in enumerate(results, 1):
        title = r.get("title") or "No title"
        url = r.get("url", "")
        excerpts = r.get("excerpts") or []
        content = " ".join(str(e) for e in excerpts).strip().replace("\n", " ")
        if len(content) > 220:
            content = content[:219] + "…"
        lines.append(f"[{i}] **{title}**\n    URL: {url}\n    {content}")
    return "\n\n".join(lines)


def _web_search_impl(
    query: Annotated[str, "Search query"],
    max_results: Annotated[int, "Number of results to return (1-10)"] = 3,
    topic: Annotated[str, "Search topic: 'general', 'news', or 'finance'"] = "general",
) -> str:
    source = _web_search_source()
    if source == "disabled":
        return _WEB_SEARCH_DISABLED
    # BYOK key first (an explicit customer choice), then the managed service —
    # unless the operator pinned a source in Config → Media & search.
    api_key = get_credential("TAVILY_API_KEY")
    if source == "olano" or (source == "" and not api_key):
        managed = _managed_search(query, max_results)
        if managed is not None:
            return managed
        if source == "olano":
            return _OLANO_WEB_UNAVAILABLE

    client_cls = _tavily_client_class()
    if client_cls is None:
        return "Error: tavily-python not installed. Run: uv add tavily-python"

    if not api_key:
        return "Error: TAVILY_API_KEY environment variable not set."

    max_results = max(1, min(10, max_results))
    client = client_cls(api_key=api_key)

    try:
        response = client.search(
            query=query,
            max_results=max_results,
            topic=topic,
            include_answer=True,
        )
    except Exception as e:
        return f"Error performing search: {e}"

    _record_service("web_search", provider="tavily", managed=False)
    lines: list[str] = []

    if response.get("answer"):
        answer = str(response["answer"]).strip()
        if len(answer) > 280:
            answer = answer[:279] + "…"
        lines.append(f"**Quick Answer:** {answer}\n")

    results = response.get("results", [])
    if not results:
        return "No results found."

    for i, r in enumerate(results, 1):
        title = r.get("title", "No title")
        url = r.get("url", "")
        content = str(r.get("content", "")).strip().replace("\n", " ")
        if len(content) > 220:
            content = content[:219] + "…"
        lines.append(f"[{i}] **{title}**\n    URL: {url}\n    {content}")

    return "\n\n".join(lines)


def _managed_extract(url: str) -> str | None:
    """Extract via the Olano service, or None to fall through to Tavily."""
    from olano.deployment.services_client import (
        ServiceUnavailable,
        service_fetch,
        services_available,
    )

    if not services_available():
        return None
    try:
        data = service_fetch(url)
    except ServiceUnavailable as exc:
        if exc.code == "credits_exhausted":
            return f"Error: {exc}"
        logger.debug("managed web extract unavailable (%s); falling back", exc.code)
        return None

    _record_service("web_fetch", provider="olano", managed=True, data=data)
    pages = data.get("pages") or []
    content = str(pages[0].get("content") or "") if pages else ""
    if not content:
        return f"No content could be extracted from: {url}"
    if len(content) > 20000:
        content = content[:20000] + "\n\n[... content truncated at 20k chars ...]"
    return f"# Content from {url}\n\n{content}"


def _web_extract_impl(
    url: Annotated[str, "URL to extract clean text content from"],
) -> str:
    source = _web_search_source()
    if source == "disabled":
        return _WEB_SEARCH_DISABLED
    api_key = get_credential("TAVILY_API_KEY")
    if source == "olano" or (source == "" and not api_key):
        managed = _managed_extract(url)
        if managed is not None:
            return managed
        if source == "olano":
            return _OLANO_WEB_UNAVAILABLE

    client_cls = _tavily_client_class()
    if client_cls is None:
        return "Error: tavily-python not installed. Run: uv add tavily-python"

    if not api_key:
        return "Error: TAVILY_API_KEY environment variable not set."

    client = client_cls(api_key=api_key)

    try:
        response = client.extract(urls=[url])
    except Exception as e:
        return f"Error extracting URL: {e}"

    _record_service("web_fetch", provider="tavily", managed=False)
    results = response.get("results", [])
    if not results:
        return f"No content could be extracted from: {url}"

    r = results[0]
    content = r.get("raw_content") or r.get("content", "")
    if not content:
        return f"Empty content returned for: {url}"

    # Truncate if very long
    if len(content) > 20000:
        content = content[:20000] + "\n\n[... content truncated at 20k chars ...]"

    return f"# Content from {url}\n\n{content}"


@tool
def web_search(
    query: Annotated[str, "Search query"],
    max_results: Annotated[int, "Number of results to return (1-10)"] = 3,
    topic: Annotated[str, "Search topic: 'general', 'news', or 'finance'"] = "general",
) -> str:
    """
    Search the open web for candidate sources.
    Use this first when you need discovery, comparison, or a shortlist of relevant pages.
    """
    return _web_search_impl(query=query, max_results=max_results, topic=topic)


@tool
def page_extract(
    url: Annotated[str, "URL to extract clean text content from"],
) -> str:
    """
    Perform a slower, higher-recall extraction of one page.
    Use this after `page_read` when the fast read looks incomplete or badly structured.
    """
    return _web_extract_impl(url=url)


@tool
def web_extract_deep(
    url: Annotated[str, "URL to extract clean text content from"],
) -> str:
    """
    Compatibility alias for `page_extract`.
    """
    return cast(str, cast(Any, page_extract).func(url=url))


@tool
def web_extract(
    url: Annotated[str, "URL to extract clean text content from"],
) -> str:
    """Compatibility alias for `page_extract`."""
    return cast(str, cast(Any, page_extract).func(url=url))


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
