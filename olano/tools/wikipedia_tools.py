"""Wikipedia toolkit — search, summarize, fetch full articles.

No API key required. Uses the public Wikipedia REST API via the ``wikipedia``
Python library.
"""

from __future__ import annotations

import logging
from typing import Annotated

from langchain_core.tools import tool

from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)


@tool
def wikipedia_search(
    query: Annotated[str, "Search query"],
    limit: Annotated[int, "Max titles to return"] = 10,
    language: Annotated[str, "Wikipedia language code (e.g. 'en', 'es')"] = "en",
) -> str:
    """Search Wikipedia and return a list of matching article titles."""
    wiki, err = optional_import("wikipedia", purpose="Wikipedia knowledge lookup")
    if err:
        return err
    assert wiki is not None  # narrowed: err is None ⇒ import succeeded
    try:
        wiki.set_lang(language)
        results = wiki.search(query, results=limit)
        if not results:
            return f"No Wikipedia results for '{query}'."
        return "\n".join(f"- {t}" for t in results)
    except Exception as exc:
        return f"[tool-error] wikipedia_search: {exc}"


@tool
def wikipedia_summary(
    title: Annotated[str, "Wikipedia article title"],
    sentences: Annotated[int, "Number of summary sentences (1-15)"] = 5,
    language: Annotated[str, "Wikipedia language code"] = "en",
) -> str:
    """Get a concise summary of a Wikipedia article."""
    wiki, err = optional_import("wikipedia", purpose="Wikipedia knowledge lookup")
    if err:
        return err
    assert wiki is not None  # narrowed: err is None ⇒ import succeeded
    try:
        wiki.set_lang(language)
        return str(wiki.summary(title, sentences=max(1, min(sentences, 15)), auto_suggest=False))
    except Exception as exc:
        DisambiguationError = getattr(wiki.exceptions, "DisambiguationError", None)
        if DisambiguationError and isinstance(exc, DisambiguationError):
            options = list(getattr(exc, "options", []))[:10]
            return (
                f"[tool-error] wikipedia_summary: '{title}' is ambiguous. "
                f"Did you mean: {', '.join(options)}"
            )
        return f"[tool-error] wikipedia_summary: {exc}"


@tool
def wikipedia_get_page(
    title: Annotated[str, "Wikipedia article title"],
    language: Annotated[str, "Wikipedia language code"] = "en",
    max_chars: Annotated[
        int, "Truncate full content to this many chars (0 = no truncation)"
    ] = 8000,
) -> str:
    """Fetch the full content of a Wikipedia article (with URL and references count)."""
    wiki, err = optional_import("wikipedia", purpose="Wikipedia knowledge lookup")
    if err:
        return err
    assert wiki is not None  # narrowed: err is None ⇒ import succeeded
    try:
        wiki.set_lang(language)
        page = wiki.page(title, auto_suggest=False)
        content = page.content or ""
        if max_chars > 0 and len(content) > max_chars:
            content = content[:max_chars] + f"\n\n[...truncated at {max_chars} chars]"
        return (
            f"# {page.title}\nURL: {page.url}\nReferences: {len(page.references or [])}\n\n"
            f"{content}"
        )
    except Exception as exc:
        return f"[tool-error] wikipedia_get_page: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
