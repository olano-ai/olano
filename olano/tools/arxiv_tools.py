"""ArXiv toolkit — search and fetch academic papers.

No API key required. Uses the public arxiv.org REST API via the ``arxiv``
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
def arxiv_search(
    query: Annotated[str, "Search query (free text, supports field prefixes like ti:, au:, cat:)"],
    limit: Annotated[int, "Max results"] = 10,
    sort_by: Annotated[str, "'relevance', 'lastUpdatedDate', or 'submittedDate'"] = "relevance",
) -> str:
    """Search ArXiv for academic papers."""
    arxiv, err = optional_import("arxiv", purpose="ArXiv paper search")
    if err:
        return err
    assert arxiv is not None  # narrowed: err is None ⇒ import succeeded
    try:
        sort_map = {
            "relevance": arxiv.SortCriterion.Relevance,
            "lastUpdatedDate": arxiv.SortCriterion.LastUpdatedDate,
            "submittedDate": arxiv.SortCriterion.SubmittedDate,
        }
        search = arxiv.Search(
            query=query,
            max_results=limit,
            sort_by=sort_map.get(sort_by, arxiv.SortCriterion.Relevance),
        )
        client = arxiv.Client()
        results = list(client.results(search))
        if not results:
            return f"No ArXiv results for '{query}'."
        lines = [f"ArXiv search '{query}' ({len(results)}):"]
        for r in results:
            authors = ", ".join(a.name for a in (r.authors or [])[:3])
            if r.authors and len(r.authors) > 3:
                authors += f", +{len(r.authors) - 3}"
            published = r.published.strftime("%Y-%m-%d") if r.published else "?"
            lines.append(
                f"- [{r.get_short_id()}] {r.title}\n  {authors} | {published}\n  {r.entry_id}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] arxiv_search: {exc}"


@tool
def arxiv_get_paper(
    arxiv_id: Annotated[str, "ArXiv ID (e.g. '2310.06825' or '2310.06825v2')"],
    include_abstract: Annotated[bool, "Include full abstract"] = True,
) -> str:
    """Fetch metadata + abstract for a single ArXiv paper by ID."""
    arxiv, err = optional_import("arxiv", purpose="ArXiv paper fetch")
    if err:
        return err
    assert arxiv is not None  # narrowed: err is None ⇒ import succeeded
    try:
        search = arxiv.Search(id_list=[arxiv_id])
        client = arxiv.Client()
        results = list(client.results(search))
        if not results:
            return f"No ArXiv paper with id '{arxiv_id}'."
        r = results[0]
        authors = ", ".join(a.name for a in (r.authors or []))
        cats = ", ".join(r.categories or [])
        body = (
            f"# {r.title}\n"
            f"ID: {r.get_short_id()}\n"
            f"Authors: {authors}\n"
            f"Categories: {cats}\n"
            f"Published: {r.published}\n"
            f"PDF: {r.pdf_url}\n"
            f"URL: {r.entry_id}"
        )
        if include_abstract:
            body += f"\n\n## Abstract\n{r.summary}"
        return body
    except Exception as exc:
        return f"[tool-error] arxiv_get_paper: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
