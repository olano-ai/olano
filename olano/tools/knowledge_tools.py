"""
Knowledge base tools — manage and query RAG vector stores.

Named stores
------------
  workspace     — workspace files (auto-ingested on boot if memory.rag=true)
  memory        — MEMORY.md + daily logs
  knowledgebase — manually added files / URLs (default for kb_add_*)
  <custom>      — created on demand via kb_create_store

Search modes
------------
  similarity (default) — cosine similarity, results ranked by score
  mmr                  — Maximum Marginal Relevance for diverse results
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

_rag: Any = None  # singleton RAGMemory


def _get_rag() -> Any:
    global _rag
    if _rag is None:
        from olano.config.workspace import get_workspace
        from olano.memory.rag_memory import RAGMemory

        workspace = get_workspace()
        _rag = RAGMemory(
            persist_dir=workspace.root / "rag",
            collection_name="knowledgebase",
        )
    return _rag


def set_rag(rag_instance: Any) -> None:
    """Inject a shared RAGMemory instance (called by builder on boot)."""
    global _rag
    _rag = rag_instance


# ── Add sources ───────────────────────────────────────────────────────────────


@tool
def kb_add_file(
    path: Annotated[str, "Absolute or workspace-relative path to a file"],
    collection: Annotated[str, "Store to add to (default: knowledgebase)"] = "knowledgebase",
) -> str:
    """
    Add a file to a knowledge base store.

    Supported formats (no config needed):
      Plain text, Markdown, code (.py/.js/.ts/…), JSON, YAML, CSV, HTML
    Optional extras (install to enable):
      PDF  → uv add pypdf
      DOCX → uv add docx2txt

    Files are hash-checked — unchanged files are skipped automatically.
    """
    try:
        rag = _get_rag()
    except Exception as e:
        return f"Error: could not initialize knowledge base — {e}"

    p = Path(path)
    if not p.is_absolute():
        try:
            from olano.config.workspace import get_workspace

            p = get_workspace().root / path
        except Exception:
            pass

    try:
        n = rag.ingest_file(p, collection=collection)
    except FileNotFoundError:
        return f"Error: file not found — {path}"
    except Exception as e:
        return f"Error ingesting file: {e}"

    if n == 0:
        return f"'{p.name}' is unchanged since last ingestion — skipped."
    return f"Added '{p.name}' to store '{collection}' ({n} chunks indexed)."


@tool
def kb_add_url(
    url: Annotated[str, "URL to scrape and add to the knowledge base"],
    collection: Annotated[str, "Store to add to (default: knowledgebase)"] = "knowledgebase",
) -> str:
    """
    Scrape a URL and add its content to a knowledge base store.
    Uses Firecrawl if available for clean markdown, falls back to httpx.
    """
    try:
        rag = _get_rag()
    except Exception as e:
        return f"Error: could not initialize knowledge base — {e}"

    try:
        n = rag.ingest_url(url, collection=collection)
    except Exception as e:
        return f"Error ingesting URL: {e}"

    return f"Added '{url}' to store '{collection}' ({n} chunks indexed)."


@tool
def kb_create_store(
    name: Annotated[str, "Name for the new store (e.g. 'project_docs', 'company_wiki')"],
    directory: Annotated[str, "Path to directory whose files will be ingested"],
    glob: Annotated[str, "Glob pattern for file selection (default: all files)"] = "**/*",
) -> str:
    """
    Create a named knowledge base store by recursively ingesting all files in a directory.

    Use this to build specialized stores — e.g. a store for a project's source code,
    a documentation folder, or any set of reference files.

    Binary files (images, executables, zips, etc.) are automatically skipped.
    """
    try:
        rag = _get_rag()
    except Exception as e:
        return f"Error: could not initialize knowledge base — {e}"

    d = Path(directory)
    if not d.is_absolute():
        try:
            from olano.config.workspace import get_workspace

            d = get_workspace().root / directory
        except Exception:
            pass

    try:
        n = rag.create_collection_from_dir(name=name, directory=d, glob=glob)
    except NotADirectoryError:
        return f"Error: not a directory — {directory}"
    except Exception as e:
        return f"Error creating store: {e}"

    if n == 0:
        return f"No new chunks indexed for store '{name}' (all files unchanged or empty)."
    return f"Created store '{name}' — {n} chunks indexed from {d}."


# ── Search ────────────────────────────────────────────────────────────────────


@tool
def kb_search(
    query: Annotated[str, "Natural language search query"],
    k: Annotated[int, "Max results (1–20)"] = 5,
    collection: Annotated[
        str,
        "Store to search: 'workspace', 'memory', 'knowledgebase', a custom store name, or 'all'",
    ] = "all",
    mode: Annotated[
        str,
        "Search mode: 'similarity' (default, ranked by score) or 'mmr' (diverse results)",
    ] = "similarity",
) -> str:
    """
    Search a knowledge base store for content semantically similar to your query.
    Use 'all' to search across every store simultaneously.
    Use 'mmr' mode when you want varied results (avoids near-duplicate chunks).
    """
    try:
        rag = _get_rag()
    except Exception as e:
        return f"Error: could not initialize knowledge base — {e}"

    k = max(1, min(20, k))

    try:
        if collection == "all":
            results = rag.search_all(query, k=k, mode=mode)
        else:
            results = rag.search(query, k=k, collection=collection, mode=mode)
    except Exception as e:
        return f"Error searching knowledge base: {e}"

    if not results:
        return f"No results found for: '{query}'"

    lines = [f"Knowledge base results for '{query}' [{mode}]:\n"]
    for i, r in enumerate(results, 1):
        score_str = f"score={r['score']:.3f}" if r.get("score") is not None else "mmr"
        col_label = r.get("collection", collection)
        source = r.get("source", "unknown")
        content = r["content"][:500].strip()
        lines.append(f"[{i}] store={col_label}  {score_str}\n     source: {source}\n     {content}")

    return "\n\n".join(lines)


@tool
def kb_list(
    collection: Annotated[
        str,
        "Store to list: 'workspace', 'memory', 'knowledgebase', a custom name, or 'all'",
    ] = "all",
) -> str:
    """List all sources currently indexed in a knowledge base store."""
    try:
        rag = _get_rag()
    except Exception as e:
        return f"Error: could not initialize knowledge base — {e}"

    try:
        if collection == "all":
            all_sources: dict[str, list[str]] = {}
            for col in rag._get_existing_collections():
                sources = rag.list_sources(collection=col)
                if sources:
                    all_sources[col] = sources
            if not all_sources:
                return "Knowledge base is empty."
            lines = ["Knowledge base contents:"]
            for col, srcs in all_sources.items():
                stats = rag.collection_stats(col)
                lines.append(f"\n  [{col}]  {stats['sources']} sources · {stats['chunks']} chunks")
                lines.extend(f"      - {s}" for s in srcs)
            return "\n".join(lines)
        else:
            sources = rag.list_sources(collection=collection)
            if not sources:
                return f"No sources in store '{collection}'."
            stats = rag.collection_stats(collection)
            lines = [f"[{collection}]  {stats['sources']} sources · {stats['chunks']} chunks"]
            lines.extend(f"  - {s}" for s in sources)
            return "\n".join(lines)
    except Exception as e:
        return f"Error listing knowledge base: {e}"


@tool
def kb_delete(
    source_id: Annotated[str, "Source ID (file path or URL) to remove from the index"],
    collection: Annotated[
        str,
        "Store to delete from: a specific store name or 'all'",
    ] = "all",
) -> str:
    """
    Remove a source (file path or URL) from the vector index.
    This only removes it from the index — the file on disk is NOT deleted.
    Use kb_remove_file to delete the physical file from the knowledgebase directory.
    """
    try:
        rag = _get_rag()
    except Exception as e:
        return f"Error: could not initialize knowledge base — {e}"

    try:
        if collection == "all":
            total = sum(
                rag.delete_source(source_id, collection=col)
                for col in rag._get_existing_collections()
            )
        else:
            total = rag.delete_source(source_id, collection=collection)
    except Exception as e:
        return f"Error deleting from knowledge base: {e}"

    if total == 0:
        return f"Source '{source_id}' not found in knowledge base."
    return f"Removed '{source_id}' from index ({total} chunks deleted)."


@tool
def kb_remove_file(
    filename: Annotated[
        str,
        "Filename (e.g. 'report.pdf') or full path of the file to remove",
    ],
    collection: Annotated[str, "Store to remove from (default: knowledgebase)"] = "knowledgebase",
    delete_from_disk: Annotated[bool, "Also delete the physical file (default: True)"] = True,
) -> str:
    """
    Remove a file from the knowledge base: de-indexes it AND (by default) deletes
    the physical file from workspace/knowledgebase/.

    Pass delete_from_disk=False to only remove from the index, keeping the file.
    """
    try:
        rag = _get_rag()
    except Exception as e:
        return f"Error: could not initialize knowledge base — {e}"

    # Resolve to full path
    p = Path(filename)
    if not p.is_absolute():
        try:
            from olano.config.workspace import get_workspace

            kb_dir = get_workspace().root / "knowledgebase"
            p = kb_dir / filename
        except Exception:
            pass

    source_id = str(p.resolve()) if p.exists() else filename

    # De-index
    try:
        removed = rag.delete_source(source_id, collection=collection)
    except Exception as e:
        return f"Error removing from index: {e}"

    # Delete physical file
    deleted_file = False
    if delete_from_disk and p.exists():
        try:
            p.unlink()
            deleted_file = True
        except Exception as e:
            return f"Removed {removed} chunks from index, but could not delete file: {e}"

    parts = [f"Removed {removed} chunks from '{collection}' index."]
    if deleted_file:
        parts.append(f"Deleted file: {p.name}")
    elif delete_from_disk:
        parts.append(f"File not found on disk: {p}")
    return "  ".join(parts)


@tool
def kb_sync(
    collection: Annotated[str, "Store to sync (default: knowledgebase)"] = "knowledgebase",
    directory: Annotated[
        str,
        "Directory to sync from (default: workspace/knowledgebase/). "
        "Leave empty to use the default managed directory.",
    ] = "",
) -> str:
    """
    Manually re-sync a knowledge base store with its source directory.

    Ingests new/changed files, removes chunks for deleted files.
    Useful if the watcher was not running or files were added externally.
    """
    try:
        rag = _get_rag()
    except Exception as e:
        return f"Error: could not initialize knowledge base — {e}"

    # Resolve directory
    if directory:
        d = Path(directory)
        if not d.is_absolute():
            try:
                from olano.config.workspace import get_workspace

                d = get_workspace().root / directory
            except Exception:
                pass
    else:
        try:
            from olano.config.workspace import get_workspace

            d = get_workspace().root / "knowledgebase"
        except Exception as e:
            return f"Error resolving knowledgebase directory: {e}"

    if not d.is_dir():
        return f"Directory not found: {d}"

    try:
        stats = rag.sync_directory(
            collection=collection,
            directory=d,
            delete_removed=True,
        )
    except Exception as e:
        return f"Error syncing: {e}"

    return (
        f"Synced store '{collection}' from {d}:\n"
        f"  +{stats['added']} added  "
        f"~{stats['updated']} updated  "
        f"-{stats['removed']} removed  "
        f"{stats['skipped']} unchanged"
    )


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
