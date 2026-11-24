"""Web scraping and crawling tools using Firecrawl."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import tool

from olano.config.workspace import get_workspace
from olano.security.request_context import get_request_context
from olano.tools._credentials import get_credential
from olano.tools._sandbox_exec import handoff_to_sandbox

try:
    from firecrawl import FirecrawlApp
except ImportError:
    FirecrawlApp = None  # type: ignore[assignment,misc]


def _get_client() -> Any:
    if FirecrawlApp is None:
        raise ImportError("firecrawl-py not installed. Run: uv add firecrawl-py")

    api_key = get_credential("FIRECRAWL_API_KEY")
    if not api_key:
        raise ValueError("FIRECRAWL_API_KEY environment variable not set.")

    return FirecrawlApp(api_key=api_key)


def _workspace_root() -> Path:
    """Resolve the *running agent's* workspace root from the request context.

    A bare ``get_workspace()`` resolves the legacy/global workspace; the request
    context carries the per-agent root (``~/.olano/agents/<name>/workspace``)
    so scraped output lands inside the agent's own files.
    """
    workspace_root = get_request_context().metadata.get("workspace_root")
    return get_workspace(str(workspace_root) if workspace_root else None).root


def _scraping_dir() -> Path:
    d = _workspace_root() / "web_scraping"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_filename(url: str) -> str:
    """Convert URL to a safe filename."""
    import re

    name = re.sub(r"https?://", "", url)
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    return name[:80]


@tool
def firecrawl_scrape(
    url: Annotated[str, "URL to scrape"],
    save_to_workspace: Annotated[bool, "Save result to workspace/web_scraping/"] = True,
) -> str:
    """
    Scrape a single URL and return clean markdown content.
    Optionally saves the result to workspace/web_scraping/.

    Use this only for public HTTP(S) webpages. Do not use Firecrawl for local
    files, `file://` URLs, or downloaded PDFs; use `document_to_text` or
    `pdf_to_text` for those.
    """
    if url.startswith("file://") or url.lower().split("?", 1)[0].endswith(".pdf"):
        return (
            "Error: firecrawl_scrape is for HTTP(S) webpages, not local files or PDFs. "
            "Use document_to_text or pdf_to_text on the local workspace path instead."
        )
    try:
        client = _get_client()
    except (ImportError, ValueError) as e:
        return f"Error: {e}"

    try:
        result = client.scrape_url(url, params={"formats": ["markdown"]})
    except Exception as e:
        return f"Error scraping {url}: {e}"

    markdown = ""
    if hasattr(result, "markdown"):
        markdown = result.markdown or ""
    elif isinstance(result, dict):
        markdown = result.get("markdown", "") or result.get("content", "")

    if not markdown:
        return f"No markdown content extracted from: {url}"

    if save_to_workspace:
        out_path = _scraping_dir() / f"{_safe_filename(url)}.md"
        out_path.write_text(f"# Scraped: {url}\n\n{markdown}", encoding="utf-8")
        handoff_to_sandbox(out_path, created=[out_path.parent])
        return f"Scraped {url} → saved to {out_path}\n\n{markdown[:3000]}"

    return markdown


@tool
def firecrawl_crawl(
    url: Annotated[str, "Starting URL to crawl"],
    max_pages: Annotated[int, "Maximum number of pages to crawl (1-50)"] = 10,
) -> str:
    """
    Crawl a website following links from the starting URL.
    Saves each page as markdown to workspace/web_scraping/ with an index file.
    """
    try:
        client = _get_client()
    except (ImportError, ValueError) as e:
        return f"Error: {e}"

    max_pages = max(1, min(50, max_pages))

    try:
        result = client.crawl_url(
            url,
            params={"limit": max_pages, "scrapeOptions": {"formats": ["markdown"]}},
        )
    except Exception as e:
        return f"Error crawling {url}: {e}"

    pages: list[Any] = []
    if hasattr(result, "data"):
        pages = result.data or []
    elif isinstance(result, dict):
        pages = result.get("data", [])

    if not pages:
        return f"No pages found when crawling: {url}"

    crawl_dir = _scraping_dir() / _safe_filename(url)
    crawl_dir.mkdir(parents=True, exist_ok=True)

    index_lines = [f"# Crawl index: {url}\n"]
    for i, page in enumerate(pages):
        page_url = ""
        markdown = ""
        if hasattr(page, "metadata"):
            page_url = page.metadata.get("url", "") if page.metadata else ""
            markdown = page.markdown or ""
        elif isinstance(page, dict):
            page_url = page.get("metadata", {}).get("url", "")
            markdown = page.get("markdown", "") or page.get("content", "")

        fname = f"page_{i + 1:03d}.md"
        page_path = crawl_dir / fname
        page_path.write_text(f"# {page_url}\n\n{markdown}", encoding="utf-8")
        handoff_to_sandbox(page_path, created=[crawl_dir])
        index_lines.append(f"- [{page_url}](./{fname})")

    index_path = crawl_dir / "index.md"
    index_path.write_text("\n".join(index_lines), encoding="utf-8")
    handoff_to_sandbox(index_path)

    return (
        f"Crawled {len(pages)} pages from {url}\n"
        f"Saved to: {crawl_dir}\n"
        f"Index: {index_path}\n\n"
        + "\n".join(index_lines[1:11])
        + ("\n..." if len(pages) > 10 else "")
    )


@tool
def firecrawl_map(
    url: Annotated[str, "URL to map site structure from"],
) -> str:
    """
    Generate a list of all URLs discovered on a website without scraping content.
    Useful for understanding site structure before crawling.
    """
    try:
        client = _get_client()
    except (ImportError, ValueError) as e:
        return f"Error: {e}"

    try:
        result = client.map_url(url)
    except Exception as e:
        return f"Error mapping {url}: {e}"

    urls: list[Any] = []
    if hasattr(result, "links"):
        urls = result.links or []
    elif isinstance(result, dict):
        urls = result.get("links", [])
    elif isinstance(result, list):
        urls = result

    if not urls:
        return f"No URLs found for: {url}"

    lines = [f"# Site map: {url}", f"Found {len(urls)} URLs:\n"]
    lines.extend(f"- {u}" for u in urls[:100])
    if len(urls) > 100:
        lines.append(f"\n... and {len(urls) - 100} more")

    return "\n".join(lines)


@tool
def firecrawl_extract(
    urls: Annotated[str, "Comma-separated URLs to extract structured data from"],
    prompt: Annotated[str, "Natural language description of what data to extract"],
) -> str:
    """
    Extract structured data from one or more URLs using a natural language prompt.
    Example: extract product prices, article titles, contact information, etc.
    """
    try:
        client = _get_client()
    except (ImportError, ValueError) as e:
        return f"Error: {e}"

    url_list = [u.strip() for u in urls.split(",") if u.strip()]
    if not url_list:
        return "Error: no URLs provided."

    try:
        result = client.extract(url_list, params={"prompt": prompt})
    except Exception as e:
        return f"Error extracting from URLs: {e}"

    if hasattr(result, "data"):
        data = result.data
    elif isinstance(result, dict):
        data = result.get("data", result)
    else:
        data = result

    import json

    try:
        return json.dumps(data, indent=2, default=str)
    except Exception:
        return str(data)


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
