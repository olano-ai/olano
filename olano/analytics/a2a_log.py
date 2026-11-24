"""Shared reader for the append-only agent-to-agent (A2A) activity log.

``<home>/logs/a2a_activity.jsonl`` is written one JSON envelope per line from
the SSE broadcast chokepoint (``runtime/api.py:_push_event``) and never rotated.
Both the dashboard's activity endpoints and the analytics rollups need the
recent tail of it, so the read lives here once instead of being forked per
call site — and it reads the **tail by bytes** rather than pulling the whole
(unbounded) file into memory.

Everything here is read-only and side-effect free.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)

# Byte budget per requested event. A2A envelopes carry whole messages *and*
# replies, so this is deliberately generous; the clamps are what actually bound
# the read, and `read_a2a_events` grows the budget when the tail comes up short.
_BYTES_PER_EVENT = 8 * 1024
_MIN_TAIL_BYTES = 256 * 1024
_MAX_TAIL_BYTES = 32 * 1024 * 1024


def a2a_log_path() -> Path:
    """Path of the A2A activity log. Does not create anything."""
    return get_olano_paths().logs_dir / "a2a_activity.jsonl"


def norm_agent(name: str) -> str:
    """Case-stable normalization for agent names used in pair keys.

    Mirrors ``tools.talk_to._norm_agent`` so ``Kevin`` and ``kevin`` never fork
    into two conversations.
    """
    return (name or "agent").strip().casefold()


def canonical_pair(a: str, b: str) -> str:
    """Unordered pair key ``"<lo>::<hi>"``.

    Both directions of a conversation share one key: the runtime keeps
    *directional* checkpointer threads (``acp::<caller>::<callee>``) so each
    side has its own model context, but to a reader ``a → b`` and ``b → a`` are
    one conversation. This is the same key the opt-in A2A digest uses for its
    store namespace.
    """
    lo, hi = sorted((norm_agent(a), norm_agent(b)))
    return f"{lo}::{hi}"


def iter_a2a_events() -> Iterator[dict[str, Any]]:
    """Every parsed envelope in the log, oldest→newest, streamed line by line.

    For callers that must see the **whole** history rather than a recent tail —
    the daily journal looks up an arbitrary past day, so bounding it would
    silently drop older days. Streams instead of materializing the file.
    """
    path = a2a_log_path()
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    yield parsed
    except OSError:
        logger.debug("Failed to stream A2A activity log", exc_info=True)


def _tail_lines(path: Path, max_bytes: int) -> tuple[list[bytes], bool]:
    """Return ``(lines, whole_file)`` for the last ``max_bytes`` of ``path``.

    ``whole_file`` is True when the budget covered the file from byte 0, so the
    caller can tell "that's all there is" from "the budget ran out".
    """
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        start = max(0, size - max_bytes)
        fh.seek(start)
        blob = fh.read()
    lines = blob.split(b"\n")
    # A mid-file seek almost always lands inside a record — drop that fragment.
    if start > 0:
        lines = lines[1:]
    return lines, start == 0


def read_a2a_events(limit: int = 500) -> list[dict[str, Any]]:
    """Parsed A2A envelopes, oldest→newest, capped at ``limit`` events.

    Malformed lines are skipped (a torn final line during a concurrent append
    is normal). When the first tail read yields fewer than ``limit`` events and
    there is more file behind it, the budget grows and the read is retried.
    """
    limit = max(1, int(limit))
    path = a2a_log_path()
    if not path.exists():
        return []
    budget = min(_MAX_TAIL_BYTES, max(_MIN_TAIL_BYTES, limit * _BYTES_PER_EVENT))
    while True:
        try:
            lines, whole_file = _tail_lines(path, budget)
        except OSError:
            logger.debug("Failed to read A2A activity log", exc_info=True)
            return []
        events: list[dict[str, Any]] = []
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        if len(events) >= limit or whole_file or budget >= _MAX_TAIL_BYTES:
            return events[-limit:]
        budget = min(_MAX_TAIL_BYTES, budget * 4)
