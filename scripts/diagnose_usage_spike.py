#!/usr/bin/env python3
"""Explain a spike in the usage ledger — what ran, and how much of it is real.

Insights reads ``<home>/analytics/usage.jsonl``, one row per turn. Two very
different things make that file show a large number, and this script separates
them:

**Real volume** — a lot of turns actually ran. The by-origin and by-hour tables
below show which kind of work, on which models, at what time.

**Re-billed volume** — a turn recorded more than it spent. Before the fix in
PR #388, ``runner.ainvoke`` folded the *whole accumulated thread state* into
each turn's usage, so on a thread that accumulates (an ``acp::`` agent-to-agent
pair thread, or a scheduled task pinned to a fixed ``thread_id``) turn N billed
turns 1..N — inflation ``(N+1)/2``, into the ledger, Insights, thread credits
and the durable budget counters alike. Only the non-streaming path was
affected: scheduler jobs, heartbeats, ``talk_to_agent`` peers, ``delegate_task``.
Interactive chat streams, so it was never inflated.

That leaves a signature this script looks for. Because every affected row
reports the thread's whole state, consecutive rows on one thread form a
**staircase** — 5, 10, 15, 20 calls rather than 5, 5, 5, 5 — and the *true*
cumulative for that run is the **last** row, not the sum. So a staircase is
both the diagnosis and the correction: report it, and report what the run
would have been without it.

Detection is :mod:`olano.analytics.rebill_audit` — the same strict rule
migration 0014 uses to correct the ledger, so what this reports and what a
deploy would change can never drift apart. Rows are only ever read here;
nothing in this script rewrites anything.

Usage
-----
    uv run python scripts/diagnose_usage_spike.py --day 2026-08-27
    uv run python scripts/diagnose_usage_spike.py --day 2026-08-27 --agent notion
    uv run python scripts/diagnose_usage_spike.py --since 2026-08-27T17 --until 2026-08-27T21
    uv run python scripts/diagnose_usage_spike.py --day 2026-08-27 --agent notion --rows 40
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from olano.analytics.rebill_audit import audit
from olano.analytics.usage_ledger import UsageEvent, iter_usage_events


def _fmt_usd(v: float) -> str:
    return f"${v:,.2f}" if v >= 0.01 or v == 0 else f"${v:.6f}"


def _fmt_int(v: float) -> str:
    return f"{int(v):,}"


@dataclass
class Bucket:
    calls: int = 0
    input_tokens: int = 0
    usd: float = 0.0
    credits: float = 0.0
    rows: int = 0

    def add(self, ev: UsageEvent) -> None:
        self.calls += ev.calls
        self.input_tokens += ev.input_tokens
        self.usd += ev.usd
        self.credits += float(ev.credits or 0.0)
        self.rows += 1


def _collect(
    events: list[UsageEvent],
) -> tuple[dict[str, Bucket], dict[str, Bucket], dict[str, Bucket], dict[str, Bucket]]:
    """Fold the window four ways: by origin, agent, model and UTC hour."""
    by_origin: dict[str, Bucket] = defaultdict(Bucket)
    by_agent: dict[str, Bucket] = defaultdict(Bucket)
    by_model: dict[str, Bucket] = defaultdict(Bucket)
    by_hour: dict[str, Bucket] = defaultdict(Bucket)
    for ev in events:
        by_origin[ev.origin or "(unset)"].add(ev)
        by_agent[ev.agent].add(ev)
        by_model[ev.model].add(ev)
        by_hour[ev.ts[:13]].add(ev)
    return by_origin, by_agent, by_model, by_hour


def _table(title: str, buckets: dict[str, Bucket], limit: int = 12) -> None:
    rows = sorted(buckets.items(), key=lambda kv: kv[1].usd, reverse=True)[:limit]
    if not rows:
        return
    print(f"\n{title}")
    print(f"  {'':<34}{'rows':>8}{'calls':>12}{'usd':>14}{'credits':>12}")
    for name, b in rows:
        credits = f"{b.credits:,.2f}" if b.credits else "—"
        print(f"  {name[:33]:<34}{b.rows:>8,}{b.calls:>12,}{_fmt_usd(b.usd):>14}{credits:>12}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--day", help="UTC day to inspect, YYYY-MM-DD")
    ap.add_argument("--since", help="ISO lower bound (e.g. 2026-08-27T17)")
    ap.add_argument("--until", help="ISO upper bound, exclusive")
    ap.add_argument("--agent", help="Restrict to one agent")
    ap.add_argument("--rows", type=int, default=25, help="Per-row detail lines to print (0 = none)")
    args = ap.parse_args(argv)

    since = args.since or (args.day if args.day else None)
    until = args.until or (f"{args.day}T99" if args.day else None)

    events = [
        ev
        for ev in iter_usage_events(since=since)
        if (until is None or ev.ts < until) and (not args.agent or ev.agent == args.agent)
    ]
    if not events:
        print("No ledger rows in that window.")
        return 0
    events.sort(key=lambda e: e.ts)

    total = Bucket()
    for ev in events:
        total.add(ev)
    scope = f"agent={args.agent}" if args.agent else "all agents"
    print(f"Usage ledger · {events[0].ts[:16]} → {events[-1].ts[:16]} UTC · {scope}")
    print(
        f"  {total.rows:,} rows · {total.calls:,} model calls · {_fmt_usd(total.usd)}"
        f" · {total.credits:,.2f} credits"
    )

    by_origin, by_agent, by_model, by_hour = _collect(events)
    _table("By origin (what kind of work)", by_origin)
    if not args.agent:
        _table("By agent", by_agent)
    _table("By model", by_model)
    _table("By hour (UTC)", by_hour, limit=24)

    report = audit(events)
    stairs = report.runs
    if not stairs:
        print("\nNo re-billing staircases found — these rows look like real volume.")
    else:
        inflated_calls = report.removed_calls
        inflated_usd = report.removed_usd
        print(
            f"\nRe-billed history (PR #388): {len(stairs)} run(s), "
            f"{_fmt_int(inflated_calls)} calls and {_fmt_usd(inflated_usd)} recorded but never spent."
        )
        print("  Each affected row reports the thread's WHOLE state, so the run's true")
        print("  cumulative is its last row — not the sum of its rows.")
        print(f"\n  {'conversation':<40}{'rows':>6}{'recorded':>12}{'true':>12}{'inflation':>11}")
        for s in stairs[:15]:
            factor = (s.recorded_usd / s.true_usd) if s.true_usd > 0 else float("inf")
            factor_s = f"{factor:.1f}x" if factor != float("inf") else "—"
            print(
                f"  {s.key[:39]:<40}{s.rows:>6}"
                f"{_fmt_usd(s.recorded_usd):>12}{_fmt_usd(s.true_usd):>12}{factor_s:>11}"
            )
        real_usd = total.usd - inflated_usd
        real_calls = total.calls - inflated_calls
        print(
            f"\n  Window after correction: ~{real_calls:,} model calls · ~{_fmt_usd(real_usd)}"
            f"  (recorded: {total.calls:,} · {_fmt_usd(total.usd)})"
        )
        print("  Rows without a thread_id are grouped by (agent, origin, model), so two")
        print("  affected conversations interleaving are under-counted here, never invented.")

    if args.rows:
        print(f"\nRows, oldest first (first {args.rows}):")
        print(f"  {'ts':<20}{'origin':<12}{'calls':>8}{'in_tokens':>12}{'usd':>12}  model")
        for ev in events[: args.rows]:
            print(
                f"  {ev.ts[:19]:<20}{(ev.origin or '-')[:11]:<12}{ev.calls:>8,}"
                f"{ev.input_tokens:>12,}{_fmt_usd(ev.usd):>12}  {ev.model}"
            )
        if len(events) > args.rows:
            print(f"  … {len(events) - args.rows:,} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
