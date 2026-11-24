"""One-time reconstruction of historical cognition credits into the usage ledger.

Why this exists: the ledger only has rows from when each writer was
instrumented. Cognition ran for months on the raw graph (``cog::`` checkpoint
threads, no ledger rows), so the Insights charts showed a flat credits line
over exactly the period the gateway billed the most. The checkpoints kept
everything needed to reconstruct it: per-thread token totals in the threadcost
cache, and — because cognition workers call the gateway NON-streamed — often
the gateway's real per-call cost headers on the checkpointed messages.

One :class:`~olano.analytics.usage_ledger.UsageEvent` is written per ``cog::``
thread (threads are 1:1 with cognition runs), stamped noon-UTC on the thread's
run day, ``origin`` ``cognition``/``delegate``, and marked ``backfill=True`` +
``src=<thread_id>``:

- ``backfill`` keeps the row LOCAL-ONLY — the cloud usage reporter and the
  budget-counter seed skip it (that spend was billed when it happened;
  re-shipping or re-counting would double it).
- ``src`` is the dedup key: re-running can never write a thread twice, which
  makes the whole pass idempotent and crash-resumable.

Pricing, best first:

1. **Replay** — sum the gateway cost headers off the thread's checkpointed
   messages (root namespace; threads with subagent calls fall through, so a
   thread is never half-replayed half-estimated).
2. **Estimate** — token counts x the tier's published rates
   (``estimate_credits_micro``), the same estimator every live streamed turn
   uses. The tier is synthesized: the ``olano:*`` alias survives nowhere in
   checkpoints (the mined model names are the gateway's wire names), so the
   worker LABEL parsed from the thread id decides — the known
   parent-inheriting labels price at the parent agent's model, everything else
   at the cognition deep model (the engine is deep-only by design). BYOK
   parents are skipped: there are no credits to reconstruct, and their dollar
   history is already visible through the mined views.

Runs from a niced daemon thread at server boot (``maybe_spawn_backfill``,
state at ``<home>/analytics/backfill-state.json``) and from
``olano backfill-credits`` (``--dry-run`` / ``--force``). A cutoff at the
first LIVE cognition ledger row's day keeps the boundary day from double
counting where live recording took over.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STATE_FILENAME = "backfill-state.json"

# Worker labels whose runs inherit the PARENT agent's model (see
# cognition/worker._build_worker_config call sites: delegate_task passes
# `sub__<skill>`, and these three modules pass no `model=`). Every other label
# — the ideation family, orchestrator phases, self-learning distills, the
# skill librarian, workspace optimizer — runs on the cognition deep model.
_INHERIT_LABELS = {"skillsmith", "memory_garden", "pulse_agent"}

# How many threads to warm per cache pass while mining to completion.
_WARM_CHUNK = 500


def _state_path(home: str | Path | None = None) -> Path:
    from olano.config.home import get_olano_paths

    return get_olano_paths(home).usage_ledger_path.parent / STATE_FILENAME


def is_complete(home: str | Path | None = None) -> bool:
    try:
        raw = json.loads(_state_path(home).read_text(encoding="utf-8"))
        return bool(raw.get("status") == "done")
    except Exception:
        return False


def _write_state(home: str | Path | None, status: str, stats: dict[str, Any]) -> None:
    try:
        path = _state_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "status": status,
                    "stats": stats,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception:  # pragma: no cover - state is a convenience, not a ledger
        logger.debug("backfill: state write failed", exc_info=True)


def _worker_label(thread_id: str) -> str:
    """``cog::merli__cog__pulse_agent__ab12cd`` → ``pulse_agent``.

    The label may itself contain ``__`` (``sub__deploy``); only the trailing
    hex run-id is stripped. Legacy ids without the marker return ``""``.
    """
    tail = thread_id.split("cog::", 1)[-1]
    if "__cog__" not in tail:
        return ""
    rest = tail.split("__cog__", 1)[1]
    return rest.rsplit("__", 1)[0] if "__" in rest else rest


def _deep_model() -> str:
    try:
        from olano.config.system_settings import load_system_settings

        return load_system_settings().deep_model or "olano:pro"
    except Exception:
        return "olano:pro"


def _parent_model(agent: str) -> str | None:
    try:
        from olano.config.loader import load_agent_config

        return str(getattr(load_agent_config(agent), "model", "") or "") or None
    except Exception:
        return None


def _synth_model(agent: str, label: str) -> str | None:
    """The ``olano:*`` model to price this run at, or None to skip (BYOK)."""
    from olano.models.olano_gateway import is_olano_model

    if label.startswith("sub__") or label in _INHERIT_LABELS:
        model = _parent_model(agent) or _deep_model()
    else:
        model = _deep_model()
    return model if is_olano_model(model) else None


def _replay_thread_credits(tc: Any, thread_id: str) -> float | None:
    """Sum the gateway's real per-call charges off the checkpointed messages.

    Cognition invokes non-streamed, so ``x-litellm-response-cost`` /
    ``x-olano-credits-charged`` usually survived into each AIMessage's
    ``response_metadata.headers``. Root namespace only — callers skip replay
    for threads with subagent calls so a thread is never priced half-and-half.
    """
    try:
        from olano.analytics.threadcost import reader, service
        from olano.analytics.usage_recorder import merge_olano_credits

        # The replay reads the whole root namespace, so it takes the miner's
        # cap: a thread too large to mine is priced off its cached row instead
        # (the estimate branch), never loaded whole from a boot-time thread.
        too_large, _size = service.history_too_large(tc, thread_id)
        if too_large:
            return None
        messages, error = reader.read_namespace_messages_ex(tc.saver, thread_id, "")
        if error or not messages:
            return None
        scratch: dict[str, int] = {}
        for msg in messages:
            merge_olano_credits(scratch, msg, managed=True)
        micro = int(scratch.get("credits_micro", 0) or 0)
        return micro / 1_000_000 if micro > 0 else None
    except Exception:  # pragma: no cover - replay is an upgrade, never a blocker
        return None


def _mine_to_completion(
    cc: Any, tc: Any, index: dict[str, str], deadline: float | None, home: str | Path | None
) -> bool:
    """Drive warm_cache until nothing is stale. Returns False when the time
    budget expired or errors stopped making progress (partial mine — the pass
    still backfills whatever IS cached; the rest lands on a later run)."""
    from olano.analytics.threadcost import service

    while True:
        status = service.warm_cache(cc, tc, index, cap=_WARM_CHUNK, home=home)
        if status.get("remaining", 0) <= 0:
            return True
        if status.get("refreshed", 0) <= 0:
            # Only errors left — retrying the same set forever helps nobody.
            logger.info("backfill: mining stalled (%s)", status.get("last_error", ""))
            return False
        if deadline is not None and time.monotonic() > deadline:
            logger.info("backfill: mining time budget expired; resuming next run")
            return False


def backfill_cognition_credits(
    home: str | Path | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
    time_budget_sec: float | None = None,
    replay: bool = True,
) -> dict[str, Any]:
    """Reconstruct ledger rows for historical ``cog::`` threads. Idempotent.

    Returns a stats dict. Marks the state file ``done`` only after a COMPLETE
    pass (fully mined + every candidate handled) so a budget-bounded partial
    run resumes on the next boot; the per-thread ``src`` dedup makes overlap
    harmless either way. ``force`` ignores the live-row cutoff (never the
    dedup). ``dry_run`` writes nothing, including state.
    """
    from olano.analytics.threadcost import cache, reader, resolver
    from olano.analytics.usage_ledger import iter_usage_events, record_usage_event
    from olano.models.olano_gateway import estimate_credits_micro

    stats: dict[str, Any] = {
        "cog_threads": 0,
        "written": 0,
        "replayed": 0,
        "estimated": 0,
        "skipped_dedup": 0,
        "skipped_cutoff": 0,
        "skipped_blank_day": 0,
        "skipped_byok": 0,
        "skipped_empty": 0,
        "complete": False,
        "dry_run": dry_run,
    }
    if not reader.thread_store_available(home):
        # Nothing to reconstruct on a box with no checkpoint history.
        stats["complete"] = True
        if not dry_run:
            _write_state(home, "done", stats)
        return stats

    deadline = time.monotonic() + time_budget_sec if time_budget_sec else None

    # Existing coverage: per-thread dedup (always) + the day live cognition
    # recording began (skip >= that day so the boundary can't double count).
    existing_src: set[str] = set()
    cutoff: str | None = None
    for ev in iter_usage_events():
        if ev.src:
            existing_src.add(ev.src)
        if not ev.backfill and ev.origin in ("cognition", "delegate"):
            day = ev.date
            if cutoff is None or day < cutoff:
                cutoff = day
    if force:
        cutoff = None

    index = resolver.build_thread_agent_index(home)
    with closing(reader.open_thread_db(home)) as tc, closing(cache.connect(home)) as cc:
        mined_fully = _mine_to_completion(cc, tc, index, deadline, home)
        rows = [r for r in cache.all_rows(cc) if str(r.get("thread_id", "")).startswith("cog::")]
        stats["cog_threads"] = len(rows)
        handled_all = True
        for row in rows:
            if deadline is not None and time.monotonic() > deadline:
                handled_all = False
                break
            tid = str(row.get("thread_id") or "")
            day = str(row.get("day") or "")
            if len(day) != 10:
                # Blank day reads as "today" downstream — history must never
                # land on today, so these stay unwritten.
                stats["skipped_blank_day"] += 1
                continue
            if tid in existing_src:
                stats["skipped_dedup"] += 1
                continue
            if cutoff is not None and day >= cutoff:
                stats["skipped_cutoff"] += 1
                continue
            agent = str(row.get("agent") or "unknown")
            label = _worker_label(tid)
            model = _synth_model(agent, label)
            if model is None:
                stats["skipped_byok"] += 1
                continue
            input_tokens = int(row.get("input_tokens") or 0)
            output_tokens = int(row.get("output_tokens") or 0)
            cache_read = int(row.get("cache_read_tokens") or 0)
            calls = int(row.get("call_count") or 0)
            if calls <= 0 and input_tokens <= 0 and output_tokens <= 0:
                stats["skipped_empty"] += 1
                continue

            credits: float | None = None
            if replay and int(row.get("subagent_call_count") or 0) == 0:
                credits = _replay_thread_credits(tc, tid)
            if credits is not None:
                stats["replayed"] += 1
            else:
                micro = estimate_credits_micro(
                    model,
                    {
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        # estimate_credits_micro reads `cache_read`, not the
                        # cache_read_tokens spelling the cache row uses.
                        "cache_read": cache_read,
                    },
                )
                credits = micro / 1_000_000 if micro > 0 else None
                stats["estimated"] += 1

            if dry_run:
                stats["written"] += 1
                continue
            record_usage_event(
                agent=agent,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                # olano:* prices to $0 by design (cost.py); this book is credits.
                usd=0.0,
                cache_read_tokens=cache_read,
                credits=credits,
                calls=max(1, calls),
                ts=f"{day}T12:00:00+00:00",
                origin="delegate" if label.startswith("sub__") else "cognition",
                backfill=True,
                src=tid,
            )
            stats["written"] += 1

    stats["complete"] = bool(mined_fully and handled_all)
    if not dry_run:
        _write_state(home, "done" if stats["complete"] else "pending", stats)
    return stats


def maybe_spawn_backfill(home: str | Path | None = None) -> threading.Thread | None:
    """Server-boot hook: run the backfill on a niced daemon thread while the
    state file still says pending. No-op (returns None) once complete."""
    if is_complete(home):
        return None

    def _run() -> None:
        try:
            os.nice(10)
        except Exception:
            pass
        try:
            stats = backfill_cognition_credits(home)
            logger.info(
                "cognition credits backfill: wrote %d rows (%d replayed, %d estimated), "
                "%d threads seen, complete=%s",
                stats["written"],
                stats["replayed"],
                stats["estimated"],
                stats["cog_threads"],
                stats["complete"],
            )
        except Exception:  # pragma: no cover - background reconstruction never crashes boot
            logger.warning("cognition credits backfill failed", exc_info=True)

    thread = threading.Thread(target=_run, name="credits-backfill", daemon=True)
    thread.start()
    return thread
