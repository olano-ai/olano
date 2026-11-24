"""Thumbs up / down on an agent's replies, kept on the box.

One file per thread beside its transcript,
``<home>/sessions/<agent>/<thread_id>.feedback.ndjson``, one row per vote:
``{ts, turn_id, trace_id, user, value, comment}``. ``value`` is ``1`` (up),
``-1`` (down) or ``0`` (the vote was taken back); the last row per
``(turn_id, user)`` is that person's vote. Append-only, so a changed vote is a
new row and nothing is ever rewritten in place.

This works with Langfuse off and never needs it: Langfuse is told about a vote
as a ``user_feedback`` score only when the reply's turn was traced
(``observability/feedback.py``). A vote names a turn by the ``turn_id`` the
runner stamps on the turn's assistant rows (``runner._TURN_ID``); rows written
before that stamp existed carry none and cannot be voted on.

Deleting a thread deletes this file with it (``routes/threads.delete_thread``).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)

# Deliberately not `.jsonl`: thread listing, housekeeping (compress / delete),
# self-learning, analytics and diagnostics all glob `*.jsonl` in the sessions
# directory as transcripts, and a vote file read as one becomes a phantom thread.
FEEDBACK_SUFFIX = ".feedback.ndjson"
COMMENT_MAX_CHARS = 2000
VALUES = (1, -1, 0)

_lock = threading.Lock()


class FeedbackError(ValueError):
    """A vote the store refuses (bad value, unknown turn)."""


@dataclass(frozen=True)
class Vote:
    turn_id: str
    value: int
    comment: str
    ts: str


def sessions_dir(agent: str) -> Path:
    return get_olano_paths().sessions_dir / agent


def safe_thread_id(thread_id: str) -> bool:
    """A thread id that names a file in the agent's sessions directory and
    nothing outside it."""
    return bool(
        thread_id
        and len(thread_id) <= 512
        and "/" not in thread_id
        and "\\" not in thread_id
        and "\x00" not in thread_id
        and not thread_id.startswith(".")
    )


def feedback_path(agent: str, thread_id: str) -> Path:
    return sessions_dir(agent) / f"{thread_id}{FEEDBACK_SUFFIX}"


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        logger.debug("feedback: %s unreadable", path, exc_info=True)
    return rows


def _transcript_rows(agent: str, thread_id: str) -> list[dict[str, Any]]:
    import gzip

    base = sessions_dir(agent)
    plain = base / f"{thread_id}.jsonl"
    if plain.exists():
        return _read_rows(plain)
    gz = base / f"{thread_id}.jsonl.gz"
    if gz.exists():
        try:
            with gzip.open(gz, "rt", encoding="utf-8") as f:
                return [
                    r
                    for r in (json.loads(line) for line in f if line.strip())
                    if isinstance(r, dict)
                ]
        except (OSError, ValueError):
            logger.debug("feedback: %s unreadable", gz, exc_info=True)
    return []


def turn_trace_id(agent: str, thread_id: str, turn_id: str) -> str | None:
    """The trace id of ``turn_id`` in the thread's transcript: ``""`` when the
    turn is there but was not traced, ``None`` when no assistant row of the
    thread carries that turn id. The transcript is the authority, so a client
    can neither vote on a reply that does not exist nor aim a score at a trace
    of its choosing."""
    found: str | None = None
    for row in _transcript_rows(agent, thread_id):
        if row.get("role") != "assistant" or row.get("turn_id") != turn_id:
            continue
        trace = row.get("trace_id")
        found = trace if isinstance(trace, str) else (found or "")
    return found


def record_vote(
    agent: str,
    thread_id: str,
    turn_id: str,
    *,
    user: str,
    value: int,
    comment: str = "",
    trace_id: str = "",
) -> Vote:
    """Append ``user``'s vote on ``turn_id``. ``value`` 0 takes it back."""
    if value not in VALUES:
        raise FeedbackError("value must be 1, -1 or 0")
    if not safe_thread_id(thread_id):
        raise FeedbackError("invalid thread id")
    comment = (comment or "").strip()[:COMMENT_MAX_CHARS] if value else ""
    ts = datetime.now(timezone.utc).isoformat()
    row = {
        "ts": ts,
        "turn_id": turn_id,
        "trace_id": trace_id or None,
        "user": user,
        "value": value,
        "comment": comment or None,
    }
    path = feedback_path(agent, thread_id)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    return Vote(turn_id=turn_id, value=value, comment=comment, ts=ts)


def _latest(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        turn = row.get("turn_id")
        user = row.get("user")
        if isinstance(turn, str) and isinstance(user, str):
            latest[(turn, user)] = row
    return latest


def votes_for(agent: str, thread_id: str, user: str) -> dict[str, Vote]:
    """``user``'s standing votes in the thread, by turn id (taken-back votes
    are absent)."""
    if not safe_thread_id(thread_id):
        return {}
    out: dict[str, Vote] = {}
    for (turn, who), row in _latest(_read_rows(feedback_path(agent, thread_id))).items():
        value = row.get("value")
        if who != user or value not in (1, -1):
            continue
        out[turn] = Vote(
            turn_id=turn,
            value=int(value),
            comment=str(row.get("comment") or ""),
            ts=str(row.get("ts") or ""),
        )
    return out


def delete_feedback(agent: str, thread_id: str) -> bool:
    if not safe_thread_id(thread_id):
        return False
    path = feedback_path(agent, thread_id)
    with _lock:
        if path.exists():
            path.unlink()
            return True
    return False


def summarize(since: datetime, agents: set[str] | None = None) -> dict[str, Any]:
    """Standing up / down votes cast since ``since``, per agent.

    Read from the files alone (no Langfuse). A vote counts in the window its
    latest row was written in; a vote taken back counts nowhere.
    """
    root = get_olano_paths().sessions_dir
    by_agent: dict[str, dict[str, int]] = {}
    if root.is_dir():
        for agent_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            agent = agent_dir.name
            if agents is not None and agent not in agents:
                continue
            for path in agent_dir.glob(f"*{FEEDBACK_SUFFIX}"):
                for row in _latest(_read_rows(path)).values():
                    value = row.get("value")
                    if value not in (1, -1):
                        continue
                    try:
                        when = datetime.fromisoformat(str(row.get("ts")))
                    except ValueError:
                        continue
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    if when < since:
                        continue
                    counts = by_agent.setdefault(agent, {"up": 0, "down": 0})
                    counts["up" if value == 1 else "down"] += 1
    rows = [{"agent": a, **c} for a, c in sorted(by_agent.items())]
    return {
        "since": since.isoformat(),
        "up": sum(r["up"] for r in rows),
        "down": sum(r["down"] for r in rows),
        "by_agent": rows,
    }
