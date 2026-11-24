"""Agent-facing tools for the workspace Notes Store (markdown wiki)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.tools import tool

if TYPE_CHECKING:
    from olano.memory.notes_store import NotesStore

_notes_path: Path | None = None
_notes_instance: NotesStore | None = None


def set_notes_path(path: Path) -> None:
    global _notes_path, _notes_instance
    _notes_path = path
    _notes_instance = None


def _get_notes() -> NotesStore:
    global _notes_instance
    if _notes_instance is None:
        if _notes_path is None:
            raise RuntimeError("Notes path not configured. Call set_notes_path() first.")
        from olano.memory.notes_store import NotesStore

        _notes_instance = NotesStore(notes_dir=_notes_path)
    return _notes_instance


@tool
def notes_add(
    title: str,
    content: str,
    tags: str = "",
    aliases: str = "",
    sources: str = "",
    auto_inject: bool = False,
) -> str:
    """Add or update a note in the workspace markdown wiki.

    Use [[wikilinks]] in content to create bidirectional links between notes.
    tags/aliases/sources are comma-separated strings.
    Not for secrets — use `secret_set` for API keys / credentials.
    """
    from olano.memory.notes_store import NotesEntry

    notes = _get_notes()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    alias_list = [a.strip() for a in aliases.split(",") if a.strip()]
    source_list = [s.strip() for s in sources.split(",") if s.strip()]

    entry = NotesEntry(
        title=title,
        content=content,
        tags=tag_list,
        aliases=alias_list,
        sources=source_list,
        auto_inject=auto_inject,
    )
    notes.upsert(entry)
    links = notes.parse_wikilinks(content)
    link_msg = f" Links to: {', '.join(links)}." if links else ""
    return f"Note '{title}' saved.{link_msg}"


@tool
def notes_get(title: str) -> str:
    """Read a note from the workspace markdown wiki by title or alias.

    Not for secrets — use `secret_get` for API keys / credentials.
    """
    notes = _get_notes()
    entry = notes.get(title)
    if not entry:
        return f"No note found for '{title}'."
    backlinks = notes.get_backlinks(entry.title)
    outlinks = notes.get_outlinks(entry.title)
    header = f"# {entry.title}\n"
    if entry.tags:
        header += f"Tags: {', '.join(entry.tags)}\n"
    if outlinks:
        header += f"Links to: {', '.join(outlinks)}\n"
    if backlinks:
        header += f"Referenced by: {', '.join(backlinks)}\n"
    return header + "\n" + entry.content


@tool
def notes_search(query: str, limit: int = 5) -> str:
    """Search notes matching the query (hybrid FTS5 + semantic)."""
    notes = _get_notes()
    results = notes.hybrid_search(query, limit=limit)
    if not results:
        return f"No notes matching '{query}'."
    lines = []
    for e in results:
        snippet = e.content[:200].replace("\n", " ")
        lines.append(f"**{e.title}**: {snippet}...")
    return "\n\n".join(lines)


@tool
def notes_list() -> str:
    """List all notes in the workspace markdown wiki."""
    notes = _get_notes()
    titles = notes.list_all(limit=50)
    if not titles:
        return "The notes store is empty."
    return "Notes:\n" + "\n".join(f"- {t}" for t in titles)


@tool
def notes_backlinks(title: str) -> str:
    """Show which notes link to this note (backlinks)."""
    notes = _get_notes()
    backlinks = notes.get_backlinks(title)
    if not backlinks:
        return f"No notes link to '{title}'."
    return f"Notes linking to '{title}':\n" + "\n".join(f"- {b}" for b in backlinks)


_NOTES_TOOL_NAMES = ["notes_add", "notes_get", "notes_search", "notes_list", "notes_backlinks"]


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
