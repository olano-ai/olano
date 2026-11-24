"""Notes Store — [[wikilink]]-aware document registry backed by SQLite + markdown files.

Notes are stored as `.md` files in a `notes/` directory.  An SQLite
index tracks frontmatter metadata, full-text search (FTS5), and a bidirectional
wikilink graph derived from `[[concept]]` patterns in every entry.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

_WIKILINK_RE = re.compile(r"\[\[([^\[\]\|]+?)(?:\|[^\[\]]+?)?\]\]")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_entries (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL UNIQUE,
    aliases     TEXT DEFAULT '[]',
    tags        TEXT DEFAULT '[]',
    sources     TEXT DEFAULT '[]',
    created     TEXT NOT NULL,
    updated     TEXT NOT NULL,
    auto_inject INTEGER DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS vault_fts USING fts5(
    entry_id UNINDEXED,
    title,
    body,
    tags,
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS vault_links (
    from_title TEXT NOT NULL,
    to_title   TEXT NOT NULL,
    PRIMARY KEY (from_title, to_title)
);

CREATE INDEX IF NOT EXISTS idx_vault_title ON vault_entries(title);
CREATE INDEX IF NOT EXISTS idx_vault_links_from ON vault_links(from_title);
CREATE INDEX IF NOT EXISTS idx_vault_links_to ON vault_links(to_title);
"""


@dataclass
class NotesEntry:
    title: str
    content: str = ""
    aliases: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    auto_inject: bool = False

    def __post_init__(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        if not self.created:
            self.created = now
        if not self.updated:
            self.updated = now

    @property
    def id(self) -> str:
        return _title_to_id(self.title)

    def to_markdown(self) -> str:
        import json

        lines = ["---"]
        lines.append(f"title: {self.title}")
        if self.aliases:
            lines.append(f"aliases: {json.dumps(self.aliases)}")
        if self.tags:
            lines.append(f"tags: {json.dumps(self.tags)}")
        if self.sources:
            lines.append(f"sources: {json.dumps(self.sources)}")
        lines.append(f"created: {self.created}")
        lines.append(f"updated: {self.updated}")
        if self.auto_inject:
            lines.append("auto_inject: true")
        lines.append("---")
        lines.append("")
        lines.append(self.content)
        return "\n".join(lines)

    @classmethod
    def from_markdown(cls, text: str, fallback_title: str = "") -> NotesEntry:
        import json

        if not text.startswith("---"):
            return cls(title=fallback_title, content=text)

        end = text.find("---", 3)
        if end == -1:
            return cls(title=fallback_title, content=text)

        fm_text = text[3:end].strip()
        content = text[end + 3 :].strip()

        fm: dict = {}
        for line in fm_text.splitlines():
            if ": " in line:
                k, _, v = line.partition(": ")
                fm[k.strip()] = v.strip()

        def _parse_list(val: str) -> list[str]:
            try:
                result = json.loads(val)
                return result if isinstance(result, list) else [str(result)]
            except (json.JSONDecodeError, TypeError):
                return []

        return cls(
            title=fm.get("title", fallback_title),
            content=content,
            aliases=_parse_list(fm.get("aliases", "[]")),
            tags=_parse_list(fm.get("tags", "[]")),
            sources=_parse_list(fm.get("sources", "[]")),
            created=fm.get("created", ""),
            updated=fm.get("updated", ""),
            auto_inject=fm.get("auto_inject", "false").lower() in ("true", "1", "yes"),
        )


def _title_to_id(title: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", title.lower().strip())


def _title_to_filename(title: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*]', "_", title.strip())
    return f"{safe}.md"


# Bare FTS5 boolean operators (uppercase-only, matching FTS5's own grammar).
_FTS_QUERY_OPERATORS = {"AND", "OR", "NOT", "NEAR"}


def _to_fts_match(query: str) -> str:
    """Convert arbitrary user text into a safe FTS5 ``MATCH`` expression.

    FTS5 has its own query grammar in which characters like ``.`` ``:`` ``-``
    ``(`` ``*`` ``^`` are operators, so passing raw text such as ``openjobs.bot``
    straight to ``MATCH`` raises ``fts5: syntax error near "."``.  We wrap each
    whitespace-separated term in double quotes (an FTS5 *string* literal, with any
    embedded ``"`` doubled) so its contents are matched literally instead of being
    parsed as syntax.  The bare boolean operators ``AND``/``OR``/``NOT``/``NEAR``
    are preserved so callers that build explicit boolean queries (e.g.
    ``get_relevant_entries``) keep working.

    Returns ``""`` when the text contains no searchable terms (e.g. it is empty
    or pure punctuation); callers should treat that as "no results".
    """
    out: list[str] = []
    pending_op: str | None = None
    for raw in query.split():
        if raw in _FTS_QUERY_OPERATORS:
            # An operator is only meaningful between two terms; drop it otherwise.
            if out:
                pending_op = raw
            continue
        if not re.search(r"\w", raw):  # skip tokens with no indexable characters
            continue
        term = '"' + raw.replace('"', '""') + '"'
        if pending_op:
            out.append(pending_op)
        out.append(term)
        pending_op = None
    return " ".join(out)


class NotesStore:
    """Document-oriented notes store with [[wikilink]] bidirectional linking."""

    def __init__(self, notes_dir: Path | None = None, *, vault_dir: Path | None = None) -> None:
        # ``vault_dir`` is accepted only for older callers during the notes rename.
        resolved_dir = notes_dir or vault_dir
        if resolved_dir is None:
            raise TypeError("NotesStore requires notes_dir")
        self.notes_dir = resolved_dir
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.notes_dir / ".notes.db"
        self._init_db()
        self._sync_from_disk()

    # ── DB ────────────────────────────────────────────────────────────────────

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ── Wikilink parsing ──────────────────────────────────────────────────────

    @staticmethod
    def parse_wikilinks(content: str) -> list[str]:
        """Return unique wikilink targets from content (e.g. [[Kubernetes]] → 'Kubernetes')."""
        return list(dict.fromkeys(m.group(1).strip() for m in _WIKILINK_RE.finditer(content)))

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def upsert(self, entry: NotesEntry) -> None:
        """Write/update a note: saves markdown file, updates DB index and wikilinks."""
        now = datetime.now(timezone.utc).isoformat()
        entry.updated = now

        path = self.notes_dir / _title_to_filename(entry.title)
        path.write_text(entry.to_markdown(), encoding="utf-8")

        self._index_entry(entry, updated=now)

    def _index_entry(self, entry: NotesEntry, *, updated: str | None = None) -> None:
        """DB-only portion of upsert: update vault_entries, vault_fts, vault_links.

        Used by `sync_file()` (watcher path) so re-importing a file does not
        rewrite it on disk — that would retrigger the watcher and create an
        infinite write loop.
        """
        import json

        now = updated or datetime.now(timezone.utc).isoformat()
        entry_id = entry.id
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT id FROM vault_entries WHERE id=?", (entry_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE vault_entries SET title=?,aliases=?,tags=?,sources=?,updated=?,auto_inject=? WHERE id=?",
                    (
                        entry.title,
                        json.dumps(entry.aliases),
                        json.dumps(entry.tags),
                        json.dumps(entry.sources),
                        now,
                        int(entry.auto_inject),
                        entry_id,
                    ),
                )
                conn.execute("DELETE FROM vault_fts WHERE entry_id=?", (entry_id,))
            else:
                conn.execute(
                    "INSERT INTO vault_entries VALUES (?,?,?,?,?,?,?,?)",
                    (
                        entry_id,
                        entry.title,
                        json.dumps(entry.aliases),
                        json.dumps(entry.tags),
                        json.dumps(entry.sources),
                        entry.created,
                        now,
                        int(entry.auto_inject),
                    ),
                )
            conn.execute(
                "INSERT INTO vault_fts(entry_id, title, body, tags) VALUES (?,?,?,?)",
                (entry_id, entry.title, entry.content[:4000], json.dumps(entry.tags)),
            )

            # Rebuild wikilinks for this entry
            conn.execute("DELETE FROM vault_links WHERE from_title=?", (entry.title,))
            for target in self.parse_wikilinks(entry.content):
                if target != entry.title:
                    conn.execute(
                        "INSERT OR IGNORE INTO vault_links VALUES (?,?)",
                        (entry.title, target),
                    )

    def get(self, title: str) -> NotesEntry | None:
        """Read a note by title (or alias)."""
        path = self.notes_dir / _title_to_filename(title)
        if path.exists():
            text = path.read_text(encoding="utf-8")
            return NotesEntry.from_markdown(text, fallback_title=title)
        # Try alias lookup
        with self._conn() as conn:
            import json

            rows = conn.execute("SELECT aliases FROM vault_entries").fetchall()
            for row in rows:
                aliases = json.loads(row["aliases"] or "[]")
                if title in aliases:
                    actual = conn.execute(
                        "SELECT title FROM vault_entries WHERE aliases LIKE ?",
                        (f'%"{title}"%',),
                    ).fetchone()
                    if actual:
                        return self.get(actual["title"])
        return None

    def delete(self, title: str) -> bool:
        path = self.notes_dir / _title_to_filename(title)
        entry_id = _title_to_id(title)
        with self._conn() as conn:
            conn.execute("DELETE FROM vault_fts WHERE entry_id=?", (entry_id,))
            conn.execute("DELETE FROM vault_entries WHERE id=?", (entry_id,))
            conn.execute("DELETE FROM vault_links WHERE from_title=? OR to_title=?", (title, title))
        if path.exists():
            path.unlink()
            return True
        return False

    # ── Search ────────────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 10) -> list[NotesEntry]:
        """Keyword search via FTS5; returns matching entries."""
        match_query = _to_fts_match(query)
        if not match_query:
            return []
        with self._conn() as conn:
            try:
                rows = conn.execute(
                    "SELECT title FROM vault_fts WHERE vault_fts MATCH ? ORDER BY rank LIMIT ?",
                    (match_query, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                # Belt-and-suspenders: never surface an FTS5 syntax error to the
                # caller (and ultimately the chat), even on unforeseen input.
                return []
        results = []
        for row in rows:
            entry = self.get(row["title"])
            if entry:
                results.append(entry)
        return results

    def list_all(self, limit: int = 100) -> list[str]:
        """Return all note titles."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT title FROM vault_entries ORDER BY updated DESC LIMIT ?", (limit,)
            ).fetchall()
            return [r["title"] for r in rows]

    # ── Wikilink graph ────────────────────────────────────────────────────────

    def get_outlinks(self, title: str) -> list[str]:
        """Return titles that this entry links TO via [[wikilinks]]."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT to_title FROM vault_links WHERE from_title=?", (title,)
            ).fetchall()
            return [r["to_title"] for r in rows]

    def get_backlinks(self, title: str) -> list[str]:
        """Return titles of entries that link TO this entry (backlinks)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT from_title FROM vault_links WHERE to_title=?", (title,)
            ).fetchall()
            return [r["from_title"] for r in rows]

    def entry_count(self) -> int:
        with self._conn() as conn:
            return cast(int, conn.execute("SELECT COUNT(*) FROM vault_entries").fetchone()[0])

    def link_count(self) -> int:
        with self._conn() as conn:
            return cast(int, conn.execute("SELECT COUNT(*) FROM vault_links").fetchone()[0])

    # ── Disk sync ─────────────────────────────────────────────────────────────

    def _sync_from_disk(self) -> None:
        """Import any `.md` files in notes_dir that are not yet in the DB index."""
        for path in self.notes_dir.glob("*.md"):
            if path.name.startswith("."):
                continue
            try:
                text = path.read_text(encoding="utf-8")
                entry = NotesEntry.from_markdown(text, fallback_title=path.stem)
                if not entry.title:
                    continue
                entry_id = entry.id
                with self._conn() as conn:
                    exists = conn.execute(
                        "SELECT 1 FROM vault_entries WHERE id=?", (entry_id,)
                    ).fetchone()
                if not exists:
                    self._index_entry(entry)
            except Exception:
                pass

    def sync_file(self, path: Path) -> None:
        """Re-import a single notes file (called by filesystem watcher).

        Updates the DB index ONLY — never rewrites the source .md file. Going
        through `upsert()` here would re-fire the watcher (upsert writes the
        file back to disk) and produce an infinite log/write loop.
        """
        try:
            text = path.read_text(encoding="utf-8")
            entry = NotesEntry.from_markdown(text, fallback_title=path.stem)
            if entry.title:
                self._index_entry(entry)
        except Exception:
            pass

    # ── Auto-inject helpers ───────────────────────────────────────────────────

    def get_auto_inject_entries(self, limit: int = 5) -> list[NotesEntry]:
        """Return entries marked for auto-injection into L0 context."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT title FROM vault_entries WHERE auto_inject=1 ORDER BY updated DESC LIMIT ?",
                (limit,),
            ).fetchall()
        results = []
        for row in rows:
            entry = self.get(row["title"])
            if entry:
                results.append(entry)
        return results

    def get_relevant_entries(self, context: str, limit: int = 5) -> list[NotesEntry]:
        """Return entries most relevant to context text via FTS5 keyword match."""
        words = [w for w in re.split(r"\W+", context) if len(w) > 4]
        if not words:
            return self.get_auto_inject_entries(limit)
        query = " OR ".join(words[:10])
        return self.search(query, limit=limit)

    # ── Hybrid search (P5.10) ─────────────────────────────────────────────────

    def hybrid_search(self, query: str, limit: int = 5, rrf_k: int = 60) -> list[NotesEntry]:
        """Hybrid search: fuse FTS5 (BM25) + vector (Chroma) rankings via RRF.

        Falls back to FTS5-only when Chroma/embeddings are not available.
        RRF score: Σ 1/(k + rank) — higher is better.
        """
        fts_results = self.search(query, limit=limit * 2)
        vector_results = self._vector_search(query, limit=limit * 2)

        # Build RRF score map: title → score
        scores: dict[str, float] = {}
        for rank, entry in enumerate(fts_results, start=1):
            scores[entry.title] = scores.get(entry.title, 0.0) + 1.0 / (rrf_k + rank)
        for rank, entry in enumerate(vector_results, start=1):
            scores[entry.title] = scores.get(entry.title, 0.0) + 1.0 / (rrf_k + rank)

        if not scores:
            return []

        # Collect and rank unique entries
        seen: dict[str, NotesEntry] = {}
        for e in fts_results + vector_results:
            seen.setdefault(e.title, e)

        ranked = sorted(seen.values(), key=lambda e: scores.get(e.title, 0.0), reverse=True)
        return ranked[:limit]

    def _vector_search(self, query: str, limit: int = 5) -> list[NotesEntry]:
        """Semantic search via Chroma + sentence-transformers.  Returns [] on failure."""
        try:
            chroma_dir = self.notes_dir / ".chroma"
            chroma_dir.mkdir(parents=True, exist_ok=True)

            import chromadb

            from olano.memory.embeddings import embed_text

            client = chromadb.PersistentClient(path=str(chroma_dir))
            collection = client.get_or_create_collection("notes")

            # Sync: upsert all entries that are in the DB but not in Chroma
            with self._conn() as conn:
                rows = conn.execute("SELECT id, title FROM vault_entries").fetchall()
            existing_ids = set(collection.get(include=[])["ids"])
            for row in rows:
                entry_id = row["id"]
                if entry_id not in existing_ids:
                    entry = self.get(row["title"])
                    if entry:
                        vec = embed_text(f"{entry.title} {entry.content[:500]}")
                        collection.upsert(
                            ids=[entry_id],
                            embeddings=[vec.tolist()],
                            metadatas=[{"title": entry.title}],
                        )

            if collection.count() == 0:
                return []

            query_vec = embed_text(query)
            results = collection.query(
                query_embeddings=[query_vec.tolist()],
                n_results=min(limit, collection.count()),
                include=["metadatas"],
            )
            metadatas = results["metadatas"] or []
            first = metadatas[0] if metadatas else []
            titles = [str(m["title"]) for m in (first or [])]
            return [e for t in titles if (e := self.get(t))]
        except Exception:
            return []


# Backwards-compatible names for older imports during the vault→notes rename.
VaultEntry = NotesEntry
KnowledgeVault = NotesStore
