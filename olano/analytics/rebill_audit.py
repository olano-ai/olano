"""Find — and undo — the usage rows a turn re-billed from its whole thread.

Before PR #388, a checkpointed ``ainvoke`` returned the thread's accumulated
state and ``runner.ainvoke`` folded all of it into the turn's usage: turn N
billed turns 1..N, inflation ``(N+1)/2``. Only the **non-streaming** path was
affected — scheduler jobs, heartbeats, ``talk_to_agent`` peers,
``delegate_task``. The rows it wrote are still in ``usage.jsonl``, so Insights
keeps reporting spend that never happened.

Those rows are correctable because each one reports the thread's *whole* state:
a run of them is a **staircase**, and the true per-turn values are its first
differences. Replacing each row with its delta from the previous one leaves the
run summing to its last row — the truth — while keeping every row's timestamp,
agent, model and origin. That is exactly what #388 does going forward, applied
backwards.

**This rewrites billing history, so detection is deliberately strict.** A false
positive turns real spend into zeros, which is worse than an obvious
over-count: it looks correct. Every guard below is a property of the defect, not
a statistical hunch:

1. **No ``thread_id``.** That field arrived in PR #391, which is downstream of
   #388 — so a row carrying one was written by a build that already had the fix
   and cannot be inflated. This is the airtight upper boundary.
2. **A non-streaming origin.** Interactive chat streams and was never affected,
   so ``interactive`` is never touched. Neither is the legacy empty/``turns``
   bucket, which predates origin tagging and could be either — under-correcting
   is the safe direction.
3. **Strictly** increasing calls, input tokens and usd across the run. A
   cumulative report can never repeat or drop a value; independent turns
   routinely do, and a run of equal ``calls`` (the common healthy shape) breaks
   the chain immediately.
4. **Rising context per call.** In a re-billed run the reported input tokens
   grow *quadratically* while calls grow linearly, because each successive call
   carries more conversation — so tokens-per-call climbs across the run.
   Independent turns hold it roughly flat. This is what separates a real
   staircase from four healthy turns that happened to increase.
5. **At least ``_MIN_RUN`` rows**, so a short coincidence cannot qualify.

Rows are grouped by ``thread_id`` when present and by (agent, origin, model)
otherwise — every correctable row is in the second case by guard 1, so two
affected conversations interleaving break each other's chains and are simply
not corrected. Under-counting, never invention.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from olano.analytics.usage_ledger import UsageEvent

# Origins whose turns ran through the non-streaming path. `interactive` streams
# and was never inflated; "" / "turns" predates origin tagging and is skipped
# rather than guessed at.
REBILLED_ORIGINS = frozenset({"scheduled", "peer", "cognition", "delegate"})

# A staircase this short is a coincidence waiting to happen.
_MIN_RUN = 4

# How much the average context per call must grow across a run (guard 4). A
# re-billed run climbs steeply — turn N averages the whole conversation — so
# this is a loose floor, not a tuned threshold.
_MIN_CONTEXT_GROWTH = 1.25


@dataclass
class RebilledRun:
    """One run of rows that re-reported its conversation's whole state."""

    key: str
    first_ts: str
    last_ts: str
    rows: int
    recorded_calls: int
    recorded_usd: float
    true_calls: int  # the last row alone: the conversation, counted once
    true_usd: float
    agent: str = ""
    origin: str = ""
    models: tuple[str, ...] = ()

    @property
    def inflation(self) -> float:
        return (self.recorded_usd / self.true_usd) if self.true_usd > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "conversation": self.key,
            "agent": self.agent,
            "origin": self.origin,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "rows": self.rows,
            "recorded_calls": self.recorded_calls,
            "true_calls": self.true_calls,
            "recorded_usd": round(self.recorded_usd, 6),
            "true_usd": round(self.true_usd, 6),
            "inflation": round(self.inflation, 2),
            "models": list(self.models),
        }


@dataclass
class AuditSummary:
    """What a pass over the ledger found, and what correcting it would change."""

    scanned_rows: int = 0
    runs: list[RebilledRun] = field(default_factory=list)
    # Over-count per ``"<agent>|<day>"`` as ``[input_tokens, output_tokens,
    # credits]`` — what the durable budget counters were over-charged by, since
    # they were written at the same chokepoint as these rows and inflated with
    # them. Populated by :func:`correct_lines`; empty from :func:`audit`, which
    # analyses without deciding to change anything.
    counter_overcount: dict[str, list[float]] = field(default_factory=dict)

    @property
    def corrected_rows(self) -> int:
        # The first row of a run is already correct; the rest get deltas.
        return sum(r.rows - 1 for r in self.runs)

    @property
    def removed_calls(self) -> int:
        return sum(r.recorded_calls - r.true_calls for r in self.runs)

    @property
    def removed_usd(self) -> float:
        return sum(r.recorded_usd - r.true_usd for r in self.runs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scanned_rows": self.scanned_rows,
            "runs": len(self.runs),
            "corrected_rows": self.corrected_rows,
            "removed_calls": self.removed_calls,
            "removed_usd": round(self.removed_usd, 6),
            "counter_agent_days": len(self.counter_overcount),
            "removed_input_tokens": int(sum(v[0] for v in self.counter_overcount.values())),
            "worst": [r.to_dict() for r in self.runs[:20]],
        }


def _eligible(ev: UsageEvent) -> bool:
    """Guards 1 and 2: could this row possibly be a re-billed one?"""
    return not ev.thread_id and (ev.origin or "") in REBILLED_ORIGINS


def _is_staircase(run: list[UsageEvent]) -> bool:
    """Guards 3–5 over an already strictly-increasing run."""
    if len(run) < _MIN_RUN:
        return False
    first, last = run[0], run[-1]
    if first.calls <= 0 or last.calls <= 0 or first.input_tokens <= 0:
        return False
    # Guard 4: context per call climbs, because turn N averages in the whole
    # conversation. Independent turns hold this roughly flat.
    return (last.input_tokens / last.calls) >= _MIN_CONTEXT_GROWTH * (
        first.input_tokens / first.calls
    )


def find_rebilled_runs(events: list[UsageEvent]) -> list[RebilledRun]:
    """Every run of rows that re-reported its conversation's whole state.

    ``events`` need not be sorted; runs are found in timestamp order within each
    conversation. Ineligible rows (see the module docstring) are excluded before
    grouping, so they can neither join a run nor break one.
    """
    groups: dict[str, list[UsageEvent]] = {}
    for ev in events:
        if not _eligible(ev):
            continue
        key = ev.thread_id or f"{ev.agent}|{ev.origin}|{ev.model}"
        groups.setdefault(key, []).append(ev)

    found: list[RebilledRun] = []
    for key, rows in groups.items():
        rows.sort(key=lambda e: e.ts)
        run: list[UsageEvent] = []

        def _flush(run: list[UsageEvent], key: str = key) -> None:
            if not _is_staircase(run):
                return
            found.append(
                RebilledRun(
                    key=key,
                    first_ts=run[0].ts,
                    last_ts=run[-1].ts,
                    rows=len(run),
                    recorded_calls=sum(e.calls for e in run),
                    recorded_usd=sum(e.usd for e in run),
                    true_calls=run[-1].calls,
                    true_usd=run[-1].usd,
                    agent=run[0].agent,
                    origin=run[0].origin,
                    models=tuple(sorted({e.model for e in run})),
                )
            )

        for ev in rows:
            prev = run[-1] if run else None
            # Guard 3: a cumulative report never repeats or drops a value.
            if (
                prev is not None
                and ev.calls > prev.calls
                and ev.input_tokens > prev.input_tokens
                and ev.usd >= prev.usd
            ):
                run.append(ev)
                continue
            _flush(run)
            run = [ev]
        _flush(run)

    found.sort(key=lambda r: r.recorded_usd - r.true_usd, reverse=True)
    return found


def audit(events: list[UsageEvent]) -> AuditSummary:
    """Analyse only — no rewriting. Safe to run anywhere."""
    return AuditSummary(scanned_rows=len(events), runs=find_rebilled_runs(events))


def _delta(row: dict[str, Any], prev: UsageEvent) -> dict[str, Any]:
    """One row rewritten as its delta from the previous cumulative report."""
    out = dict(row)
    out["calls"] = max(1, int(row.get("calls") or 1) - prev.calls)
    for key, before in (
        ("input_tokens", prev.input_tokens),
        ("output_tokens", prev.output_tokens),
        ("cache_read_tokens", prev.cache_read_tokens),
    ):
        out[key] = max(0, int(row.get(key) or 0) - before)
    out["usd"] = max(0.0, round(float(row.get("usd") or 0.0) - prev.usd, 9))
    if row.get("credits") is not None and prev.credits is not None:
        out["credits"] = max(0.0, round(float(row["credits"]) - float(prev.credits), 9))
    # Provenance: this row's numbers were computed, not recorded. It also makes
    # the correction idempotent — a corrected row is no longer a staircase.
    out["rebill_corrected"] = True
    return out


def correct_lines(lines: list[str]) -> tuple[list[str], AuditSummary]:
    """Rewrite a ledger's lines with every re-billed run reduced to its deltas.

    Takes and returns raw JSONL lines (without trailing newlines) so unparseable
    or foreign lines pass through byte-for-byte — a ledger is an append-only
    record and this must not quietly drop anything it did not understand.
    """
    parsed: list[tuple[int, UsageEvent, dict[str, Any]]] = []
    for idx, line in enumerate(lines):
        try:
            raw = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(raw, dict) or raw.get("rebill_corrected"):
            continue
        ev = UsageEvent.from_json(raw)
        if ev is not None:
            parsed.append((idx, ev, raw))

    by_id = {id(ev): (idx, raw) for idx, ev, raw in parsed}
    summary = audit([ev for _, ev, _ in parsed])

    # Re-derive each run's member rows to rewrite them in place. find_* returns
    # summaries, so walk the same grouping once more against the parsed events.
    events = [ev for _, ev, _ in parsed]
    runs_by_key = {r.key: r for r in summary.runs}
    grouped: dict[str, list[UsageEvent]] = {}
    for ev in events:
        if not _eligible(ev):
            continue
        grouped.setdefault(ev.thread_id or f"{ev.agent}|{ev.origin}|{ev.model}", []).append(ev)

    out = list(lines)
    for key, run_summary in runs_by_key.items():
        rows = sorted(grouped.get(key, []), key=lambda e: e.ts)
        window = [e for e in rows if run_summary.first_ts <= e.ts <= run_summary.last_ts]
        for prev, ev in pairwise(window):
            idx, raw = by_id[id(ev)]
            fixed = _delta(raw, prev)
            out[idx] = json.dumps(fixed, ensure_ascii=False)
            # What this row over-charged the budget counters by. They are
            # written at the same chokepoint as the ledger row and were
            # inflated with it, so the correction owes them the same delta.
            bucket = summary.counter_overcount.setdefault(f"{ev.agent}|{ev.date}", [0, 0, 0.0])
            bucket[0] += ev.input_tokens - int(fixed.get("input_tokens") or 0)
            bucket[1] += ev.output_tokens - int(fixed.get("output_tokens") or 0)
            bucket[2] += float(ev.credits or 0.0) - float(fixed.get("credits") or 0.0)
    return out, summary
