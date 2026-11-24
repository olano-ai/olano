"""What the thread list needs from one transcript, remembered per file version.

``GET /api/agents/{name}/threads`` labels every transcript an agent has ever
had, and it used to read and JSON-parse each of them in full on every call to
get a title, a preview, two timestamps and a count. Cost grew with the agent's
whole history (every tool result included), so the agent someone used most was
the one whose list took longest; a phone asking for it at launch waited past
its timeout and opened an empty chat. Every ``thread_updated`` frame made each
open client ask again.

A transcript is append-only, so a summary of it stays valid until the file's
``(mtime_ns, size, inode)`` moves. The list stats every file and re-reads only
those that changed, and the summaries are kept in two tiers: an in-process LRU
and an SQLite file under the home's analytics directory (``thread_summaries.db``,
derived data, safe to delete). The on-disk tier is what makes a restart free:
without it the first list after every restart read the agent's whole history
again, and on the box that mattered that first read outlasted the phone's
request timeout (the runtime had restarted for an update a minute earlier). Now
a cold process reads only the transcripts that changed while it was down, and
the boot warm (``warm_directory``) pays even that off the request path.

The summary keeps the transcript rows ``derive_label`` reads (the head row, the
first row with provenance, the first human turn, the last row), in their
original order, so a label derived from them is the label the full transcript
gives. The things the list resolves per request (scheduler task names, the
channel recovered from the chat directory, stored names, cost) stay out of it.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Upper bound on remembered transcripts, across all agents (an LRU).
MAX_CACHED = 20000

#: A stored head row keeps at most this many characters per string field. The
#: label is condensed to 120 characters after the directives are stripped, so
#: nothing a row is used for reaches this far; it bounds the row a pasted
#: document would otherwise put on disk twice.
STORED_FIELD_CHARS = 8000

#: ``LIONCLAW_THREAD_SUMMARY_DB=0`` keeps the summaries in memory only.
_DISABLE_ENV = "LIONCLAW_THREAD_SUMMARY_DB"

_HUMAN_ROLES = frozenset({"user", "peer_inbound"})
_COUNTED_ROLES = frozenset({"user", "peer_inbound", "assistant", "tool_call", "tool_result"})


@dataclass(frozen=True)
class TranscriptSummary:
    #: The rows ``derive_label`` reads, in transcript order (hidden rows excluded).
    head: tuple[Mapping, ...]
    #: The first human turn, or the last row when there is none (the preview source).
    preview_source: Mapping | None
    message_count: int
    created_at: str | None
    #: The last row's timestamp ("" when it has none; the caller falls back to mtime).
    last_ts: str
    #: The model the newest stamped assistant row ran on ("" when unknown).
    model: str
    reasoning_effort: str
    #: A compaction successor's predecessor id ("" otherwise).
    supersedes: str
    empty: bool


EMPTY = TranscriptSummary((), None, 0, None, "", "", "", "", True)


def summarize(entries: Sequence[Mapping], *, supersedes: str = "") -> TranscriptSummary:
    """Summarize visible transcript rows (the caller drops ``hidden`` ones)."""
    if not entries:
        return EMPTY
    first_user = next((i for i, e in enumerate(entries) if e.get("role") in _HUMAN_ROLES), None)
    provenance = next(
        (i for i, e in enumerate(entries) if e.get("channel") or e.get("origin")), None
    )
    keep = sorted({0, len(entries) - 1} | {i for i in (first_user, provenance) if i is not None})
    model = effort = ""
    for e in reversed(entries):
        if e.get("role") == "assistant" and e.get("model"):
            model = str(e["model"])
            effort = str(e.get("reasoning_effort") or "")
            break
    last = entries[-1]
    return TranscriptSummary(
        head=tuple(entries[i] for i in keep),
        preview_source=entries[first_user] if first_user is not None else last,
        message_count=sum(1 for e in entries if e.get("role") in _COUNTED_ROLES),
        created_at=entries[0].get("ts"),
        last_ts=str(last.get("ts") or ""),
        model=model,
        reasoning_effort=effort,
        supersedes=supersedes,
        empty=False,
    )


# ── Reading a transcript ─────────────────────────────────────────────────────

_SUPERSEDES = re.compile(r"Prior thread:\s*(\S+)")


def read_transcript_text(path: Path) -> str:
    """The transcript's text; housekeeping gzips old ones (``<thread>.jsonl.gz``)."""
    if path.suffix == ".gz":
        return gzip.decompress(path.read_bytes()).decode("utf-8")
    return path.read_text(encoding="utf-8")


def parse_jsonl(text: str) -> list[dict]:
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def summarize_transcript(path: Path) -> TranscriptSummary:
    """Read one transcript and summarize its visible rows.

    A transcript deleted or unreadable between the directory listing and the
    read sits the list out (``EMPTY``) rather than failing it; its stat moves
    the moment it is readable again, so nothing stale sticks.
    """
    try:
        rows = parse_jsonl(read_transcript_text(path))
    except OSError:
        logger.debug("thread list: transcript unreadable, skipped: %s", path, exc_info=True)
        return EMPTY
    # Hidden prompt rows never reach a human surface: not the preview, not the
    # derived title, not the count (same rule as the transcript endpoint).
    entries = [e for e in rows if not e.get("hidden")]
    supersedes = ""
    seed = entries[0].get("content", "") if entries else ""
    # An auto-compaction successor names the thread it replaces in its seed row.
    if isinstance(seed, str) and seed.startswith("[Conversation compacted]"):
        m = _SUPERSEDES.search(seed)
        if m:
            supersedes = m.group(1)
    return summarize(entries, supersedes=supersedes)


# ── The on-disk tier ─────────────────────────────────────────────────────────


def _clip_row(row: Mapping) -> dict:
    return {
        k: (v[:STORED_FIELD_CHARS] if isinstance(v, str) and len(v) > STORED_FIELD_CHARS else v)
        for k, v in row.items()
    }


def encode_summary(summary: TranscriptSummary) -> str:
    """The summary as the JSON the store keeps."""
    head = [_clip_row(r) for r in summary.head]
    preview = None
    if summary.preview_source is not None:
        # The preview source is one of the head rows; keep its position, not a copy.
        preview = next((i for i, r in enumerate(summary.head) if r is summary.preview_source), None)
        if preview is None:
            head.append(_clip_row(summary.preview_source))
            preview = len(head) - 1
    return json.dumps(
        {
            "head": head,
            "preview": preview,
            "count": summary.message_count,
            "created": summary.created_at,
            "last": summary.last_ts,
            "model": summary.model,
            "effort": summary.reasoning_effort,
            "supersedes": summary.supersedes,
            "empty": summary.empty,
        },
        ensure_ascii=False,
    )


def decode_summary(data: str) -> TranscriptSummary | None:
    """The summary a stored row holds, or None when the row is not one."""
    try:
        doc = json.loads(data)
        head = tuple(doc["head"])
        preview = doc["preview"]
        return TranscriptSummary(
            head=head,
            preview_source=head[preview] if preview is not None else None,
            message_count=int(doc["count"]),
            created_at=doc["created"],
            last_ts=str(doc["last"]),
            model=str(doc["model"]),
            reasoning_effort=str(doc["effort"]),
            supersedes=str(doc["supersedes"]),
            empty=bool(doc["empty"]),
        )
    except (ValueError, KeyError, TypeError, IndexError):
        return None


class SummaryStore:
    """Summaries keyed on the transcript's path and stat, in one SQLite file.

    One connection, serialized by a lock: the list runs in worker threads and
    two of them can list the same agent at once (the app asks for the newest
    conversation and the first page together). WAL with ``synchronous=NORMAL``
    so a few thousand writes on the first build never fsync one by one.
    """

    def __init__(self, db_path: Path) -> None:
        self.path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=5.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS summaries ("
            " path TEXT PRIMARY KEY, dir TEXT NOT NULL,"
            " mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, inode INTEGER NOT NULL,"
            " data TEXT NOT NULL)"
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS summaries_dir ON summaries(dir)")
        self._conn.commit()

    def get(self, path: Path, key: tuple[int, int, int]) -> TranscriptSummary | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT mtime_ns, size, inode, data FROM summaries WHERE path = ?", (str(path),)
            ).fetchone()
        if row is None or tuple(row[:3]) != key:
            return None
        return decode_summary(row[3])

    def put(self, path: Path, key: tuple[int, int, int], summary: TranscriptSummary) -> None:
        data = encode_summary(summary)
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO summaries (path, dir, mtime_ns, size, inode, data)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (str(path), str(path.parent), *key, data),
            )
            self._conn.commit()

    def paths_in(self, directory: Path) -> set[Path]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM summaries WHERE dir = ?", (str(directory),)
            ).fetchall()
        return {Path(r[0]) for r in rows}

    def delete(self, paths: Iterable[Path]) -> None:
        doomed = [(str(p),) for p in paths]
        if not doomed:
            return
        with self._lock:
            self._conn.executemany("DELETE FROM summaries WHERE path = ?", doomed)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_stores_lock = threading.Lock()
_stores: dict[Path, SummaryStore | None] = {}


def store_path() -> Path:
    from olano.config.home import get_olano_paths

    return get_olano_paths().thread_summaries_db_path


def _store() -> SummaryStore | None:
    """The store for the active home, opened once; None when off or unusable.

    A store that cannot be opened (read-only disk, a directory where the file
    should be) costs the on-disk tier and nothing else: the list keeps working
    from memory, as it did before the tier existed.
    """
    if os.environ.get(_DISABLE_ENV, "").strip().lower() in {"0", "false", "no", "off"}:
        return None
    try:
        path = store_path()
    except Exception:
        return None
    with _stores_lock:
        if path in _stores:
            return _stores[path]
        try:
            store: SummaryStore | None = SummaryStore(path)
        except (sqlite3.Error, OSError) as e:
            logger.warning("thread summaries: on-disk store unavailable at %s: %s", path, e)
            store = None
        _stores[path] = store
        return store


# ── The in-process tier ──────────────────────────────────────────────────────

_lock = threading.Lock()
_cache: OrderedDict[Path, tuple[tuple[int, int, int], TranscriptSummary]] = OrderedDict()
#: The cached paths of each sessions directory, so a list request forgets its
#: agent's deleted transcripts without walking every agent's.
_by_dir: dict[Path, set[Path]] = {}


def _drop(path: Path) -> None:
    del _cache[path]
    siblings = _by_dir.get(path.parent)
    if siblings is not None:
        siblings.discard(path)
        if not siblings:
            del _by_dir[path.parent]


def _key(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def cached_summary(
    path: Path,
    compute: Callable[[Path], TranscriptSummary],
) -> TranscriptSummary:
    """The summary of ``path``, computed only when the file changed since last time.

    The stat is taken before the read, so a file that grows while it is being
    read is stored under its old stat and read again on the next call.
    """
    key = _key(path)
    if key is None:
        return EMPTY
    with _lock:
        hit = _cache.get(path)
        if hit is not None and hit[0] == key:
            _cache.move_to_end(path)
            return hit[1]
    store = _store()
    summary: TranscriptSummary | None = None
    if store is not None:
        summary = _stored_get(store, path, key)
    if summary is None:
        summary = compute(path)
        if store is not None:
            _stored_put(store, path, key, summary)
    with _lock:
        _cache[path] = (key, summary)
        _cache.move_to_end(path)
        _by_dir.setdefault(path.parent, set()).add(path)
        while len(_cache) > MAX_CACHED:
            _drop(next(iter(_cache)))
    return summary


def _stored_get(
    store: SummaryStore, path: Path, key: tuple[int, int, int]
) -> TranscriptSummary | None:
    try:
        return store.get(path, key)
    except (sqlite3.Error, OSError) as e:
        logger.debug("thread summaries: store read failed: %s", e)
        return None


def _stored_put(
    store: SummaryStore, path: Path, key: tuple[int, int, int], summary: TranscriptSummary
) -> None:
    try:
        store.put(path, key, summary)
    except (sqlite3.Error, OSError) as e:
        # A full or read-only disk costs the on-disk tier, never the list.
        logger.debug("thread summaries: store write failed: %s", e)


def forget_missing(directory: Path, present: Iterable[Path]) -> None:
    """Drop remembered transcripts of ``directory`` that are no longer on disk."""
    keep = set(present)
    with _lock:
        for path in [p for p in _by_dir.get(directory, ()) if p not in keep]:
            _drop(path)
    store = _store()
    if store is not None:
        try:
            store.delete(p for p in store.paths_in(directory) if p not in keep)
        except (sqlite3.Error, OSError) as e:
            logger.debug("thread summaries: store prune failed: %s", e)


def transcripts_in(directory: Path) -> dict[str, Path]:
    """thread id -> transcript, compressed sessions (``<thread>.jsonl.gz``, from
    housekeeping) included and the plain file winning if both somehow exist."""
    by_thread: dict[str, Path] = {}
    for gz in directory.glob("*.jsonl.gz"):
        by_thread[gz.name[: -len(".jsonl.gz")]] = gz
    for plain in directory.glob("*.jsonl"):
        by_thread[plain.stem] = plain
    return by_thread


def warm_directory(directory: Path) -> int:
    """Summarize every transcript in one agent's sessions directory.

    Run from the boot warm thread: afterwards the first list request after a
    restart reads no transcript at all, and the first list ever reads none on
    the request path. Returns how many transcripts had to be read.
    """
    if not directory.is_dir():
        return 0
    read = 0

    def counting(path: Path) -> TranscriptSummary:
        nonlocal read
        read += 1
        return summarize_transcript(path)

    files = transcripts_in(directory)
    for path in files.values():
        cached_summary(path, counting)
    forget_missing(directory, files.values())
    return read


def clear() -> None:
    """Forget everything in memory, and close the stores (tests)."""
    with _lock:
        _cache.clear()
        _by_dir.clear()
    with _stores_lock:
        for store in _stores.values():
            if store is not None:
                try:
                    store.close()
                except sqlite3.Error:
                    pass
        _stores.clear()
