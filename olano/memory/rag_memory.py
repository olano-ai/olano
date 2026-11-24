"""
RAG memory — ChromaDB-backed vector stores with proper text splitting,
multi-format file loading, and a shared embedding singleton.

Collections
-----------
  workspace    — workspace files (auto-ingested on boot when memory.rag=true)
  memory       — MEMORY.md + daily logs (for semantic recall of past facts)
  knowledgebase — manually added files / URLs via kb_* agent tools
  <custom>     — any user-defined name via create_collection_from_dir()

Design decisions
----------------
- Embeddings: default fastembed / BAAI/bge-small-en-v1.5 (ONNX, no torch, no
  HF token); override via LIONCLAW_EMBEDDING_MODEL=hf for legacy MiniLM. See
  olano/memory/embeddings.py. Loaded ONCE as a process singleton, shared by
  the LangChain (Chroma) and raw embed_text paths — no weight reloads.
  The store is namespaced by the active model (persist_dir/<fingerprint>/) so
  switching backends never mixes incompatible vectors.
- Text splitting: RecursiveCharacterTextSplitter (sentence/paragraph-aware).
  chunk_size=1500 chars (~300 tokens), overlap=200 chars.
- Chroma: cosine distance (correct for normalized embeddings).
- Hash index: persisted to <persist_dir>/hashes.json — skip unchanged files.
- File loaders: format-aware with graceful fallbacks to plain text.
  PDF needs pypdf, DOCX needs docx2txt — installed lazily, not required at boot.
- Search modes: 'similarity' (default) or 'mmr' (diverse results).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, cast

from langchain_core.documents import Document
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ── Defaults ──────────────────────────────────────────────────────────────────

CHUNK_SIZE = 1500  # chars ≈ 300-375 tokens (well within 256-token model limit)
CHUNK_OVERLAP = 200  # chars of overlap between consecutive chunks
DEFAULT_K = 5
SCORE_THRESHOLD = 0.20  # minimum relevance score (cosine, 0–1)

# Extensions skipped during directory sync / create_collection_from_dir
_DEFAULT_SKIP_EXTENSIONS: set[str] = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".exe",
    ".bin",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".ico",
    ".svg",
    ".webp",
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
    ".wav",
    ".flac",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".7z",
    ".rar",
    ".lock",
    ".cache",
    ".DS_Store",
    ".git",
}

# File extensions treated as plain text without a special loader
_TEXT_LIKE = {
    ".txt",
    ".md",
    ".rst",
    ".log",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".sh",
    ".bash",
    ".zsh",
    ".css",
    ".scss",
    ".less",
    ".sql",
    ".xml",
    ".json",
    ".env",
    ".r",
    ".ipynb",
}


# ── File loading ──────────────────────────────────────────────────────────────


def _load_documents(path: Path) -> list[Document]:
    """
    Load a file and return a list of LangChain Documents.
    Uses the best available loader for each format; falls back to plain text.
    """
    ext = path.suffix.lower()

    # ── PDF ───────────────────────────────────────────────────────────────────
    if ext == ".pdf":
        try:
            from langchain_community.document_loaders import PyPDFLoader

            loader: Any = PyPDFLoader(str(path))
            return cast(list[Document], loader.load())
        except ImportError:
            logger.debug("pypdf not installed, reading PDF as raw text")
        except Exception as e:
            logger.warning("PyPDFLoader failed for %s: %s", path.name, e)

    # ── Word / DOCX ───────────────────────────────────────────────────────────
    elif ext in (".docx", ".doc"):
        try:
            from langchain_community.document_loaders import Docx2txtLoader

            loader = Docx2txtLoader(str(path))
            return cast(list[Document], loader.load())
        except ImportError:
            logger.debug("docx2txt not installed, reading DOCX as raw text")
        except Exception as e:
            logger.warning("Docx2txtLoader failed for %s: %s", path.name, e)

    # ── HTML ──────────────────────────────────────────────────────────────────
    elif ext in (".html", ".htm"):
        try:
            from langchain_community.document_loaders import BSHTMLLoader

            loader = BSHTMLLoader(str(path), open_encoding="utf-8")
            return cast(list[Document], loader.load())
        except ImportError:
            logger.debug("beautifulsoup4 not installed, reading HTML as raw text")
        except Exception as e:
            logger.warning("BSHTMLLoader failed for %s: %s", path.name, e)

    # ── CSV ───────────────────────────────────────────────────────────────────
    elif ext == ".csv":
        try:
            from langchain_community.document_loaders.csv_loader import CSVLoader

            loader = CSVLoader(str(path))
            return cast(list[Document], loader.load())
        except Exception as e:
            logger.warning("CSVLoader failed for %s: %s", path.name, e)

    # ── Plain text / code (all other extensions) ──────────────────────────────
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return [Document(page_content=text, metadata={"source": str(path)})]
    except Exception as e:
        raise ValueError(f"Cannot read file {path}: {e}") from e


def _get_splitter(chunk_size: int = CHUNK_SIZE, chunk_overlap: int = CHUNK_OVERLAP):
    """Return a RecursiveCharacterTextSplitter (sentence/paragraph-aware)."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", ". ", "! ", "? ", "; ", ", ", " ", ""],
        length_function=len,
        is_separator_regex=False,
    )


def _file_hash(path: Path) -> str:
    h = hashlib.md5(path.read_bytes(), usedforsecurity=False)
    return h.hexdigest()


# ── Ingest throttling ─────────────────────────────────────────────────────────
#
# Nobody waits on an ingest. It is a background warm-up: the RAG *tools* are
# wired before it starts, so the agent is already answering while its documents
# are still being embedded. Left unthrottled it nevertheless behaves like a
# batch job — running flat out until done — and on a small host that is the
# difference between a runtime that answers in milliseconds and one that takes
# seconds to serve /api/status while the cores are busy.
#
# Two dials, applied at the single choke point every ingest path goes through
# (``_upsert_chunks``):
#
#   * embedding_threads()  — how much CPU one forward pass may use (see
#     embeddings.py; default 1 thread).
#   * INGEST_DUTY_CYCLE    — what fraction of wall-clock the ingest may spend
#     working. After each embed batch the ingest sleeps long enough to hold
#     this ratio, so 0.5 means "one batch of work, an equal rest" ≈ half a core
#     average. Ingesting takes proportionally longer; that is the point.
#
# Set LIONCLAW_RAG_INGEST_DUTY=1.0 to disable pacing (e.g. a CLI one-shot where
# somebody IS waiting).
_DEFAULT_INGEST_DUTY = 0.5
_MIN_PACE_SLEEP = 0.01  # don't bother with sub-10ms naps

# Background ingests (boot warm-up, directory watchers) serialize on this. It is
# deliberately NOT taken by interactive paths — an agent calling kb_add should
# not queue behind a multi-minute boot warm-up; pacing alone keeps it polite.
BACKGROUND_INGEST_GATE = threading.BoundedSemaphore(1)


def ingest_duty_cycle() -> float:
    """Resolve the ingest duty cycle (``LIONCLAW_RAG_INGEST_DUTY``), 0 < d <= 1."""
    raw = (os.environ.get("LIONCLAW_RAG_INGEST_DUTY") or "").strip()
    if raw:
        try:
            value = float(raw)
            if 0.0 < value <= 1.0:
                return value
            logger.warning(
                "Ignoring out-of-range LIONCLAW_RAG_INGEST_DUTY=%r (want 0 < d <= 1)", raw
            )
        except ValueError:
            logger.warning("Ignoring non-numeric LIONCLAW_RAG_INGEST_DUTY=%r", raw)
    return _DEFAULT_INGEST_DUTY


def _pace(work_seconds: float) -> None:
    """Sleep long enough that `work_seconds` of work holds the duty cycle."""
    duty = ingest_duty_cycle()
    if duty >= 1.0:
        return
    rest = work_seconds * (1.0 / duty - 1.0)
    if rest >= _MIN_PACE_SLEEP:
        time.sleep(rest)


# ── Core RAGMemory class ──────────────────────────────────────────────────────


class RAGMemory:
    """
    Persistent vector memory using ChromaDB with a shared embedding singleton.

    All collections live in the same ChromaDB persist directory, so only one
    Chroma client is used per process (no redundant file handles).
    """

    def __init__(
        self,
        persist_dir: Path,
        collection_name: str = "knowledgebase",
        chunk_size: int = CHUNK_SIZE,
        chunk_overlap: int = CHUNK_OVERLAP,
        embedding_model: str | None = None,
    ) -> None:
        self.collection_name = collection_name
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.embedding_model = embedding_model

        # Namespace the store by the active embedding model. Different models
        # produce incompatible vectors at the SAME dimension (bge-small and
        # MiniLM are both 384-dim), so mixing them in one Chroma collection would
        # silently return garbage rather than raising. Putting each model's Chroma
        # DB + hash index under its own subdir means switching backends (or the
        # fastembed default landing on an existing MiniLM install) transparently
        # starts a fresh store that the boot ingest repopulates from source docs.
        from olano.memory.embeddings import embedding_fingerprint

        self.persist_dir = Path(persist_dir) / embedding_fingerprint(embedding_model)

        self.persist_dir.mkdir(parents=True, exist_ok=True)

        self._stores: dict[str, Any] = {}  # collection name → Chroma store
        self._hash_index: dict[str, str] = self._load_hash_index()

    # ── Embedding singleton ───────────────────────────────────────────────────

    def _get_embeddings(self) -> Any:
        """Return the shared embedding singleton (loads model once per process)."""
        if self.embedding_model and self.embedding_model.startswith("openai:"):
            from langchain_openai import OpenAIEmbeddings

            return OpenAIEmbeddings(model=self.embedding_model.split(":", 1)[1])
        from olano.memory.embeddings import get_langchain_embeddings

        return get_langchain_embeddings()

    # ── Chroma store management ───────────────────────────────────────────────

    def _get_store(self, collection: str | None = None) -> Any:
        col = collection or self.collection_name
        if col not in self._stores:
            self._stores[col] = self._init_store(col)
        return self._stores[col]

    def _init_store(self, collection: str) -> Any:
        try:
            from langchain_chroma import Chroma
        except ImportError:
            raise ImportError("RAG requires langchain-chroma. Install: uv add langchain-chroma")
        return Chroma(
            collection_name=f"lionclaw_{collection}",
            embedding_function=self._get_embeddings(),
            persist_directory=str(self.persist_dir / "chroma"),
            # cosine distance is correct for L2-normalized embeddings
            collection_metadata={"hnsw:space": "cosine"},
        )

    # ── Hash index (persisted across restarts) ────────────────────────────────

    def _hash_index_path(self) -> Path:
        return self.persist_dir / "hashes.json"

    def _load_hash_index(self) -> dict[str, str]:
        p = self._hash_index_path()
        if p.exists():
            try:
                data: dict[str, str] = json.loads(p.read_text(encoding="utf-8"))
                return data
            except Exception:
                pass
        return {}

    def _save_hash_index(self) -> None:
        p = self._hash_index_path()
        try:
            p.write_text(
                json.dumps(self._hash_index, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("Could not persist hash index: %s", e)

    def _known_sources_under_directory(self, directory: Path) -> set[str]:
        """Return hashed file sources that live under the given directory."""
        root = directory.resolve()
        sources: set[str] = set()
        for source_id in self._hash_index:
            try:
                candidate = Path(source_id)
            except Exception:
                continue
            if not candidate.is_absolute():
                continue
            try:
                resolved = candidate.resolve(strict=False)
                resolved.relative_to(root)
            except Exception:
                continue
            sources.add(str(resolved))
        return sources

    # ── Ingestion ─────────────────────────────────────────────────────────────

    def ingest_file(self, path: str | Path, collection: str | None = None) -> int:
        """
        Load a file, split into chunks, and embed into a collection.

        Supported formats (with optional extras):
          Plain text / code / markdown / JSON / YAML — always
          PDF (.pdf)        — requires pypdf        (uv add pypdf)
          Word  (.docx)     — requires docx2txt     (uv add docx2txt)
          HTML  (.html)     — requires beautifulsoup4 (uv add beautifulsoup4)
          CSV   (.csv)      — always (via langchain_community)

        Skips unchanged files (hash-checked, index persisted across restarts).
        Returns number of chunks indexed (0 = skipped).
        """
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {p}")

        col = collection or self.collection_name
        source_id = str(p.resolve())
        new_hash = _file_hash(p)

        if self._hash_index.get(source_id) == new_hash:
            logger.debug("RAG: unchanged, skipping %s", p.name)
            return 0

        docs = _load_documents(p)
        split = _get_splitter(self.chunk_size, self.chunk_overlap)
        chunks = split.split_documents(docs)

        # Stamp source metadata on every chunk
        for i, chunk in enumerate(chunks):
            chunk.metadata.setdefault("source", source_id)
            chunk.metadata["chunk"] = i
            chunk.metadata["collection"] = col
            chunk.metadata["filename"] = p.name

        self._upsert_chunks(chunks, source_id, col)
        self._hash_index[source_id] = new_hash
        self._save_hash_index()

        logger.info("RAG: ingested %d chunks from '%s' → '%s'", len(chunks), p.name, col)
        return len(chunks)

    def ingest_url(self, url: str, collection: str | None = None) -> int:
        """
        Scrape a URL, split, and embed into a collection (default: knowledgebase).
        Returns number of chunks indexed.
        """
        col = collection or self.collection_name

        content = self._fetch_url(url)
        if not content:
            raise ValueError(f"Could not fetch content from {url}")

        doc = Document(page_content=content, metadata={"source": url})
        split = _get_splitter(self.chunk_size, self.chunk_overlap)
        chunks = split.split_documents([doc])

        for i, chunk in enumerate(chunks):
            chunk.metadata["chunk"] = i
            chunk.metadata["collection"] = col

        self._upsert_chunks(chunks, url, col)
        self._save_hash_index()

        logger.info("RAG: ingested %d chunks from '%s' → '%s'", len(chunks), url, col)
        return len(chunks)

    def ingest_text(self, text: str, source_id: str, collection: str | None = None) -> int:
        """Directly ingest arbitrary text. Returns chunk count."""
        col = collection or self.collection_name
        doc = Document(page_content=text, metadata={"source": source_id})
        split = _get_splitter(self.chunk_size, self.chunk_overlap)
        chunks = split.split_documents([doc])
        for i, chunk in enumerate(chunks):
            chunk.metadata["chunk"] = i
            chunk.metadata["collection"] = col
        self._upsert_chunks(chunks, source_id, col)
        self._save_hash_index()
        return len(chunks)

    def create_collection_from_dir(
        self,
        name: str,
        directory: str | Path,
        glob: str = "**/*",
        skip_extensions: set[str] | None = None,
    ) -> int:
        """
        Recursively ingest all files in a directory into a named collection.
        Returns total chunks indexed (new + updated; unchanged files skipped).
        """
        stats = self.sync_directory(
            collection=name,
            directory=directory,
            glob=glob,
            skip_extensions=skip_extensions,
            delete_removed=False,
        )
        total = stats["added"] + stats["updated"]
        logger.info(
            "RAG: collection '%s' — %d added, %d updated, %d skipped from %s",
            name,
            stats["added"],
            stats["updated"],
            stats["skipped"],
            directory,
        )
        return total

    def sync_directory(
        self,
        collection: str,
        directory: str | Path,
        glob: str = "**/*",
        skip_extensions: set[str] | None = None,
        delete_removed: bool = True,
    ) -> dict[str, int]:
        """
        Bi-directional sync between a filesystem directory and a collection:
          - New or changed files → ingest
          - Files deleted from disk → remove from collection (if delete_removed=True)
          - Unchanged files → skip

        Returns {"added": N, "updated": N, "removed": N, "skipped": N}.
        """
        _skip = skip_extensions or _DEFAULT_SKIP_EXTENSIONS
        d = Path(directory)
        if not d.is_dir():
            raise NotADirectoryError(f"Not a directory: {d}")

        existing_sources = self._known_sources_under_directory(d)
        seen: set[str] = set()
        changed_paths: list[tuple[Path, str]] = []
        stats = {"added": 0, "updated": 0, "removed": 0, "skipped": 0}

        for p in sorted(d.glob(glob)):
            if not p.is_file() or p.suffix.lower() in _skip:
                continue
            source_id = str(p.resolve())
            seen.add(source_id)
            new_hash = _file_hash(p)

            if self._hash_index.get(source_id) == new_hash:
                stats["skipped"] += 1
                continue
            changed_paths.append((p, source_id))

        removed_sources = (existing_sources - seen) if delete_removed else set()
        if not changed_paths and not removed_sources:
            return stats

        for p, source_id in changed_paths:
            try:
                n = self.ingest_file(p, collection=collection)
                if source_id in existing_sources:
                    stats["updated"] += 1 if n > 0 else 0
                    stats["skipped"] += 1 if n == 0 else 0
                else:
                    stats["added"] += 1 if n > 0 else 0
            except Exception as e:
                logger.warning("RAG sync: skipping %s — %s", p.name, e)

        for source_id in removed_sources:
            p = Path(source_id)
            if p.is_absolute() and not p.exists():
                removed = self.delete_source(source_id, collection=collection)
                if removed:
                    stats["removed"] += 1
                    logger.info("RAG sync: removed deleted file '%s'", source_id)

        return stats

    # ── URL fetching ──────────────────────────────────────────────────────────

    def _fetch_url(self, url: str) -> str:
        # A model-supplied URL: refuse private and metadata addresses before
        # anything fetches it, and again on every redirect hop below.
        from olano.security.request_context import get_request_context
        from olano.security.ssrf import assert_safe_outbound_url, redirect_guard

        security = dict(get_request_context().metadata.get("security", {}) or {})
        allowlist = list(security.get("ssrf_allowlist", []) or [])
        blocklist = list(security.get("ssrf_blocklist", []) or [])
        assert_safe_outbound_url(url, allowlist=allowlist, blocklist=blocklist)

        # Try Firecrawl first (returns clean markdown)
        try:
            from olano.tools.firecrawl import firecrawl_scrape

            result = firecrawl_scrape.invoke({"url": url, "save_to_workspace": False})
            if result and not str(result).startswith("Error"):
                return str(result)
        except Exception:
            pass

        # Fallback: httpx raw fetch
        try:
            import httpx

            hooks = {"request": [redirect_guard(allowlist=allowlist, blocklist=blocklist)]}
            with httpx.Client(
                follow_redirects=True,
                timeout=30,
                headers={"User-Agent": "Mozilla/5.0"},
                event_hooks=hooks,
            ) as client:
                resp = client.get(url)
            resp.raise_for_status()
            # Strip HTML tags if BeautifulSoup is available
            try:
                from bs4 import BeautifulSoup

                return str(BeautifulSoup(resp.text, "html.parser").get_text(separator="\n"))
            except ImportError:
                return resp.text
        except Exception as e:
            raise ValueError(f"Cannot fetch {url}: {e}") from e

    # ── Chunk upsert ──────────────────────────────────────────────────────────

    def _upsert_chunks(self, chunks: list[Document], source_id: str, collection: str) -> None:
        """Delete old chunks for this source then add the new ones.

        Added in embed-sized slices with a rest between them (see
        ``_pace``): one ``add_documents`` call per slice embeds exactly the
        chunks it was given, so slicing here is what makes the ingest
        interruptible and pace-able. Peak memory is unchanged — the encoder
        already batched internally at the same size.
        """
        store = self._get_store(collection)
        try:
            existing = store.get(where={"source": source_id})
            old_ids = existing.get("ids", [])
            if old_ids:
                store.delete(ids=old_ids)
        except Exception:
            pass  # collection may be empty
        if not chunks:
            return

        from olano.memory.embeddings import embedding_batch_size

        batch = max(1, embedding_batch_size())
        for start in range(0, len(chunks), batch):
            began = time.monotonic()
            store.add_documents(chunks[start : start + batch])
            if start + batch < len(chunks):  # no need to rest after the last one
                _pace(time.monotonic() - began)

    # ── Search ────────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        k: int = DEFAULT_K,
        collection: str | None = None,
        mode: str = "similarity",
        score_threshold: float = SCORE_THRESHOLD,
    ) -> list[dict[str, Any]]:
        """
        Search a collection for chunks semantically similar to the query.

        Args:
            query:           Natural-language search string.
            k:               Maximum results to return.
            collection:      Which collection to search (default: self.collection_name).
            mode:            'similarity' (ranked by score) or 'mmr' (diverse).
            score_threshold: Minimum relevance score 0–1 (similarity mode only).

        Returns list of dicts: {content, source, chunk, score, collection}.
        """
        store = self._get_store(collection or self.collection_name)
        col = collection or self.collection_name

        if mode == "mmr":
            docs = store.max_marginal_relevance_search(query, k=k, fetch_k=k * 3)
            return [
                {
                    "content": d.page_content,
                    "source": d.metadata.get("source", ""),
                    "chunk": d.metadata.get("chunk", 0),
                    "score": None,
                    "collection": col,
                }
                for d in docs
            ]

        # Default: similarity with relevance scores
        try:
            pairs = store.similarity_search_with_relevance_scores(query, k=k)
        except Exception:
            pairs = [(d, 0.0) for d in store.similarity_search(query, k=k)]

        results = []
        for doc, score in pairs:
            s = round(float(score), 4)
            if s < score_threshold:
                continue
            results.append(
                {
                    "content": doc.page_content,
                    "source": doc.metadata.get("source", ""),
                    "chunk": doc.metadata.get("chunk", 0),
                    "score": s,
                    "collection": col,
                }
            )
        return results

    def search_all(
        self,
        query: str,
        k: int = DEFAULT_K,
        mode: str = "similarity",
        score_threshold: float = SCORE_THRESHOLD,
        collections: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search across all (or specified) collections and merge results by score.
        """
        targets = collections or self._get_existing_collections()
        results: list[dict[str, Any]] = []
        for col in targets:
            try:
                r = self.search(
                    query, k=k, collection=col, mode=mode, score_threshold=score_threshold
                )
                results.extend(r)
            except Exception as e:
                logger.debug("search_all skipping '%s': %s", col, e)

        if mode == "similarity":
            results.sort(key=lambda x: x.get("score") or 0, reverse=True)

        return results[:k]

    def _get_existing_collections(self) -> list[str]:
        """Return collection names that have been initialised this session."""
        return list(self._stores) or [self.collection_name]

    # ── Management ────────────────────────────────────────────────────────────

    def list_sources(self, collection: str | None = None) -> list[str]:
        store = self._get_store(collection or self.collection_name)
        try:
            data = store.get()
            sources = {m.get("source", "") for m in data.get("metadatas", [])}
            return sorted(s for s in sources if s)
        except Exception:
            return []

    def delete_source(self, source_id: str, collection: str | None = None) -> int:
        store = self._get_store(collection or self.collection_name)
        try:
            existing = store.get(where={"source": source_id})
            ids = existing.get("ids", [])
            if ids:
                store.delete(ids=ids)
            self._hash_index.pop(source_id, None)
            self._save_hash_index()
            return len(ids)
        except Exception as e:
            logger.warning("RAG delete_source error: %s", e)
            return 0

    def collection_stats(self, collection: str | None = None) -> dict[str, Any]:
        """Return basic stats for a collection: source count, chunk count."""
        store = self._get_store(collection or self.collection_name)
        try:
            data = store.get()
            metas = data.get("metadatas", [])
            sources = {m.get("source", "") for m in metas}
            return {
                "collection": collection or self.collection_name,
                "chunks": len(metas),
                "sources": len(sources),
            }
        except Exception:
            return {"collection": collection or self.collection_name, "chunks": 0, "sources": 0}

    # ── Legacy compatibility ──────────────────────────────────────────────────

    def add(self, text: str, metadata: dict[str, Any] | None = None) -> None:
        """Add a single document to the memory collection (legacy API)."""
        doc = Document(page_content=text, metadata=metadata or {})
        self._get_store("memory").add_documents([doc])

    def add_session_turn(self, role: str, content: str, agent_name: str, thread_id: str) -> None:
        self.add(
            f"[{role}]: {content}",
            metadata={"role": role, "agent": agent_name, "thread_id": thread_id},
        )


# ── Tool factories ────────────────────────────────────────────────────────────


def make_rag_tools(rag: RAGMemory):
    """Create langchain tools bound to a RAGMemory instance."""

    @tool
    def rag_save(text: str) -> str:
        """Save a fact or note to the semantic memory store for later retrieval."""
        rag.ingest_text(text, source_id=f"manual:{hash(text)}", collection="memory")
        return "Saved to vector memory."

    @tool
    def rag_search(query: str, k: int = 5) -> str:
        """Search vector memory (all collections) for semantically similar content."""
        results = rag.search_all(query, k=k)
        if not results:
            return "No relevant content found."
        lines = [
            f"[{i + 1}] ({r['collection']}) score={r.get('score', '?'):.3f}\n    {r['content'][:400]}"
            for i, r in enumerate(results)
        ]
        return "\n\n".join(lines)

    return [rag_save, rag_search]
