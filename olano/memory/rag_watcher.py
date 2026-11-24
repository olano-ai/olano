"""
File-system watcher that keeps a RAG collection in sync with a directory.

Uses watchdog (already in deps) in a background thread:
  - File created / modified → re-ingest into the collection
  - File deleted            → remove chunks from collection
  - File moved / renamed    → remove old path, ingest new path

Usage
-----
    from olano.memory.rag_watcher import RAGDirectoryWatcher

    watcher = RAGDirectoryWatcher(rag, collection="knowledgebase", directory=kb_dir)
    watcher.start()   # non-blocking, runs in background thread
    ...
    watcher.stop()
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from olano.memory.rag_memory import RAGMemory

logger = logging.getLogger(__name__)

_SKIP_EXTENSIONS: frozenset[str] = frozenset(
    {
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
    }
)

# Serialise concurrent ingest/delete so Chroma is never written from two threads
_WATCHER_LOCK = threading.Lock()


@contextmanager
def _ingest_slot() -> Iterator[None]:
    """Hold both the watcher lock and the process-wide background-ingest gate.

    ``_WATCHER_LOCK`` alone only orders the watchers against each other; the
    boot warm-up runs on its own thread behind ``BACKGROUND_INGEST_GATE``, so
    without taking that too, a file saved into a knowledgebase during a boot
    embeds *alongside* the warm-up and doubles the CPU the throttle is there to
    bound. Gate first, then the lock — the same order builder.py uses, so the
    two can never deadlock against each other.
    """
    from olano.memory.rag_memory import BACKGROUND_INGEST_GATE

    with BACKGROUND_INGEST_GATE, _WATCHER_LOCK:
        yield


class RAGDirectoryWatcher:
    """
    Watches a directory and keeps a RAG collection in sync (background thread).
    watchdog is imported lazily — if not installed the watcher silently disables.
    """

    def __init__(
        self,
        rag: RAGMemory,
        collection: str,
        directory: str | Path,
    ) -> None:
        self.rag = rag
        self.collection = collection
        self.directory = Path(directory)
        self._observer: Any = None

    @property
    def is_running(self) -> bool:
        return self._observer is not None and self._observer.is_alive()

    def start(self) -> bool:
        """
        Start the background observer.  Returns True if started, False if
        watchdog is not installed (non-fatal — watcher is optional).
        """
        if self.is_running:
            return True
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            logger.warning(
                "watchdog not installed — real-time RAG sync disabled. Run: uv add watchdog"
            )
            return False

        self.directory.mkdir(parents=True, exist_ok=True)

        class _Handler(FileSystemEventHandler):
            def __init__(h_self):
                super().__init__()

            def _ok(h_self, path: str) -> bool:
                return Path(path).suffix.lower() not in _SKIP_EXTENSIONS

            def on_created(h_self, event: Any) -> None:
                if event.is_directory or not h_self._ok(event.src_path):
                    return
                with _ingest_slot():
                    try:
                        n = self.rag.ingest_file(event.src_path, collection=self.collection)
                        if n:
                            logger.info("RAG watcher: +%d chunks ← %s", n, event.src_path)
                    except Exception as e:
                        logger.warning("RAG watcher ingest error: %s", e)

            def on_modified(h_self, event: Any) -> None:
                h_self.on_created(event)

            def on_deleted(h_self, event: Any) -> None:
                if event.is_directory:
                    return
                with _WATCHER_LOCK:
                    try:
                        n = self.rag.delete_source(event.src_path, collection=self.collection)
                        if n:
                            logger.info("RAG watcher: -%d chunks ← %s", n, event.src_path)
                    except Exception as e:
                        logger.warning("RAG watcher delete error: %s", e)

            def on_moved(h_self, event: Any) -> None:
                if event.is_directory:
                    return
                with _ingest_slot():
                    try:
                        self.rag.delete_source(event.src_path, collection=self.collection)
                        if h_self._ok(event.dest_path):
                            n = self.rag.ingest_file(event.dest_path, collection=self.collection)
                            if n:
                                logger.info(
                                    "RAG watcher: moved %s → %s", event.src_path, event.dest_path
                                )
                    except Exception as e:
                        logger.warning("RAG watcher move error: %s", e)

        self._observer = Observer()
        self._observer.schedule(_Handler(), str(self.directory), recursive=True)
        self._observer.start()
        logger.info(
            "RAG watcher started: %s → collection '%s'",
            self.directory,
            self.collection,
        )
        return True

    def stop(self) -> None:
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception as e:
                logger.debug("RAG watcher stop error: %s", e)
            self._observer = None

    def __repr__(self) -> str:
        return (
            f"RAGDirectoryWatcher(collection={self.collection!r}, "
            f"dir={self.directory!r}, running={self.is_running})"
        )


class NotesDirectoryWatcher:
    """Watches the notes/ directory and re-imports changed .md files into NotesStore.

    P5.12: users can edit notes in any markdown editor; the watcher keeps
    the SQLite index in sync automatically.
    """

    def __init__(self, notes: Any, notes_dir: str | Path) -> None:
        self.notes = notes
        self.notes_dir = Path(notes_dir)
        self._observer: Any = None

    @property
    def is_running(self) -> bool:
        return self._observer is not None and self._observer.is_alive()

    def start(self) -> bool:
        """Start the background observer. Returns True if started."""
        if self.is_running:
            return True
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            logger.warning("watchdog not installed — real-time notes sync disabled.")
            return False

        self.notes_dir.mkdir(parents=True, exist_ok=True)
        notes = self.notes

        class _NotesHandler(FileSystemEventHandler):
            def _is_notes_md(h_self, path: str) -> bool:
                p = Path(path)
                return p.suffix.lower() == ".md" and not p.name.startswith(".")

            def on_created(h_self, event: Any) -> None:
                if not event.is_directory and h_self._is_notes_md(event.src_path):
                    with _WATCHER_LOCK:
                        try:
                            notes.sync_file(Path(event.src_path))
                            logger.info("Notes watcher: synced %s", event.src_path)
                        except Exception as e:
                            logger.warning("Notes watcher sync error: %s", e)

            def on_modified(h_self, event: Any) -> None:
                h_self.on_created(event)

            def on_deleted(h_self, event: Any) -> None:
                if not event.is_directory and h_self._is_notes_md(event.src_path):
                    with _WATCHER_LOCK:
                        try:
                            title = Path(event.src_path).stem
                            notes.delete(title)
                            logger.info("Notes watcher: removed '%s'", title)
                        except Exception as e:
                            logger.warning("Notes watcher delete error: %s", e)

            def on_moved(h_self, event: Any) -> None:
                if event.is_directory:
                    return
                with _WATCHER_LOCK:
                    try:
                        old_title = Path(event.src_path).stem
                        notes.delete(old_title)
                        if h_self._is_notes_md(event.dest_path):
                            notes.sync_file(Path(event.dest_path))
                    except Exception as e:
                        logger.warning("Notes watcher move error: %s", e)

        self._observer = Observer()
        self._observer.schedule(_NotesHandler(), str(self.notes_dir), recursive=False)
        self._observer.start()
        logger.info("Notes watcher started: %s", self.notes_dir)
        return True

    def stop(self) -> None:
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=5)
            except Exception as e:
                logger.debug("Notes watcher stop error: %s", e)
            self._observer = None

    def __repr__(self) -> str:
        return f"NotesDirectoryWatcher(dir={self.notes_dir!r}, running={self.is_running})"


VaultDirectoryWatcher = NotesDirectoryWatcher
