"""Append-only ledger of per-call LLM usage events.

Why a dedicated ledger? The existing ``CostTracker`` (``olano/pulse``) writes
``<workspace>/.olano/cost_ledger.json`` keyed *only by date* — agents that
share a workspace merge together and the model is never recorded, so it cannot
answer "tokens per agent", "$ per agent", or "$ per model". This ledger records
one self-describing event per call at the single chokepoint where usage is
already tallied (``core/runner._record_usage``), so the analytics layer can slice
it any way the dashboard needs.

The file is a JSONL at ``<home>/analytics/usage.jsonl`` (global, cross-agent —
mirrors how sessions and the A2A log live under the home, not the workspace).
Each line::

    {
        "ts": "2026-06-07T12:34:56.789+00:00",
        "agent": "merli",
        "model": "anthropic:claude-sonnet-4-6",
        "input_tokens": 1200,
        "output_tokens": 340,
        "usd": 0.0087,
        "credits": 1.52,  # Olano-managed calls only (see below)
    }

Two billing sources share this ledger, and the difference matters to every
consumer:

- **BYOK** (``anthropic:…``, ``openai:…``) — the customer owns the provider
  bill. ``usd`` is our *estimate* of what they were charged; ``credits`` is
  absent, because Olano sells them nothing here.
- **Olano-managed** (``olano:auto|turbo|pro|max``) — Olano owns the
  provider bill. ``credits`` is the authoritative charge, reported by the cloud
  gateway per call (this box is never told the rate). ``usd`` is a local
  estimate that has no billing meaning and must not be shown to the customer.

``UsageEvent.billing_source`` derives this from the model string rather than
storing it, so events written before the split classify correctly too.

``origin`` says *what kind of work* spent the tokens, so Insights can answer
"where did my credits go". Runner turns map the request context's root source
(``interactive`` / ``scheduled`` / ``cognition`` / ``peer``); background
writers use their own labels (``cognition``, ``delegate``, ``pulse``,
``highlights``, ``reports``, ``prompt_gen``, ``acp`` — see
:mod:`olano.analytics.usage_recorder`). Rows written before the field existed
read as ``""`` and render as plain agent turns.

Writes never raise — accounting must never break a turn.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)


@lru_cache(maxsize=256)
def _billing_source_for_model(model: str) -> str:
    """Memoized billing-source classification — a rollup parses many thousands
    of events sharing a handful of model strings, and the per-access
    import + string walk added up to real time on large ledgers."""
    from olano.models.olano_gateway import is_olano_model

    return "olano" if is_olano_model(model) else "customer"


def _line_ts_before(line: str, since: str) -> bool:
    """Cheap raw-line check: True when the line's ``ts`` is definitely < ``since``.

    Every writer serializes via :meth:`UsageEvent.to_json` + ``json.dumps``
    (default separators), so rows carry the literal needle ``"ts": "``. A line
    without it (hand-written, foreign) returns False so the caller falls
    through to the full parse — the prefilter is an optimization, never the
    authority.
    """
    i = line.find('"ts": "')
    if i < 0:
        return False
    start = i + 7
    end = line.find('"', start)
    if end < 0:
        return False
    return line[start:end] < since


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class UsageEvent:
    """One recorded LLM call."""

    ts: str
    agent: str
    model: str
    input_tokens: int
    output_tokens: int
    usd: float
    cache_read_tokens: int = 0
    # Olano credits charged by the gateway. None for BYOK calls — which is not
    # the same as 0.0: "Olano sold nothing here" versus "a free managed call".
    credits: float | None = None
    # Model sub-calls folded into this row. One event is written per *turn*,
    # and a tool-heavy turn is many model calls; rows written before this field
    # existed read as 1, which is what they were assumed to be anyway.
    calls: int = 1
    # What kind of work spent these tokens (see module docstring). "" = a row
    # written before the field existed.
    origin: str = ""
    # The conversation this turn ran on, so spend can be ranked per thread
    # without mining checkpoints. "" on background writers with no thread and
    # on rows written before the field existed — the drill-down treats an
    # unknown thread as one it has never seen rather than guessing.
    thread_id: str = ""
    # Synthesized-history marker (analytics/backfill.py): True on rows
    # reconstructed from checkpointed cognition threads. They render in every
    # local chart like live rows, but the cloud reporter and the budget-counter
    # seed MUST skip them — that history was already billed by the gateway when
    # it happened, and re-shipping or re-counting it would double it.
    backfill: bool = False
    # Provenance of a backfilled row (the source thread id). Doubles as the
    # dedup key so re-running the backfill can never write a thread twice.
    src: str = ""

    @property
    def billing_source(self) -> str:
        """``"olano"`` when Olano paid the provider, ``"customer"`` for BYOK."""
        return _billing_source_for_model(self.model)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def date(self) -> str:
        """Calendar date (UTC) of the event, ``YYYY-MM-DD``."""
        return self.ts[:10]

    def to_json(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "ts": self.ts,
            "agent": self.agent,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "usd": self.usd,
            "cache_read_tokens": self.cache_read_tokens,
            "calls": self.calls,
            **({"credits": self.credits} if self.credits is not None else {}),
            **({"origin": self.origin} if self.origin else {}),
            **({"thread_id": self.thread_id} if self.thread_id else {}),
            **({"backfill": True} if self.backfill else {}),
            **({"src": self.src} if self.src else {}),
        }

    @classmethod
    def from_json(cls, raw: dict) -> UsageEvent | None:
        try:
            return cls(
                ts=str(raw["ts"]),
                agent=str(raw.get("agent") or "unknown"),
                model=str(raw.get("model") or "unknown"),
                input_tokens=int(raw.get("input_tokens") or 0),
                output_tokens=int(raw.get("output_tokens") or 0),
                usd=float(raw.get("usd") or 0.0),
                cache_read_tokens=int(raw.get("cache_read_tokens") or 0),
                credits=(float(raw["credits"]) if raw.get("credits") is not None else None),
                calls=max(1, int(raw.get("calls") or 1)),
                origin=str(raw.get("origin") or ""),
                thread_id=str(raw.get("thread_id") or ""),
                backfill=bool(raw.get("backfill")),
                src=str(raw.get("src") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


def usage_ledger_path() -> Path:
    return get_olano_paths().usage_ledger_path


def record_usage_event(
    agent: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    usd: float,
    *,
    cache_read_tokens: int = 0,
    credits: float | None = None,
    calls: int = 1,
    ts: str | None = None,
    origin: str = "",
    thread_id: str = "",
    backfill: bool = False,
    src: str = "",
) -> None:
    """Append one usage event. Best-effort — never raises."""
    try:
        path = usage_ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        event = UsageEvent(
            ts=ts or datetime.now(timezone.utc).isoformat(),
            agent=agent or "unknown",
            model=model or "unknown",
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            usd=float(usd or 0.0),
            cache_read_tokens=int(cache_read_tokens or 0),
            credits=float(credits) if credits is not None else None,
            calls=max(1, int(calls or 1)),
            origin=str(origin or ""),
            thread_id=str(thread_id or ""),
            backfill=bool(backfill),
            src=str(src or ""),
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_json(), ensure_ascii=False) + "\n")
    except Exception:  # pragma: no cover - defensive
        logger.debug("usage_ledger: record failed", exc_info=True)


def iter_usage_events(
    *, since: str | None = None, path: Path | None = None
) -> Iterator[UsageEvent]:
    """Yield parsed events, optionally filtered to ``ts >= since`` (ISO/date string).

    ``since`` is compared lexicographically against the ISO timestamp, which is
    correct for ISO-8601 (and for plain ``YYYY-MM-DD`` date bounds).
    """
    p = path or usage_ledger_path()
    if not p.exists():
        return
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                # Skip out-of-window rows BEFORE parsing: a 30-day query on a
                # year-old ledger used to json-parse every historical row just
                # to drop it. The post-parse check below stays the authority.
                if since is not None and _line_ts_before(line, since):
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw, dict):
                    continue
                event = UsageEvent.from_json(raw)
                if event is None:
                    continue
                if since is not None and event.ts < since:
                    continue
                yield event
    except OSError:
        return


def read_usage_events(*, since: str | None = None, path: Path | None = None) -> list[UsageEvent]:
    """Eager list form of :func:`iter_usage_events`."""
    return list(iter_usage_events(since=since, path=path))


_TS_NEEDLE = b'"ts": "'

# Memo for :func:`ledger_start_day`: (path, bytes scanned, mtime, answer).
# Not a TTL — a stale seam silently moves which days the analytics layer treats
# as pre-ledger history, so this stays exact and gets its speed from the file
# being append-only instead (see the incremental tail read below).
_start_day_memo: tuple[str, int, int, str | None] = ("", -1, -1, None)


def _min_day_from(fh: BinaryIO, seed: str | None) -> str | None:
    """Smallest ``"ts"`` day over the lines from ``fh``'s current offset, vs ``seed``.

    A raw prefix scan — no JSON parse — since every writer serializes through
    :meth:`UsageEvent.to_json`. Reads bytes so the caller can seek to an exact
    offset (a text stream only accepts opaque ``tell()`` cookies), and the day
    prefix is ASCII in every row a writer produces.
    """
    earliest = seed
    for line in fh:
        i = line.find(_TS_NEEDLE)
        if i < 0:
            continue
        raw = line[i + len(_TS_NEEDLE) : i + len(_TS_NEEDLE) + 10]
        if len(raw) != 10:
            continue
        day = raw.decode("ascii", "ignore")
        if len(day) == 10 and (earliest is None or day < earliest):
            earliest = day
    return earliest


def ledger_start_day(path: Path | None = None) -> str | None:
    """Earliest UTC day (``YYYY-MM-DD``) the ledger holds an event for, or None.

    This is the seam between the ledger's own era and the history that predates
    it, so it is deliberately **window-independent**: a boundary that moved with
    the caller's window would let a 7-day and a 30-day view classify the same
    day differently, which is precisely how a shorter window ends up totalling
    more than a longer one.

    The first line is *not* the answer — ``analytics/backfill.py`` appends
    reconstructed history stamped at its original run day, so the minimum can
    arrive last. But the file is append-only, so an unchanged file reuses the
    memo and a grown one only re-reads the bytes appended since the last scan;
    a shrunk or replaced file falls back to a full pass.
    """
    global _start_day_memo
    p = path or usage_ledger_path()
    key = str(p)
    prev_key, prev_size, prev_mtime, prev_val = _start_day_memo
    try:
        st = p.stat()
    except OSError:
        return None
    if key == prev_key and st.st_size == prev_size and st.st_mtime_ns == prev_mtime:
        return prev_val
    resume = prev_size if (key == prev_key and st.st_size > prev_size >= 0) else 0
    try:
        with p.open("rb") as fh:
            if resume:
                fh.seek(resume)
            earliest = _min_day_from(fh, prev_val if resume else None)
    except OSError:
        # An unreadable ledger must not be memoized as "no history" — leave the
        # previous answer standing and retry on the next call.
        return prev_val if key == prev_key else None
    _start_day_memo = (key, st.st_size, st.st_mtime_ns, earliest)
    return earliest
