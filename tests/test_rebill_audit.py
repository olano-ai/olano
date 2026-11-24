"""Detection and correction of usage rows a turn re-billed from its whole thread.

Migration 0014 rewrites billing history with this logic, so the tests that
matter most are the ones asserting what it must NOT touch: a false positive
turns real spend into zeros, which reads as correct and is unrecoverable
without the backup.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from olano.analytics import rebill_audit, usage_ledger
from olano.analytics.usage_ledger import UsageEvent
from olano.config.home import get_olano_paths

_MODEL = "openai:gpt-5.6-terra"
TODAY = datetime.now(timezone.utc).date().isoformat()


def _ev(minute: int, calls: int, inp: int, usd: float, **kw) -> UsageEvent:
    return UsageEvent(
        ts=f"{TODAY}T18:{minute:02d}:00+00:00",
        agent=kw.pop("agent", "notion"),
        model=kw.pop("model", _MODEL),
        input_tokens=inp,
        output_tokens=inp // 20,
        usd=usd,
        calls=calls,
        origin=kw.pop("origin", "scheduled"),
        **kw,
    )


def _rebilled(turns: int = 20, per_turn_calls: int = 5) -> list[UsageEvent]:
    """A thread whose every turn re-reports the whole conversation.

    Turn N holds N turns' calls and the *cumulative* input across a growing
    context — so tokens-per-call climbs, which is the signature guard 4 keys on.
    """
    events, cum_in = [], 0
    for n in range(1, turns + 1):
        cum_in += 1_000 * n  # turn n's own call carries n turns of context
        events.append(_ev(n, per_turn_calls * n, cum_in, 0.30 * n))
    return events


# ── what it must catch ────────────────────────────────────────────────────────


def test_finds_a_rebilled_run_and_recovers_its_true_total() -> None:
    events = _rebilled(turns=20)
    runs = rebill_audit.find_rebilled_runs(events)

    assert len(runs) == 1
    run = runs[0]
    assert run.rows == 20
    assert run.recorded_calls == 5 * 20 * 21 // 2  # the triangular sum
    assert run.true_calls == 5 * 20  # the last row alone
    # The inflation factor the fix names, derived from the data.
    assert run.inflation == pytest.approx((20 + 1) / 2)


def test_correction_makes_the_run_sum_to_its_last_row() -> None:
    """Deltas, not deletions: same rows, same timestamps, true totals."""
    events = _rebilled(turns=10)
    lines = [json.dumps(e.to_json()) for e in events]

    corrected, summary = rebill_audit.correct_lines(lines)

    assert len(corrected) == len(lines)  # nothing dropped
    assert summary.corrected_rows == 9  # the first row was already right
    rows = [json.loads(line) for line in corrected]
    assert sum(r["calls"] for r in rows) == events[-1].calls
    assert sum(r["input_tokens"] for r in rows) == events[-1].input_tokens
    assert sum(r["usd"] for r in rows) == pytest.approx(events[-1].usd)
    # Timestamps and attribution survive; corrected rows say so.
    assert [r["ts"] for r in rows] == [e.ts for e in events]
    assert all(r["agent"] == "notion" and r["model"] == _MODEL for r in rows)
    assert [r.get("rebill_corrected") for r in rows] == [None] + [True] * 9


def test_correction_is_idempotent() -> None:
    lines = [json.dumps(e.to_json()) for e in _rebilled(turns=8)]
    once, first = rebill_audit.correct_lines(lines)
    twice, second = rebill_audit.correct_lines(once)
    assert twice == once
    assert second.runs == []
    assert first.runs != []


def test_unparseable_and_foreign_lines_pass_through_untouched() -> None:
    """A ledger is an append-only record; this must not silently drop a line."""
    lines = [json.dumps(e.to_json()) for e in _rebilled(turns=6)]
    lines.insert(3, "{not json")
    lines.append(json.dumps({"hello": "world"}))

    corrected, _ = rebill_audit.correct_lines(lines)
    assert corrected[3] == "{not json"
    assert json.loads(corrected[-1]) == {"hello": "world"}
    assert len(corrected) == len(lines)


# ── what it must NOT touch ────────────────────────────────────────────────────


def test_leaves_independent_turns_alone_even_when_they_grow() -> None:
    """Constant calls with a growing context is the healthy long-thread shape.

    A cumulative report can never repeat a value, so equal `calls` breaks the
    chain — this is the false positive that would zero out real spend.
    """
    events = [_ev(n, 4, 3_000 + 200 * n, 0.05) for n in range(1, 10)]
    assert rebill_audit.find_rebilled_runs(events) == []


def test_leaves_interactive_rows_alone() -> None:
    """Chat streams; the streaming path was never inflated."""
    events = [e for e in _rebilled(turns=20)]
    events = [
        UsageEvent(**{**e.__dict__, "origin": "interactive"})  # type: ignore[arg-type]
        for e in events
    ]
    assert rebill_audit.find_rebilled_runs(events) == []


def test_leaves_rows_that_carry_a_thread_id_alone() -> None:
    """`thread_id` shipped after the fix, so such a row cannot be inflated."""
    events = [
        UsageEvent(**{**e.__dict__, "thread_id": "acp::aurelius::notion"})  # type: ignore[arg-type]
        for e in _rebilled(turns=20)
    ]
    assert rebill_audit.find_rebilled_runs(events) == []


def test_a_short_rising_run_is_not_enough() -> None:
    """Three rising rows happen by chance; a staircase needs more."""
    events = [_ev(n, 3 * n, 5_000 * n * n, 0.1 * n) for n in range(1, 4)]
    assert rebill_audit.find_rebilled_runs(events) == []


def test_rising_rows_with_flat_context_per_call_are_not_a_staircase() -> None:
    """Guard 4: busier-but-independent turns scale calls and tokens together.

    Both rise, so guard 3 alone would flag them; tokens-per-call stays flat,
    which is what a cumulative report can never do.
    """
    events = [_ev(n, 2 * n, 6_000 * n, 0.1 * n) for n in range(1, 9)]
    assert rebill_audit.find_rebilled_runs(events) == []


# ── the migration ─────────────────────────────────────────────────────────────


def _mig():
    """The migration module — imported by name, since it starts with a digit."""
    import importlib

    return importlib.import_module("olano.migrations.0014_correct_rebilled_usage_rows")


def _run_migration() -> dict:
    mig = _mig()
    mig.apply(Path(get_olano_paths().root))
    audit_path = get_olano_paths().analytics_dir / mig.AUDIT_FILENAME
    return json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else {}


def test_migration_corrects_the_ledger_and_keeps_a_backup() -> None:
    for e in _rebilled(turns=12):
        usage_ledger.record_usage_event(
            e.agent,
            e.model,
            e.input_tokens,
            e.output_tokens,
            e.usd,
            calls=e.calls,
            origin=e.origin,
            ts=e.ts,
        )
    before = usage_ledger.read_usage_events()
    assert sum(x.calls for x in before) == 5 * 12 * 13 // 2

    report = _run_migration()
    assert report["runs"] == 1 and report["corrected_rows"] == 11

    after = usage_ledger.read_usage_events()
    assert len(after) == len(before)  # rows preserved, values corrected
    assert sum(x.calls for x in after) == 5 * 12  # the conversation, counted once

    ledger = usage_ledger.usage_ledger_path()
    backups = list(ledger.parent.glob(f"{ledger.name}.pre-0014-*"))
    assert len(backups) == 1, "the original must survive as the undo"
    assert backups[0].read_text(encoding="utf-8").count("\n") == len(before)


def test_migration_gives_the_budget_counters_back_what_was_never_spent() -> None:
    """The counters ENFORCE, and their weekly/monthly/lifetime windows keep
    summing the phantom day — so an agent sits throttled against a ceiling it
    never reached. They were written at the same chokepoint as the ledger row
    and inflated with it, so the correction owes them the same delta."""
    import sqlite3

    events = _rebilled(turns=12)
    for e in events:
        usage_ledger.record_usage_event(
            e.agent,
            e.model,
            e.input_tokens,
            e.output_tokens,
            e.usd,
            calls=e.calls,
            origin=e.origin,
            ts=e.ts,
        )
    # The counters as the buggy chokepoint left them: the same inflated totals.
    db = Path(get_olano_paths().root) / "deployment" / "usage_counters.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    recorded_in = sum(e.input_tokens for e in events)
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "CREATE TABLE daily_usage (agent TEXT NOT NULL, day TEXT NOT NULL,"
            " input_tokens INTEGER NOT NULL DEFAULT 0,"
            " output_tokens INTEGER NOT NULL DEFAULT 0,"
            " cost_micro_usd INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (agent, day))"
        )
        conn.execute(
            "INSERT INTO daily_usage VALUES (?,?,?,?,?)",
            ("notion", TODAY, recorded_in, sum(e.output_tokens for e in events), 0),
        )
        conn.commit()

    report = _run_migration()
    assert report["counters"]["adjusted_agent_days"] == 1

    with sqlite3.connect(str(db)) as conn:
        left = conn.execute(
            "SELECT input_tokens FROM daily_usage WHERE agent = 'notion'"
        ).fetchone()[0]
    # What the conversation really used — the last row, counted once.
    assert left == events[-1].input_tokens


def test_migration_leaves_an_unmanaged_box_without_a_counters_database() -> None:
    """An unmanaged install enforces nothing; never create the db for it."""
    for e in _rebilled(turns=8):
        usage_ledger.record_usage_event(
            e.agent,
            e.model,
            e.input_tokens,
            e.output_tokens,
            e.usd,
            calls=e.calls,
            origin=e.origin,
            ts=e.ts,
        )
    report = _run_migration()
    assert report["counters"]["skipped"] == "no counters database"
    assert not (Path(get_olano_paths().root) / "deployment" / "usage_counters.db").exists()


def test_migration_on_a_clean_ledger_writes_an_audit_and_no_backup() -> None:
    for n in range(1, 6):
        usage_ledger.record_usage_event("merli", _MODEL, 3_000, 150, 0.05, calls=4)
    report = _run_migration()
    assert report["runs"] == 0 and report["corrected_rows"] == 0
    ledger = usage_ledger.usage_ledger_path()
    assert list(ledger.parent.glob(f"{ledger.name}.pre-0014-*")) == []


def test_migration_gate_defers_until_a_ledger_exists() -> None:
    """Unrecorded rather than 'applied', so a box that records later still gets it."""
    mig = _mig()

    assert mig.applicable() is False
    usage_ledger.record_usage_event("merli", _MODEL, 1, 1, 0.0)
    assert mig.applicable() is True
