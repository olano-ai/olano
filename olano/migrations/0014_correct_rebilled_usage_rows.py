"""Undo the usage rows a turn re-billed from its whole thread (pre-PR #388).

A checkpointed ``ainvoke`` returns the thread's accumulated state, and until
#388 ``runner.ainvoke`` folded all of it into the turn's usage — turn N billed
turns 1..N, inflation ``(N+1)/2``, on the non-streaming path only (scheduler
jobs, heartbeats, ``talk_to_agent`` peers, ``delegate_task``). The fix stopped
new rows from doing it; the rows already written stay wrong, so Insights keeps
reporting spend that never happened. On one deployment that was **36,340 model
calls and $2,622 inside a single hour** from one agent.

This runs once per home, on the boot after the snapshot carrying it lands:

* Every re-billed run is rewritten to its **first differences**, so the run
  sums to its last row — the truth — and every row keeps its timestamp, agent,
  model and origin. Corrected rows carry ``rebill_corrected: true``.
* The original file is copied to ``usage.jsonl.pre-0014-<stamp>`` first. If the
  correction is ever judged wrong, that copy is the whole undo.
* An audit lands at ``analytics/rebill-audit.json`` and is surfaced by
  ``GET /api/analytics/threadcost/diagnostics`` → ``rebill_audit``, so an
  operator with no shell can see exactly what changed.

Detection lives in :mod:`olano.analytics.rebill_audit` and is deliberately
strict — it will not touch a row that carries a ``thread_id`` (written by a
build that already had the fix), a row from the streaming ``interactive`` path,
or a run that does not show the cumulative signature. Under-correcting is the
safe direction: an over-count is visibly wrong, while spend erased from the
record looks correct.

Idempotent twice over: corrected rows are skipped on a re-read, and a corrected
run is no longer a staircase.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0014_correct_rebilled_usage_rows"

AUDIT_FILENAME = "rebill-audit.json"


def _ledger_path(home: Path) -> Path:
    return home / "analytics" / "usage.jsonl"


def applicable() -> bool:
    """Cheap gate: only run where there is a ledger to correct.

    A home whose ledger has not been created yet is not "done" — it is a box
    that has recorded nothing, and if it later replays history from an older
    build this should still get its chance. Left unrecorded, so it stays
    pending until the file exists.
    """
    try:
        from olano.config.home import get_olano_paths

        return get_olano_paths().usage_ledger_path.exists()
    except Exception:  # pragma: no cover - the gate is total; apply() decides
        return True


def apply(home: Path) -> None:
    from olano.analytics.rebill_audit import correct_lines

    path = _ledger_path(home)
    if not path.exists():
        return
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        logger.warning("0014: usage ledger unreadable, leaving it alone", exc_info=True)
        return

    lines = raw.splitlines()
    try:
        corrected, summary = correct_lines(lines)
    except Exception:  # pragma: no cover - never break a boot over accounting
        logger.warning("0014: re-billing scan failed, ledger untouched", exc_info=True)
        return

    report = summary.to_dict()
    report["migration"] = ID
    report["generated_at"] = datetime.now(timezone.utc).isoformat()

    if not summary.runs:
        logger.info("0014: no re-billed usage rows found (%d scanned)", summary.scanned_rows)
        _write_audit(path.parent, report)
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.pre-0014-{stamp}")
    try:
        shutil.copy2(path, backup)
        # A trailing newline keeps the file appendable exactly as it was.
        path.write_text("\n".join(corrected) + "\n", encoding="utf-8")
    except OSError:
        logger.warning("0014: could not rewrite the usage ledger", exc_info=True)
        return

    report["backup"] = backup.name
    report["counters"] = _repair_budget_counters(home, summary.counter_overcount)
    _write_audit(path.parent, report)
    logger.info(
        "0014: corrected %d re-billed rows across %d conversations "
        "(-%s model calls, -$%.2f never spent); original kept at %s",
        summary.corrected_rows,
        len(summary.runs),
        f"{summary.removed_calls:,}",
        summary.removed_usd,
        backup.name,
    )


def _repair_budget_counters(home: Path, overcount: dict[str, list[float]]) -> dict:
    """Give the durable budget counters back what the ledger never really spent.

    ``deployment/usage_counters.db`` is written at the same chokepoint as the
    ledger row, so it was inflated by exactly the same amount — and unlike the
    ledger it *enforces*. Its windows are what make this urgent: a ``daily``
    budget ages out on its own, but ``weekly``, ``monthly`` and ``lifetime``
    keep summing the phantom day, so an agent can sit throttled against a
    ceiling it never actually reached. #388 made that ceiling visible and
    raisable; it did not give back the tokens.

    Decrements are exactly the over-count this migration removed from the
    ledger, clamped at zero, and only on a box that already has the database —
    an unmanaged install enforces nothing and must not have one created for it.
    Never raises: a counter left high is a throttled agent, but a broken boot
    is every agent.
    """
    out: dict = {"adjusted_agent_days": 0, "input_tokens": 0, "output_tokens": 0}
    db = home / "deployment" / "usage_counters.db"
    if not overcount or not db.exists():
        out["skipped"] = "no counters database" if not db.exists() else "nothing to give back"
        return out
    try:
        import sqlite3

        from olano.catalog.credits_registry import credits_per_usd

        rate = credits_per_usd()
        with sqlite3.connect(str(db)) as conn:
            for key, (inp, outp, credits) in overcount.items():
                agent, _, day = key.partition("|")
                if not agent or len(day) != 10:
                    continue
                # Credits are the only thing the cost ceiling counts (a BYOK
                # row's dollars are the customer's, never Olano's bill), so a
                # BYOK-only over-count gives back tokens and no cost.
                cost_micro = round(float(credits) * 1_000_000 / rate) if credits and rate else 0
                cur = conn.execute(
                    "UPDATE daily_usage SET"
                    " input_tokens = MAX(0, input_tokens - ?),"
                    " output_tokens = MAX(0, output_tokens - ?),"
                    " cost_micro_usd = MAX(0, cost_micro_usd - ?)"
                    " WHERE agent = ? AND day = ?",
                    (int(inp), int(outp), int(cost_micro), agent, day),
                )
                if cur.rowcount:
                    out["adjusted_agent_days"] += 1
                    out["input_tokens"] += int(inp)
                    out["output_tokens"] += int(outp)
            conn.commit()
    except Exception as exc:  # pragma: no cover - a boot must never fail here
        logger.warning("0014: budget counters not adjusted", exc_info=True)
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    if out["adjusted_agent_days"]:
        logger.info(
            "0014: returned %s input tokens to the budget counters across %d agent-days",
            f"{out['input_tokens']:,}",
            out["adjusted_agent_days"],
        )
    return out


def _write_audit(analytics_dir: Path, report: dict) -> None:
    try:
        analytics_dir.mkdir(parents=True, exist_ok=True)
        (analytics_dir / AUDIT_FILENAME).write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:  # pragma: no cover - the correction already happened
        logger.debug("0014: audit report not written", exc_info=True)
