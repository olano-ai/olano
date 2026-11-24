"""Per-agent boot profiling — where does agent startup actually go?

A runtime boot walks the agent roster serially, so one slow agent can dominate
the whole startup (observed: a single agent taking 173s of a 234s fleet boot).
The log lines around a boot only mark its *edges* — "Registry: registered",
"Notes watcher started", "Agent 'x' booted" — so a stall between two of them is
invisible: you can see that three minutes vanished, not into what.

This module records a nested timing tree per agent boot, so the answer is a log
line instead of a bisect.

Design notes:

* **Nothing is recorded unless a profile is active.** :func:`profile_boot` opens
  one; :func:`phase` outside of one is a no-op. That is deliberate — the same
  ``build_agent_*`` code paths are re-entered at runtime by ``/model`` switches,
  ``delegate_task`` subagents and cognition workers, and those must neither
  clobber the boot profile nor accrue cost of their own.
* **Collection is always on during a boot; only the report is gated.** The cost
  is a few dozen ``perf_counter()`` calls per agent, so paying it unconditionally
  buys the ability to ask "why was that boot slow?" *after* the fact, over
  ``GET /api/diagnostics/boot``. A flag you have to set in advance can only ever
  measure the *next* slow boot. Set ``LIONCLAW_BOOT_PROFILE=1`` to also log the
  full tree per agent.
* **Context propagates into worker threads, not into detached ones.** The build
  runs its blocking phases through ``asyncio.to_thread``, which copies the
  caller's context — so a phase entered inside the worker nests correctly under
  the phase that awaited it. A bare ``threading.Thread`` (the background RAG
  ingest) does *not* copy context, so its work is correctly left unattributed
  rather than billed to whichever agent happened to spawn it.

Because parents are timed on the event loop while their children run in a worker
thread, the gap between a parent and the sum of its children is itself a signal:
it is time the phase spent waiting for a thread slot or the GIL rather than
doing its own work. That gap is reported as ``(unattributed)``.
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Keep the most recent boots addressable for the diagnostics endpoint. Bounded so
# a long-lived runtime that restarts agents on a schedule can't grow this forever.
_MAX_PROFILES = 64

# A phase shorter than this is dropped from the rendered tree (and folded into its
# parent's remainder) — sub-10ms rows are noise in a boot measured in seconds.
_MIN_ROW_SECONDS = 0.01


@dataclass
class _Record:
    """One timed phase. ``seconds`` stays ``None`` until the phase exits."""

    label: str
    depth: int
    seq: int
    seconds: float | None = None


class BootProfile:
    """The timing tree for a single agent boot."""

    def __init__(self, agent: str) -> None:
        self.agent = agent
        self.started_at = time.time()
        self.total: float | None = None
        self._records: list[_Record] = []
        self._lock = threading.Lock()
        self._t0 = time.perf_counter()
        self._finished = False

    # ── recording ────────────────────────────────────────────────────────────

    def open(self, label: str, depth: int) -> _Record | None:
        """Allocate a record in *entry* order; ``None`` once the profile is done.

        Entry order (rather than completion order) is what makes the rendered
        tree read top-to-bottom the way the boot actually ran, and makes the
        record list a pre-order traversal the renderer can walk with a stack.
        """
        with self._lock:
            if self._finished:
                return None
            rec = _Record(label=label, depth=depth, seq=len(self._records))
            self._records.append(rec)
            return rec

    def finish(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            self.total = time.perf_counter() - self._t0

    # ── reporting ────────────────────────────────────────────────────────────

    def _direct_child_total(self, records: list[_Record], idx: int) -> tuple[float, bool]:
        """Sum the *direct* children of ``records[idx]`` in a pre-order list."""
        parent = records[idx]
        total = 0.0
        found = False
        for nxt in records[idx + 1 :]:
            if nxt.depth <= parent.depth:
                break
            if nxt.depth == parent.depth + 1:
                found = True
                total += nxt.seconds or 0.0
        return total, found

    def rows(self) -> list[dict[str, Any]]:
        """Flatten to render-ready rows, inserting ``(unattributed)`` remainders.

        A remainder is emitted for any phase whose direct children don't account
        for its own duration — that gap is where an uninstrumented call, or
        contention for a thread or the GIL, is hiding. It is emitted after the
        parent's whole subtree, so it reads as the last child. The boot total
        gets one too, so a stall in code nobody thought to wrap still shows up
        rather than silently vanishing between the top-level rows.
        """
        with self._lock:
            records = sorted(
                (r for r in self._records if r.seconds is not None), key=lambda r: r.seq
            )

        out: list[dict[str, Any]] = []
        # (parent_depth, row) awaiting the end of that parent's subtree.
        pending: list[tuple[int, dict[str, Any]]] = []
        for idx, rec in enumerate(records):
            while pending and pending[-1][0] >= rec.depth:
                out.append(pending.pop()[1])
            out.append({"label": rec.label, "depth": rec.depth, "seconds": rec.seconds or 0.0})
            child_total, has_children = self._direct_child_total(records, idx)
            remainder = (rec.seconds or 0.0) - child_total
            if has_children and remainder > _MIN_ROW_SECONDS:
                pending.append(
                    (
                        rec.depth,
                        {
                            "label": "(unattributed)",
                            "depth": rec.depth + 1,
                            "seconds": remainder,
                        },
                    )
                )
        while pending:
            out.append(pending.pop()[1])

        # Same remainder logic, one level up: whatever the boot spent outside any
        # instrumented top-level phase.
        if self.total is not None and out:
            root_remainder = self.total - sum(
                float(r["seconds"]) for r in out if int(r["depth"]) == 0
            )
            if root_remainder > _MIN_ROW_SECONDS:
                out.append({"label": "(unattributed)", "depth": 0, "seconds": root_remainder})
        return out

    def render(self) -> str:
        total = self.total or 0.0
        header = f"boot profile '{self.agent}' — total {total:.2f}s"
        if total <= 0:
            return header
        lines = [header]
        for row in self.rows():
            secs = float(row["seconds"])
            depth = int(row["depth"])
            if depth > 0 and secs < _MIN_ROW_SECONDS:
                continue
            pct = 100.0 * secs / total
            lines.append(f"  {secs:8.2f}s {pct:5.1f}%  {'  ' * depth}{row['label']}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "started_at": self.started_at,
            "total_seconds": round(self.total, 3) if self.total is not None else None,
            "phases": [
                {
                    "label": row["label"],
                    "depth": int(row["depth"]),
                    "seconds": round(float(row["seconds"]), 3),
                }
                for row in self.rows()
            ],
        }


# ── module state ─────────────────────────────────────────────────────────────

_CURRENT: contextvars.ContextVar[BootProfile | None] = contextvars.ContextVar(
    "lionclaw_boot_profile", default=None
)
_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar(
    "lionclaw_boot_profile_depth", default=0
)

_PROFILES: OrderedDict[str, BootProfile] = OrderedDict()
_PROFILES_LOCK = threading.Lock()


def report_enabled() -> bool:
    """True when ``LIONCLAW_BOOT_PROFILE`` asks for the per-agent tree in the log."""
    return (os.environ.get("LIONCLAW_BOOT_PROFILE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@contextmanager
def profile_boot(agent: str) -> Iterator[BootProfile]:
    """Profile one agent boot; log the tree on exit when reporting is enabled.

    Safe to nest (a boot triggered inside a boot): the inner call yields the
    outer profile rather than starting a competing one, so phases stay in a
    single tree.
    """
    existing = _CURRENT.get()
    if existing is not None:
        yield existing
        return

    profile = BootProfile(agent)
    tok_profile = _CURRENT.set(profile)
    tok_depth = _DEPTH.set(0)
    try:
        yield profile
    finally:
        _DEPTH.reset(tok_depth)
        _CURRENT.reset(tok_profile)
        profile.finish()
        with _PROFILES_LOCK:
            _PROFILES.pop(agent, None)
            _PROFILES[agent] = profile
            while len(_PROFILES) > _MAX_PROFILES:
                _PROFILES.popitem(last=False)
        if report_enabled():
            logger.info("%s", profile.render())


@contextmanager
def phase(label: str) -> Iterator[None]:
    """Time a named phase of the active boot; a no-op outside :func:`profile_boot`.

    The elapsed time is recorded even when the wrapped block raises — a phase
    that blew up after 90s is exactly the one worth seeing — and the exception
    itself is left to propagate untouched.
    """
    profile = _CURRENT.get()
    if profile is None:
        yield
        return
    depth = _DEPTH.get()
    rec = profile.open(label, depth)
    if rec is None:
        yield
        return
    tok = _DEPTH.set(depth + 1)
    started = time.perf_counter()
    try:
        yield
    finally:
        _DEPTH.reset(tok)
        rec.seconds = time.perf_counter() - started


def record(label: str, seconds: float) -> None:
    """Record an already-measured leaf phase; a no-op outside :func:`profile_boot`.

    For sections that can't be wrapped in a ``with`` without re-indenting a large
    block (a long ``for`` body, say). Only valid for phases with no nested
    children — it lands in the tree where it is called, which for a leaf is the
    right place and for a parent would not be.
    """
    profile = _CURRENT.get()
    if profile is None:
        return
    rec = profile.open(label, _DEPTH.get())
    if rec is not None:
        rec.seconds = seconds


def get_profile(agent: str) -> BootProfile | None:
    with _PROFILES_LOCK:
        return _PROFILES.get(agent)


def all_profiles() -> list[BootProfile]:
    """Recorded boots, oldest first."""
    with _PROFILES_LOCK:
        return list(_PROFILES.values())


def fleet_summary(profiles: list[BootProfile] | None = None, *, top: int = 3) -> str:
    """One line: total boot cost and the worst offenders.

    Logged unconditionally after the boot loop — a fleet whose startup is
    dominated by one agent should say so without a flag having been set in
    advance, because such a flag can only ever measure the *next* slow boot.
    """
    entries = [p for p in (all_profiles() if profiles is None else profiles) if p.total]
    if not entries:
        return ""
    total = sum(p.total or 0.0 for p in entries)
    worst = sorted(entries, key=lambda p: p.total or 0.0, reverse=True)[:top]
    detail = ", ".join(f"{p.agent} {p.total or 0.0:.1f}s" for p in worst)
    hint = "" if report_enabled() else " (set LIONCLAW_BOOT_PROFILE=1 for the per-phase tree)"
    return f"Agent boot: {total:.1f}s across {len(entries)} agents — slowest: {detail}{hint}"
