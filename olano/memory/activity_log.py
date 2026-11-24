"""Deterministic daily activity log — what the agent *did*, written without a model.

The self-learning distiller (``self_learning.py``) extracts *durable facts* from
the transcripts and, by design, drops the transient record of work: a completed
scheduled run, a peer request answered, a Cortex phase that produced something.
So an agent asked "what did you do yesterday?" read a daily log that held the
roster facts it had learned and none of the work it had done.

This pass writes that record. It reads the same primary sources the dashboard's
work journal reads — the agent's own JSONL transcripts under
``<home>/sessions/<agent>/`` and the cognition audit log in the workspace — and
appends a bounded ``Activity:`` block to ``memories/<day>.md`` for the UTC day
each event happened on. Only *machinery* is logged: scheduled runs (config
schedule, DB tasks, the issue poll), heartbeat findings, peer (agent-to-agent)
exchanges and Cortex phases. Human conversations are left to the distiller — a
customer's chat is not the agent's activity, and copying it into a file that is
loaded into every prompt would be both noise and a privacy regression.

Three properties are load-bearing:

* **No model call.** The distiller shares one daily deep-run cap with every
  Cortex phase and is skipped when it is spent — exactly on the busy days this
  record matters most. This pass costs file I/O and runs every cycle.
* **Idempotent across restarts.** Byte offsets per transcript and a timestamp
  watermark for the audit log are persisted under the workspace state dir, so a
  redeploy never replays a day into the log twice. A transcript that shrank
  (rotated, truncated) resets its offset instead of seeking past EOF.
* **Bounded.** Today's and yesterday's daily logs are loaded into the prompt
  *uncapped* (``prompt_assembler.build_l1``), so a busy agent could otherwise
  balloon its own context. Lines are truncated, capped per day, and events
  older than :data:`MAX_EVENT_AGE_DAYS` are consumed but never written — that
  also stops the first run on a long-lived box from back-filling months of
  history into files the roll-up already archived.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from olano.config.home import get_olano_paths
from olano.config.workspace import WORKSPACE_STATE_DIRNAME
from olano.memory.markdown_memory import MarkdownMemory, atomic_write_text

logger = logging.getLogger(__name__)

#: Most activity lines written per UTC day, across every pass. Sixty lines at
#: ~180 chars is ~11 KB — roughly half of what MEMORY.md is allowed in the prompt.
MAX_LINES_PER_DAY = 60
#: Hard cap on one rendered line (prefix + excerpts).
MAX_LINE_CHARS = 180
#: Excerpt budget for the prompt side and the reply side of one line.
PROMPT_EXCERPT_CHARS = 70
REPLY_EXCERPT_CHARS = 90
#: Events older than this are consumed (offsets advance) but never written.
MAX_EVENT_AGE_DAYS = 2
#: A trailing prompt with no answer yet is re-read next pass so the pair is
#: logged whole — unless it is this old, in which case the turn died and the
#: row is consumed. Comfortably above the worker timeout and any chat turn.
DANGLING_PROMPT_MAX_AGE = timedelta(hours=2)

_HEARTBEAT_OK = "HEARTBEAT_OK"
_HEARTBEAT_PREFIX = "heartbeat_"
_ISSUES_PREFIX = "issues_"
_SCHEDULED_PREFIX = "scheduled_"
_PROMPT_ROLES = ("user", "peer_inbound")
_WS_RE = re.compile(r"\s+")


@dataclass
class _Event:
    ts: str  # ISO-8601 UTC as persisted on the row
    line: str

    @property
    def day(self) -> str:
        return self.ts[:10]


@dataclass
class _State:
    """Persisted watermark. ``sessions`` is keyed per agent so two agents that
    share a workspace (and therefore this file) keep separate transcript
    offsets, while the audit watermark is shared on purpose: the workspace's
    Cortex is one Cortex, and logging each phase once per agent that shares it
    would duplicate every line."""

    sessions: dict[str, dict[str, int]] = field(default_factory=dict)
    audit_ts: str = ""
    days: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> _State:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        state = cls(audit_ts=str(raw.get("audit_ts") or ""))
        sessions = raw.get("sessions")
        if isinstance(sessions, dict):
            for agent, offsets in sessions.items():
                if isinstance(offsets, dict):
                    state.sessions[str(agent)] = {
                        str(p): int(o) for p, o in offsets.items() if isinstance(o, int)
                    }
        days = raw.get("days")
        if isinstance(days, dict):
            state.days = {str(d): dict(v) for d, v in days.items() if isinstance(v, dict)}
        return state

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            path,
            json.dumps(
                {"sessions": self.sessions, "audit_ts": self.audit_ts, "days": self.days},
                indent=1,
                sort_keys=True,
            ),
        )


def _excerpt(text: str, limit: int) -> str:
    flat = _WS_RE.sub(" ", str(text or "")).strip()
    if len(flat) <= limit:
        return flat
    return flat[: max(1, limit - 1)].rstrip() + "…"


def _parse_ts(ts: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clock(ts: str) -> str:
    parsed = _parse_ts(ts)
    return parsed.strftime("%H:%M") if parsed else "--:--"


def _render(ts: str, label: str, prompt: str, reply: str) -> str:
    line = f"- {_clock(ts)} {label}"
    body = _excerpt(prompt, PROMPT_EXCERPT_CHARS)
    tail = _excerpt(reply, REPLY_EXCERPT_CHARS)
    if body:
        line += f": {body}"
    if tail:
        line += f" → {tail}"
    if len(line) > MAX_LINE_CHARS:
        line = line[: MAX_LINE_CHARS - 1].rstrip() + "…"
    return line


def _thread_kind(stem: str) -> str:
    """``heartbeat`` | ``issues`` | ``scheduled`` | ``""`` from a thread file stem."""
    from olano.core.thread_labels import strip_human_namespace

    stem = strip_human_namespace(stem)
    if stem.startswith(_HEARTBEAT_PREFIX):
        return "heartbeat"
    if stem.startswith(_ISSUES_PREFIX):
        return "issues"
    if stem.startswith(_SCHEDULED_PREFIX):
        return "scheduled"
    return ""


def _heartbeat_task(content: str) -> str:
    """``"HEARTBEAT: <task>\\n\\nContext: …"`` → ``<task>`` (first line)."""
    text = content.strip()
    if text.upper().startswith("HEARTBEAT:"):
        text = text.split(":", 1)[1]
    text = text.strip()
    return text.splitlines()[0] if text else ""


def _read_records(path: Path, offset: int) -> list[tuple[dict[str, Any] | None, int]]:
    """``(row-or-None, byte offset after the line)`` for every line after
    ``offset``. Mirrors the distiller's byte-offset reader, plus the shrink
    guard: an offset past EOF means the file was rotated or truncated, and
    seeking there would read nothing forever. Unparseable lines come back as
    ``None`` so their bytes are still accounted for."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if offset > size:
        offset = 0
    out: list[tuple[dict[str, Any] | None, int]] = []
    pos = offset
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            for raw in fh:
                pos += len(raw)
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    row = None
                out.append((row if isinstance(row, dict) else None, pos))
    except OSError:
        return []
    return out


def events_from_rows(rows: Iterable[dict[str, Any]], *, thread_kind: str) -> list[_Event]:
    """Fold one transcript's rows into activity events.

    A run is a prompt row followed by the assistant row that answers it, and
    the event is emitted on the answer.

    * ``peer_inbound`` + ``assistant``/``via_peer`` → a peer exchange, on any
      thread (an async callback can land on a user thread).
    * ``user`` + ``assistant`` → a run, but only on machinery: a thread whose
      stem says so, or a row stamped ``channel: scheduler`` (a schedule pinned
      to an arbitrary ``thread_id`` carries no prefix). A human's message on
      a plain thread clears the pending run and is never logged.
    """
    events: list[_Event] = []
    pending_run: tuple[str, str] | None = None  # (kind, prompt)
    pending_peer: tuple[str, str] | None = None  # (sender, message)

    for row in rows:
        role = str(row.get("role") or "")
        content = str(row.get("content") or "")
        ts = str(row.get("ts") or "")
        if role == "peer_inbound":
            pending_peer = (str(row.get("from") or "a peer"), content)
            continue
        if role == "assistant" and row.get("via_peer"):
            sender = str(row.get("via_peer") or "")
            prompt = ""
            if pending_peer and (not sender or pending_peer[0] == sender):
                sender, prompt = pending_peer
            pending_peer = None
            if content.strip():
                label = f"Peer request from {sender or 'a peer'}"
                events.append(_Event(ts, _render(ts, label, prompt, content)))
            continue
        if role == "user":
            if row.get("hidden"):
                continue
            kind = thread_kind or ("scheduled" if row.get("channel") == "scheduler" else "")
            pending_run = (kind, content) if kind else None
            continue
        if role == "assistant":
            if pending_run is None:
                continue
            kind, prompt = pending_run
            pending_run = None
            reply = content.strip()
            if not reply:
                continue
            if kind == "heartbeat":
                if reply == _HEARTBEAT_OK:
                    continue  # a quiet check is not an event
                events.append(_Event(ts, _render(ts, "Heartbeat", _heartbeat_task(prompt), reply)))
            elif kind == "issues":
                events.append(_Event(ts, _render(ts, "Issue poll", "", reply)))
            else:
                events.append(_Event(ts, _render(ts, "Scheduled run", prompt, reply)))
    return events


def events_from_audit(actions: Iterable[dict[str, Any]], *, after_ts: str) -> list[_Event]:
    """Cortex phases from ``cognition/audit`` rows newer than ``after_ts``.

    Only outcomes count: a completed or empty run and a failure. A phase the
    gate skipped, or one disabled, did no work and is not activity.
    """
    events: list[_Event] = []
    for row in actions:
        action = str(row.get("action") or "")
        ts = str(row.get("ts") or "")
        if not action.startswith("cortex_") or not ts or ts <= after_ts:
            continue
        raw_detail = row.get("detail")
        detail: dict[str, Any] = raw_detail if isinstance(raw_detail, dict) else {}
        status = str(detail.get("status") or "")
        label = str(detail.get("label") or action[len("cortex_") :]).strip() or "phase"
        summary = str(row.get("summary") or "")
        if status in ("completed", "empty"):
            events.append(_Event(ts, _render(ts, f"Cortex {label}", "", summary)))
        elif status == "error":
            events.append(_Event(ts, _render(ts, f"Cortex {label} failed", "", summary)))
    return events


class ActivityLogger:
    """One agent's activity pass. Sync and I/O-bound — call via ``asyncio.to_thread``."""

    def __init__(
        self,
        agent_name: str,
        workspace_root: Path,
        markdown_memory: MarkdownMemory,
        *,
        state_dir: Path | None = None,
        now: datetime | None = None,
    ) -> None:
        self.agent = agent_name
        self.workspace_root = workspace_root
        self.memory = markdown_memory
        base = state_dir or (workspace_root / WORKSPACE_STATE_DIRNAME)
        self.state_path = base / "state" / "self_learning" / "activity_watermark.json"
        self._now = now or datetime.now(timezone.utc)

    # ------------------------------------------------------------------ sources

    def _session_events(self, state: _State) -> list[_Event]:
        sessions_dir = get_olano_paths().sessions_dir / self.agent
        offsets = state.sessions.setdefault(self.agent, {})
        if not sessions_dir.is_dir():
            offsets.clear()
            return []
        files = sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        present = {str(f) for f in files}
        for stale in [k for k in offsets if k not in present]:
            del offsets[stale]
        events: list[_Event] = []
        for path in files:
            key = str(path)
            start = offsets.get(key, 0)
            records = _read_records(path, start)
            if not records:
                continue
            rows = [r for r, _ in records if r is not None]
            events.extend(events_from_rows(rows, thread_kind=_thread_kind(path.stem)))
            offsets[key] = self._consumed_offset(records, fallback=start)
        return events

    def _consumed_offset(
        self, records: list[tuple[dict[str, Any] | None, int]], *, fallback: int
    ) -> int:
        """Byte offset to persist after reading ``records``.

        Normally the end of the last line. When the file ends on a prompt row
        that nothing has answered yet — a turn in flight — stop *before* it, so
        the prompt and its answer are read together next pass instead of the
        answer arriving orphaned and the run never being logged. Every scheduled
        run gets its own thread file, so a turn that died leaves exactly this
        shape forever; :data:`DANGLING_PROMPT_MAX_AGE` is what stops that from
        pinning the offset (and re-reading the row) on every pass until the end
        of time.
        """
        end = records[-1][1]
        last_row, _ = records[-1]
        if last_row is None or last_row.get("role") not in _PROMPT_ROLES:
            return end
        stamped = _parse_ts(str(last_row.get("ts") or ""))
        if stamped is None or self._now - stamped > DANGLING_PROMPT_MAX_AGE:
            return end
        # Only the final record is dangling — any earlier prompt row is dead by
        # construction (the runner writes prompt then answer, nothing between).
        return records[-2][1] if len(records) > 1 else fallback

    def _audit_events(self, state: _State) -> list[_Event]:
        try:
            from olano.cognition.audit import read_actions

            actions = read_actions(self.workspace_root, limit=500)
        except Exception:
            logger.debug("activity_log: audit unavailable", exc_info=True)
            return []
        events = events_from_audit(actions, after_ts=state.audit_ts)
        newest = max((str(a.get("ts") or "") for a in actions), default="")
        if newest > state.audit_ts:
            state.audit_ts = newest
        return events

    # ------------------------------------------------------------------ writer

    def run(self) -> int:
        """Append new activity to the daily logs. Returns the number of lines written."""
        state = _State.load(self.state_path)
        events = self._session_events(state) + self._audit_events(state)

        cutoff = (self._now - timedelta(days=MAX_EVENT_AGE_DAYS)).date().isoformat()
        by_day: dict[str, list[_Event]] = {}
        for ev in events:
            if len(ev.ts) < 10 or ev.day < cutoff:
                continue
            by_day.setdefault(ev.day, []).append(ev)

        written = 0
        for day in sorted(by_day):
            when = _parse_ts(f"{day}T00:00:00+00:00")
            if when is None:
                continue
            day_state = state.days.setdefault(day, {"lines": 0, "overflow": False})
            room = MAX_LINES_PER_DAY - int(day_state.get("lines", 0))
            lines = [ev.line for ev in sorted(by_day[day], key=lambda e: e.ts)]
            dropped = 0
            if room <= 0:
                dropped, lines = len(lines), []
            elif len(lines) > room:
                dropped, lines = len(lines) - room, lines[:room]
            if dropped and not day_state.get("overflow"):
                lines.append(f"- … {dropped} more event(s) not logged (daily activity cap)")
                day_state["overflow"] = True
            if not lines:
                continue
            self.memory.append_daily("Activity:\n" + "\n".join(lines), date=when)
            day_state["lines"] = int(day_state.get("lines", 0)) + len(lines)
            written += len(lines)

        for old in [d for d in state.days if d < cutoff]:
            del state.days[old]
        try:
            state.save(self.state_path)
        except OSError:
            logger.warning("activity_log: could not persist watermark %s", self.state_path)
        if written:
            logger.info("activity_log[%s]: %d line(s) appended", self.agent, written)
        return written


__all__ = [
    "MAX_EVENT_AGE_DAYS",
    "MAX_LINES_PER_DAY",
    "ActivityLogger",
    "events_from_audit",
    "events_from_rows",
]
