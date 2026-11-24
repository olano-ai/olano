"""Orchestration tying the checkpoint reader, attribution, resolver, and cache into
the payloads the dashboard drill-down API serves.

Read strategy (hybrid + cached):
- agent/thread *ranking* reads only the small ``threadcost.db`` cache;
- a bounded warmer refreshes stale threads (changed checkpoint version) per request;
- per-thread *drill-down* (calls, tree, raw, tools) mines the one thread live.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from olano.analytics.threadcost import attribution, cache, reader, resolver
from olano.analytics.threadcost.cost import cost_for_call
from olano.config.home import get_olano_paths
from olano.core.checkpoints import unscope_thread

logger = logging.getLogger(__name__)

# Bound how much checkpoint mining a single request will trigger.
# Stale threads refreshed per warmer pass. The pass runs inline in the request
# that asked, and each thread it reads is deserialized whole (up to the
# history cap) and freed again, so the cap is what bounds one page load's
# peak and its wall time; the next request continues where this one stopped.
# 150 was most of a minute and over a gigabyte of churn on a box with hundreds
# of never-indexed threads.
_WARM_CAP = 40

# The miner reads a thread by loading its whole checkpoint state, the same
# replay a turn does, and it used to do that inline on every Overview and
# Insights request for every thread whose version moved. A thread that has not
# re-snapshotted since the whole-list middleware writes replays gigabytes; one
# such read was a 12 GB climb and an OOM kill, four times in a day on one box,
# with no chat turn anywhere near it. Three bounds now hold:
#
# - a thread whose pending history is over ``_MAX_HISTORY_BYTES`` (raw stored
#   bytes; decoded objects are several times that) is not read, and is not
#   retried until its version moves (its next turn snapshots, the sweep then
#   drops the old writes, and it comes back under the cap by itself);
# - one warm pass at a time per process (``_WARM_LOCK``): a second concurrent
#   request serves the cache as it is instead of mining alongside the first;
# - staleness is the newest checkpoint id only (``reader.version_key``), so the
#   sweep's deletions never mark a thread stale.
_MAX_HISTORY_ENV = "LIONCLAW_THREADCOST_MAX_HISTORY_MB"
_DEFAULT_MAX_HISTORY_MB = 64


def _max_history_bytes() -> int:
    raw = (os.environ.get(_MAX_HISTORY_ENV) or "").strip()
    try:
        mb = int(raw) if raw else _DEFAULT_MAX_HISTORY_MB
    except ValueError:
        mb = _DEFAULT_MAX_HISTORY_MB
    return max(1, mb) * 1024 * 1024


# thread_id -> version it was too large at; cleared when the version moves.
_TOO_LARGE: dict[str, str] = {}
_WARM_LOCK = threading.Lock()


def _reset_mining_bounds_for_tests() -> None:
    _TOO_LARGE.clear()


def history_too_large(
    threads_conn, thread_id: str, *, version: str | None = None
) -> tuple[bool, int | None]:
    """``(too_large, bytes)`` for one thread, remembered per version.

    ``bytes`` is None when the size is unknown (sqlite, or the query failed),
    which never counts as too large: an unknown size mines as it always did,
    and only a measured size over the cap refuses the read.
    """
    key = reader.version_key(version)
    if key and _TOO_LARGE.get(thread_id) == key:
        return True, None
    size = reader.pending_history_bytes(threads_conn, thread_id)
    if size is None or size <= _max_history_bytes():
        if key:
            _TOO_LARGE.pop(thread_id, None)
        return False, size
    if key:
        _TOO_LARGE[thread_id] = key
    logger.info(
        "threadcost: not mining %s, pending history is %d MB (cap %d MB, %s)",
        thread_id,
        size // (1024 * 1024),
        _max_history_bytes() // (1024 * 1024),
        _MAX_HISTORY_ENV,
    )
    return True, size


def _release_heap() -> float | None:
    """Hand freed arenas back to the OS after a pass; the MB reclaimed, or None."""
    try:
        from olano.admin.memory import release_free_heap

        return release_free_heap()
    except Exception:  # pragma: no cover - housekeeping, never a failure
        return None


def _skipped_payload(size: int | None) -> dict[str, Any]:
    """The ``skipped`` block a refused read carries, one shape for every reader."""
    return {"reason": "history_too_large", "bytes": size, "cap_bytes": _max_history_bytes()}


_AGENT_TOOLS_THREAD_CAP = 50  # threads mined for an agent-wide tool rollup
_RAW_MAX_FIELD = 8000  # chars per message field in the raw inspector
_RAW_MAX_MESSAGES = 60  # most-recent context messages returned by the raw view


# ─────────────────────────────── transcript bits ─────────────────────────────


def _first_entry(agent: str, thread_id: str, home: str | Path | None = None) -> dict | None:
    path = get_olano_paths(home).sessions_dir / agent / f"{thread_id}.jsonl"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    parsed = json.loads(line)
                    return parsed if isinstance(parsed, dict) else None
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _thread_label(agent: str, thread_id: str, home: str | Path | None = None) -> tuple[str, str]:
    """Return ``(name, preview)`` for a thread (best effort)."""
    base = get_olano_paths(home).sessions_dir / agent
    name = ""
    meta = base / f"{thread_id}.meta.json"
    if meta.exists():
        try:
            name = str(json.loads(meta.read_text(encoding="utf-8")).get("name") or "")
        except (OSError, json.JSONDecodeError):
            name = ""
    preview = ""
    entry = _first_entry(agent, thread_id, home)
    if entry:
        preview = str(entry.get("content") or "")[:120]
    return name, preview


# ─────────────────────────────── cache refresh ───────────────────────────────


def _mining_candidates(agent_index: dict[str, str], versions: dict[str, str]) -> list[str]:
    """Thread ids eligible for mining: session-file threads with checkpoint
    history, plus every ``cog::`` cognition thread.

    The JSONL anchor deliberately excludes orphan checkpoints — deleting a
    thread removes its JSONL but leaves the checkpoint behind. Cognition
    workers never write a JSONL in the first place (they run on the raw graph,
    one fresh ``cog::`` thread per run), so that rationale doesn't apply to
    them: their prefix is the anchor and the owner parses out of the id
    (``resolver.resolve_agent``). Without this arm, every cortex phase,
    self-learning cycle and ``delegate_task`` run was absent from the mined
    calls/token numbers. JSONL threads come first so a cog backlog can't
    starve interactive threads of the per-pass warm cap.

    Shared by :func:`warm_cache` and :func:`diagnostics` so the health check
    can never disagree with the warmer about what is mineable.
    """
    candidates = [tid for tid in agent_index if tid in versions]
    candidates += [tid for tid in versions if tid.startswith("cog::") and tid not in agent_index]
    return candidates


def refresh_thread(
    cache_conn,
    threads_conn,
    thread_id: str,
    agent_index: dict[str, str],
    *,
    version: str | None = None,
    home: str | Path | None = None,
) -> dict:
    """Mine + cache one thread's aggregate (skips work if version is unchanged)."""
    if version is None:
        version = reader.thread_version(threads_conn, thread_id)
    too_large, size = history_too_large(threads_conn, thread_id, version=version)
    if too_large:
        # Nothing cached, so the row keeps whatever an earlier pass mined; the
        # thread is retried once its version moves (see history_too_large).
        return {
            "thread_id": thread_id,
            "skipped": True,
            "too_large": True,
            "error": f"pending history over the mining cap ({_MAX_HISTORY_ENV})"
            + (f": {size} bytes" if size is not None else ""),
        }
    callset = reader.read_thread_callset(threads_conn, thread_id)
    calls = callset.all_calls()
    if callset.read_errors and not calls:
        # Every namespace failed to read back. Caching a $0 row under the current
        # version would freeze the thread at zero forever (version matches ⇒ never
        # re-mined), silently blanking Insights. Leave the cache stale so the next
        # pass retries, and hand the error to the caller for surfacing.
        return {
            "thread_id": thread_id,
            "skipped": True,
            "error": callset.last_error or "checkpoint state failed to load",
        }
    agent = resolver.resolve_agent(thread_id, agent_index)
    # thread_id is the agent-scoped checkpoint key; the JSONL transcript is stored
    # under the human-facing stem, so unscope before touching the filesystem.
    stem = unscope_thread(agent, thread_id)
    origin = resolver.classify_origin(thread_id, _first_entry(agent, stem, home))

    # Per-model breakdown (real per-call model) for the spend-by-model view.
    models: dict[str, dict[str, float]] = {}
    for c in calls:
        m = models.setdefault(
            c.model,
            {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "usd": 0.0, "calls": 0},
        )
        m["input_tokens"] += c.input_tokens
        m["output_tokens"] += c.output_tokens
        m["cache_read_tokens"] += c.cache_read_tokens
        m["usd"] += c.usd
        m["calls"] += 1

    row = cache.CachedThread(
        thread_id=thread_id,
        agent=agent,
        origin=origin,
        day=(callset.ts or "")[:10],
        last_ts=callset.ts or "",
        input_tokens=sum(c.input_tokens for c in calls),
        output_tokens=sum(c.output_tokens for c in calls),
        cache_read_tokens=sum(c.cache_read_tokens for c in calls),
        total_tokens=sum(c.total_tokens for c in calls),
        usd=sum(c.usd for c in calls),
        call_count=len(calls),
        subagent_call_count=sum(len(v) for v in callset.subagent_calls.values()),
        namespaces=(1 if callset.parent_calls else 0) + len(callset.subagent_calls),
        pricing_complete=1 if all(c.pricing_known for c in calls) else 0,
        model_json=json.dumps(models),
        version=version,
    )
    cache.upsert(cache_conn, row)
    return row.as_dict()


def warm_cache(
    cache_conn,
    threads_conn,
    agent_index: dict[str, str],
    *,
    cap: int = _WARM_CAP,
    priority_agent: str | None = None,
    home: str | Path | None = None,
) -> dict:
    """Refresh stale threads (changed version), priority first, up to ``cap``.

    Candidate selection lives in :func:`_mining_candidates`: session-file
    threads with live checkpoints (orphan checkpoints from deleted threads stay
    excluded — a large share of ``threads.db``) plus ``cog::`` cognition
    threads, which never have a JSONL.
    """
    versions = reader.all_thread_versions(threads_conn)
    cached = cache.cached_versions(cache_conn)
    # Candidates = session-file threads with checkpoint history + cog:: threads.
    candidates = _mining_candidates(agent_index, versions)
    cache.prune(cache_conn, set(candidates))
    # Stale = the thread's newest checkpoint moved. Not the row count: the
    # checkpoint sweep changes that for every thread it trims, and a sweep must
    # not turn into a fleet-wide re-mine on the next request.
    stale = [
        tid
        for tid in candidates
        if reader.version_key(cached.get(tid)) != reader.version_key(versions[tid])
    ]
    if priority_agent:
        owned = {tid for tid, a in agent_index.items() if a == priority_agent}
        stale.sort(key=lambda tid: 0 if tid in owned else 1)

    refreshed = 0
    errors = 0
    too_large = 0
    last_error = ""
    # One miner at a time: a second request arriving mid-pass (the Overview
    # teaser and the Insights page open together, a phone and a laptop) serves
    # what the cache holds rather than reading the same threads twice at once.
    if not _WARM_LOCK.acquire(blocking=False):
        return {
            "total": len(candidates),
            "stale": len(stale),
            "refreshed": 0,
            "remaining": len(stale),
            "errors": 0,
            "too_large": 0,
            "last_error": "",
            "busy": True,
        }
    try:
        for tid in stale:
            if refreshed >= cap:
                break
            try:
                row = refresh_thread(
                    cache_conn, threads_conn, tid, agent_index, version=versions[tid], home=home
                )
                if row.get("skipped"):
                    if row.get("too_large"):
                        too_large += 1
                    else:
                        errors += 1
                        last_error = str(row.get("error") or "")
                    continue
                refreshed += 1
            except Exception as exc:  # pragma: no cover - one bad thread can't break the sweep
                errors += 1
                last_error = f"{type(exc).__name__}: {exc}"
                logger.debug("threadcost: refresh failed for %s", tid, exc_info=True)
    finally:
        _WARM_LOCK.release()
    remaining = max(0, len(stale) - refreshed - errors - too_large)
    if refreshed:
        # Every thread read above was deserialized whole and freed again, and
        # glibc keeps those freed arenas mapped: RSS sits at the pass's peak
        # until something hands the pages back. A box's "slow leak" of a
        # gigabyte per Insights load was exactly this.
        released = _release_heap()
        logger.info(
            "threadcost: warm pass refreshed %d thread(s), %d too large, %d pending%s",
            refreshed,
            too_large,
            remaining,
            f", released {released:.0f} MB of heap" if released else "",
        )
    elif remaining:
        logger.info(
            "threadcost: %d threads still pending index (refreshed %d)", remaining, refreshed
        )
    if errors:
        # The per-thread warn-once lives in reader._note_read_failure; this summary
        # repeats every warm pass (every Insights request), so keep it at info.
        logger.info(
            "threadcost: %d thread(s) failed to mine this pass (last error: %s)",
            errors,
            last_error,
        )
    return {
        "total": len(candidates),
        "stale": len(stale),
        "too_large": too_large,
        "refreshed": refreshed,
        "remaining": remaining,
        "errors": errors,
        "last_error": last_error,
    }


# ─────────────────────────────── API payloads ────────────────────────────────


def _has_thread_db(home: str | Path | None) -> bool:
    return reader.thread_store_available(home)


def _since_bound(days: int | None, hours: int | None = None) -> str | None:
    """Lower bound on a thread's last-active time; None = all history.

    ``hours`` gives a rolling last-``hours`` bound as a full ISO timestamp (the
    dashboard's "1 day" button sends ``hours=24`` here exactly as it does to the
    Overview, so one button means one window on both tabs); ``days`` gives a
    trailing ``YYYY-MM-DD`` calendar bound. :func:`cache.since_clause` compares
    either against ``last_ts``.

    What stays day-shaped regardless: rows are filtered by the thread's LAST
    ACTIVE time, not per-call timestamps, so a long-running thread's full
    lifetime cost lands in whichever window that last moment falls into. This
    tab ranks threads and tools; the Overview is where a per-call time series
    lives.
    """
    from olano.analytics.aggregate import _hour_window_bounds, _window_bounds

    if hours:
        _, since, _ = _hour_window_bounds(hours)
        return since
    if not days:
        return None
    _, since, _ = _window_bounds(days)
    return since


# Ledger origins that mean "a human was waiting for this". Everything else —
# scheduled, cognition, delegate, pulse, highlights, reports, acp, peer — is
# work the fleet did on its own. `turns` is the pre-origin legacy bucket, which
# the ledger documents as plain runner turns.
_LEDGER_INTERACTIVE_ORIGINS = {resolver.ORIGIN_INTERACTIVE, "turns", ""}


def _ledger_agent_rollup(
    since: str | None, *, agents: set[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Per-agent spend from the usage ledger, with the interactive/autonomous split.

    **This is the money the drill-down shows**, and it is the same store the
    Overview reads — so an agent's total is one number across the whole page.
    It used to be mined from ``threads.db`` here, which meant the landing card
    of the drill-down disagreed with the card one tab over by whatever
    compaction had dropped: a fleet's top spender read $7.81 against the
    Overview's $2,800 for the same window, because 36k of its scheduled calls
    lived in three long-since-compacted threads.

    Mining still answers *which conversation and which tool* — see
    :func:`agents_ranked` for how the two are kept apart on one row.
    """
    from olano.analytics.usage_ledger import iter_usage_events

    def _blank() -> dict[str, Any]:
        return {
            "usd": 0.0,
            "credits": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "calls": 0,
            "interactive_usd": 0.0,
            "autonomous_usd": 0.0,
            "interactive_calls": 0,
            "autonomous_calls": 0,
            "threads": set(),
        }

    out: dict[str, dict[str, Any]] = {}
    try:
        for ev in iter_usage_events(since=since):
            if agents is not None and ev.agent not in agents:
                continue
            b = out.setdefault(ev.agent, _blank())
            b["usd"] += ev.usd
            b["credits"] += float(ev.credits or 0.0)
            b["input_tokens"] += ev.input_tokens
            b["output_tokens"] += ev.output_tokens
            b["calls"] += ev.calls
            side = (
                "interactive" if (ev.origin or "") in _LEDGER_INTERACTIVE_ORIGINS else "autonomous"
            )
            b[f"{side}_usd"] += ev.usd
            b[f"{side}_calls"] += ev.calls
            if ev.thread_id:
                b["threads"].add(ev.thread_id)
    except Exception:  # pragma: no cover - defensive
        logger.debug("threadcost: ledger agent rollup failed", exc_info=True)
    return out


def _attach_credits_book(agents: list[dict], credits_by_agent: dict[str, float]) -> None:
    """Give every mined agent row its Olano-credit figure, in place.

    An agent that ran *only* on Olano models still has cached threads (mined
    normally, priced at $0), so it is usually already in ``agents``; the append
    below covers the case where it isn't — dropping it would make this page's
    credits total disagree with the Insights one for no reason the reader can
    see. Such a row is honest about being empty: 0 threads, 0 calls, and the
    thread list it opens says so.
    """
    seen = {a.get("agent") or "" for a in agents}
    for a in agents:
        a["credits"] = round(credits_by_agent.get(a.get("agent") or "", 0.0), 6)
    for name, credits in credits_by_agent.items():
        if name in seen or not credits:
            continue
        agents.append(
            {
                "agent": name,
                "usd": 0.0,
                "credits": round(credits, 6),
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cache_read_tokens": 0,
                "call_count": 0,
                "thread_count": 0,
                "pricing_complete": 1,
                "interactive_usd": 0.0,
                "autonomous_usd": 0.0,
            }
        )


def agents_ranked(
    home: str | Path | None = None,
    *,
    agents_filter: set[str] | None = None,
    days: int | None = None,
    hours: int | None = None,
) -> dict[str, Any]:
    """Agents ranked by spend. ``agents_filter`` scopes rows AND totals to the
    given agent names (multi-user member view); ``days``/``hours`` limit the
    window (neither = all history).

    **Money comes from the usage ledger; navigability comes from the index.**
    Every money figure on a row — ``usd``, ``credits``, ``call_count``,
    ``total_tokens`` and the interactive/autonomous split — is the same
    per-call ledger the Overview reads, so an agent's total is one number
    across the whole page. ``thread_count`` and ``inspectable_usd`` are the
    mined half: how many of that agent's conversations the checkpoint store can
    still open, and how much of the spend those conversations account for.

    That division is the point. Mining is the only thing that can attribute a
    cost to a *thread* or a *tool*, but it reads the live checkpoint state, and
    compaction deletes the messages it reads — so it under-reports exactly the
    long, expensive conversations. Ranking agents by it made this page disagree
    with the Insights window next door: a fleet's top spender read **$7.81**
    against the Overview's **$2,800**, because 36k of its scheduled calls lived
    in three long-since-compacted threads. Now the ranking matches, and
    ``inspectable_usd`` says how much of it you can actually drill into.

    ``credits`` is the gateway's authoritative charge for managed calls; ``usd``
    is the BYOK book (``cost_for_call`` prices an Olano-managed call at 0 on
    purpose). The two are never summed.
    """
    since = _since_bound(days, hours)
    ledger = _ledger_agent_rollup(since, agents=agents_filter)

    mined: dict[str, dict] = {}
    with _warm_cache_conn(home) as (cc, status):
        if cc is not None:
            mined = {str(r.get("agent") or ""): r for r in cache.rank_agents(cc, since=since)}

    agents: list[dict[str, Any]] = []
    for name in set(ledger) | set(mined):
        if agents_filter is not None and name not in agents_filter:
            continue
        led = ledger.get(name)
        mine = mined.get(name, {})
        if led is None:
            # Mined but never ledgered: pre-instrumentation history. Show it
            # rather than dropping the agent, flagged by inspectable == total.
            led = {
                "usd": float(mine.get("usd") or 0.0),
                "credits": 0.0,
                "input_tokens": int(mine.get("total_tokens") or 0),
                "output_tokens": 0,
                "calls": int(mine.get("call_count") or 0),
                "interactive_usd": float(mine.get("usd") or 0.0),
                "autonomous_usd": 0.0,
            }
        agents.append(
            {
                "agent": name,
                "usd": round(float(led["usd"]), 6),
                "credits": round(float(led["credits"]), 6),
                "total_tokens": int(led["input_tokens"] + led["output_tokens"]),
                "call_count": int(led["calls"]),
                "interactive_usd": round(float(led["interactive_usd"]), 6),
                "autonomous_usd": round(float(led["autonomous_usd"]), 6),
                # The mined half: what you can open, and what it accounts for.
                "thread_count": int(mine.get("thread_count") or 0),
                "inspectable_usd": round(float(mine.get("usd") or 0.0), 6),
                "cache_read_tokens": int(mine.get("cache_read_tokens") or 0),
                "pricing_complete": int(mine.get("pricing_complete") or 1),
            }
        )
    # Managed rows carry usd == 0 by design, so credits breaks the tie.
    agents.sort(key=lambda r: (r["usd"], r["credits"]), reverse=True)
    totals = {
        "usd": round(sum(a["usd"] for a in agents), 6),
        "credits": round(sum(a["credits"] for a in agents), 6),
        "total_tokens": sum(a["total_tokens"] for a in agents),
        "thread_count": sum(a["thread_count"] for a in agents),
        "call_count": sum(a["call_count"] for a in agents),
        "inspectable_usd": round(sum(a["inspectable_usd"] for a in agents), 6),
    }
    return {"agents": agents, "totals": totals, "index_status": status}


def threads_ranked(
    agent: str | None = None,
    *,
    origin: str | None = None,
    limit: int = 50,
    offset: int = 0,
    days: int | None = None,
    hours: int | None = None,
    home: str | Path | None = None,
) -> dict[str, Any]:
    if not _has_thread_db(home):
        return {"threads": [], "total": 0}
    since = _since_bound(days, hours)
    index = resolver.build_thread_agent_index(home)
    with closing(reader.open_thread_db(home)) as tc, closing(cache.connect(home)) as cc:
        warm_cache(cc, tc, index, priority_agent=agent, home=home)
        rows, total = cache.rank_threads(
            cc, agent=agent, origin=origin, since=since, limit=limit, offset=offset
        )
    for r in rows:
        agent = r.get("agent") or ""
        # r["thread_id"] is the scoped checkpoint key; the transcript label/preview
        # live under the human-facing stem.
        stem = unscope_thread(agent, r["thread_id"])
        name, preview = _thread_label(agent, stem, home)
        r["name"] = name
        r["preview"] = preview
    return {"threads": rows, "total": total}


def thread_calls(thread_id: str, home: str | Path | None = None) -> dict[str, Any]:
    # ``thread_id`` is the agent-scoped checkpoint key (Insights drill-down passes it
    # through; the chat cost-enrichment call site scopes the raw stem before calling).
    index = resolver.build_thread_agent_index(home)
    agent = resolver.resolve_agent(thread_id, index)
    stem = unscope_thread(agent, thread_id)
    if not _has_thread_db(home):
        return {
            "thread_id": thread_id,
            "agent": agent,
            "origin": "interactive",
            "totals": {
                "usd": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cache_read_tokens": 0,
                "call_count": 0,
            },
            "calls": [],
            "turn_tree": [],
            "tools": [],
        }
    with closing(reader.open_thread_db(home)) as tc:
        # The same cap as the warmer: this runs on every thread open in the
        # dashboard (the chat header's running tally) and on the drill-down,
        # and a single too-large read is the whole process's memory.
        too_large, size = history_too_large(tc, thread_id)
        if too_large:
            return {
                "thread_id": thread_id,
                "agent": agent,
                "origin": resolver.classify_origin(thread_id, _first_entry(agent, stem, home)),
                "totals": {
                    "usd": 0.0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cache_read_tokens": 0,
                    "call_count": 0,
                },
                "calls": [],
                "turn_tree": [],
                "tools": [],
                "skipped": _skipped_payload(size),
            }
        callset = reader.read_thread_callset(tc, thread_id)
    calls = callset.all_calls()
    tree = attribution.build_turn_tree(callset)
    tools = attribution.attribute_tools(callset)
    return {
        "thread_id": thread_id,
        "agent": agent,
        "origin": resolver.classify_origin(thread_id, _first_entry(agent, stem, home)),
        "totals": {
            "usd": round(sum(c.usd for c in calls), 6),
            "input_tokens": sum(c.input_tokens for c in calls),
            "output_tokens": sum(c.output_tokens for c in calls),
            "total_tokens": sum(c.total_tokens for c in calls),
            "cache_read_tokens": sum(c.cache_read_tokens for c in calls),
            "call_count": len(calls),
        },
        "calls": [
            {
                "seq": c.seq,
                "ns": c.ns,
                "model": c.model,
                "input_tokens": c.input_tokens,
                "output_tokens": c.output_tokens,
                "total_tokens": c.total_tokens,
                "cache_read_tokens": c.cache_read_tokens,
                "usd": round(c.usd, 6),
                "pricing_known": c.pricing_known,
                "tool_calls": [
                    {"name": tc.name, "subagent_type": tc.subagent_type} for tc in c.tool_calls
                ],
                "ts": c.ts,
            }
            for c in calls
        ],
        "turn_tree": [n.as_dict() for n in tree],
        "tools": [t.as_dict() for t in tools],
    }


def _content_text(msg: Any) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or json.dumps(block)))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def _truncate(text: str) -> dict:
    full = len(text)
    return {
        "content": text[:_RAW_MAX_FIELD],
        "truncated": full > _RAW_MAX_FIELD,
        "full_len": full,
    }


_ROLE_BY_KIND = {
    "HumanMessage": "user",
    "AIMessage": "assistant",
    "AIMessageChunk": "assistant",
    "ToolMessage": "tool",
    "SystemMessage": "system",
}


def call_raw(thread_id: str, ns: str, seq: int, home: str | Path | None = None) -> dict[str, Any]:
    """Raw prompt context + response for one call (the 'ultimate raw data')."""
    if not _has_thread_db(home):
        return {"thread_id": thread_id, "ns": ns, "seq": seq, "found": False}
    with closing(reader.open_thread_db(home)) as tc:
        # Same cap as every other reader: the drill-down opens one call of a
        # thread the ranking already listed, and that listing can predate the
        # thread's growth, so the row is no proof the whole thread fits.
        too_large, size = history_too_large(tc, thread_id)
        if too_large:
            return {
                "thread_id": thread_id,
                "ns": ns,
                "seq": seq,
                "found": False,
                "skipped": _skipped_payload(size),
            }
        messages, error = reader.read_namespace_messages_ex(tc.saver, thread_id, ns)
    if not messages or seq < 0 or seq >= len(messages):
        out: dict[str, Any] = {"thread_id": thread_id, "ns": ns, "seq": seq, "found": False}
        if error:
            out["error"] = error
        return out

    response_msg = messages[seq]
    context = messages[:seq]
    omitted = max(0, len(context) - _RAW_MAX_MESSAGES)
    context = context[-_RAW_MAX_MESSAGES:]

    request_messages = []
    for m in context:
        field = _truncate(_content_text(m))
        request_messages.append(
            {
                "role": _ROLE_BY_KIND.get(type(m).__name__, "other"),
                "name": getattr(m, "name", "") or "",
                **field,
            }
        )

    usage = reader._as_dict(getattr(response_msg, "usage_metadata", None))
    meta = reader._as_dict(getattr(response_msg, "response_metadata", None))
    model = str(meta.get("model_name") or meta.get("model") or "unknown")
    resp = _truncate(_content_text(response_msg))
    tool_calls = [
        {
            "name": str(tc.get("name") or ""),
            "args": json.dumps(tc.get("args") or "")[:_RAW_MAX_FIELD],
        }
        for tc in (getattr(response_msg, "tool_calls", None) or [])
        if isinstance(tc, dict)
    ]
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    cache_read = int(reader._as_dict(usage.get("input_token_details")).get("cache_read") or 0)
    return {
        "thread_id": thread_id,
        "ns": ns,
        "seq": seq,
        "found": True,
        "model": model,
        "omitted_context": omitted,
        "request_messages": request_messages,
        "response": {**resp, "tool_calls": tool_calls},
        "usage": {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_read_tokens": cache_read,
        },
        "usd": round(cost_for_call(model, in_tok, out_tok, cache_read), 6),
    }


def tools_rollup(
    agent: str | None = None,
    thread_id: str | None = None,
    home: str | Path | None = None,
    *,
    agents_filter: set[str] | None = None,
    days: int | None = None,
    hours: int | None = None,
) -> dict[str, Any]:
    """Per-tool cost attribution. ``agents_filter`` scopes the global (no
    agent / no thread) rollup's candidate thread set to the given agents —
    rows carry no agent field, so scoping must happen before mining.
    ``days``/``hours`` limit that candidate set to threads last active in the
    trailing window (ignored for a single-thread rollup, which is already one
    thread)."""
    if not _has_thread_db(home):
        return {"tools": [], "totals": {"usd": 0.0, "threads_scanned": 0, "capped": False}}
    since = _since_bound(days, hours)
    index = resolver.build_thread_agent_index(home)
    rows: dict[str, attribution.ToolAttribution] = {}
    capped = False
    with closing(reader.open_thread_db(home)) as tc:
        if thread_id:
            target_ids = [thread_id]
        elif agent:
            with closing(cache.connect(home)) as cc:
                warm_cache(cc, tc, index, priority_agent=agent, home=home)
                ranked, _ = cache.rank_threads(
                    cc, agent=agent, since=since, limit=_AGENT_TOOLS_THREAD_CAP
                )
            target_ids = [r["thread_id"] for r in ranked]
            capped = len(target_ids) >= _AGENT_TOOLS_THREAD_CAP
        else:
            with closing(cache.connect(home)) as cc:
                warm_cache(cc, tc, index, home=home)
                # Scope the ranking to the member's agents inside the query, so
                # their threads aren't truncated by a fleet-wide top-N cap
                # dominated by agents they can't see (which would return an
                # empty rollup with a misleading capped=False).
                ranked, _ = cache.rank_threads(
                    cc, agents=agents_filter, since=since, limit=_AGENT_TOOLS_THREAD_CAP
                )
            target_ids = [r["thread_id"] for r in ranked]
            capped = len(target_ids) >= _AGENT_TOOLS_THREAD_CAP

        skipped: list[dict[str, Any]] = []
        scanned = 0
        for tid in target_ids:
            # The ranking lists a thread off its cached row, which can predate
            # the growth that put it over the cap; measure before reading.
            too_large, size = history_too_large(tc, tid)
            if too_large:
                skipped.append({"thread_id": tid, **_skipped_payload(size)})
                continue
            scanned += 1
            callset = reader.read_thread_callset(tc, tid)
            for attr in attribution.attribute_tools(callset):
                b = rows.setdefault(attr.tool, attribution.ToolAttribution(attr.tool))
                b.calls += attr.calls
                b.input_tokens += attr.input_tokens
                b.output_tokens += attr.output_tokens
                b.usd += attr.usd
                b.subagent_internal_usd += attr.subagent_internal_usd

    tools = sorted(rows.values(), key=lambda b: b.usd, reverse=True)
    return {
        "tools": [t.as_dict() for t in tools],
        "totals": {
            "usd": round(sum(t.usd for t in tools), 6),
            "threads_scanned": scanned,
            "capped": capped,
        },
        "skipped": skipped,
    }


def origins(
    home: str | Path | None = None,
    *,
    agents_filter: set[str] | None = None,
    days: int | None = None,
    hours: int | None = None,
) -> dict[str, Any]:
    """Interactive-vs-autonomous spend split, from the usage ledger.

    These two cards sit directly above the agent ranking, so they read the same
    store it does (see :func:`agents_ranked`) — a chat-vs-autonomous split mined
    from checkpoints would under-report the autonomous side hardest, which is
    the side that runs unattended and therefore the side an operator is
    checking. ``thread_count`` stays the *mined* count of openable
    conversations, the one figure on this page that is about navigation rather
    than money. ``agents_filter`` restricts to those agents (member view);
    ``days``/``hours`` bound the window (neither = all history).
    """
    since = _since_bound(days, hours)
    ledger = _ledger_agent_rollup(since, agents=agents_filter)

    split: list[dict] = []
    mined_threads = {"interactive": 0, "non_interactive": 0}
    with _warm_cache_conn(home) as (cc, _status):
        if cc is not None:
            split = cache.origin_split(cc, agents=agents_filter, since=since)
    for r in split:
        key = (
            "interactive"
            if r["origin"] == resolver.ORIGIN_INTERACTIVE
            else "non_interactive"
            if r["origin"] in resolver.NON_INTERACTIVE
            else None
        )
        if key:
            mined_threads[key] += int(r.get("thread_count") or 0)

    def _bucket(threads_key: str, side: str) -> dict:
        return {
            "usd": round(sum(float(b[f"{side}_usd"]) for b in ledger.values()), 6),
            "call_count": sum(int(b[f"{side}_calls"]) for b in ledger.values()),
            "thread_count": mined_threads[threads_key],
        }

    return {
        "split": split,
        "interactive": _bucket("interactive", "interactive"),
        "non_interactive": _bucket("non_interactive", "autonomous"),
    }


def _ledger_credits_overlay(
    since: str | None, *, agents: set[str] | None = None, day_only: str | None = None
) -> dict[str, Any]:
    """Olano-credit totals from the usage ledger, keyed like the mined rollup.

    The drill-down mines ``threads.db``, and checkpoints carry no gateway
    charge — the per-call ``credits`` number exists only in the usage ledger. So
    its mined usd/token rows get their credits grafted from here rather than
    pretending the checkpoint cache knew them. (The Insights rollups read the
    ledger directly and need no overlay; see :func:`usage_rollup`.) Shape:
    ``{by_agent, by_model, by_day, by_source, by_origin, total}`` where by_*
    map key → credits float, and by_source / by_origin rows carry the full
    bucket (both accounting systems are summed per key, never into each other).
    """
    from olano.analytics.aggregate import tier_of
    from olano.analytics.usage_ledger import iter_usage_events

    def _blank() -> dict[str, float]:
        return {"usd": 0.0, "credits": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0}

    by_agent: dict[str, float] = {}
    by_model: dict[str, float] = {}
    by_day: dict[str, float] = {}
    by_source: dict[str, dict[str, float]] = {}
    by_origin: dict[str, dict[str, float]] = {}
    by_tier: dict[str, dict[str, float]] = {}
    total = 0.0
    try:
        for ev in iter_usage_events(since=since):
            if agents is not None and ev.agent not in agents:
                continue
            if day_only is not None and ev.date != day_only:
                continue
            sb = by_source.setdefault(ev.billing_source, _blank())
            # by_origin/by_tier mirror by_source: full buckets, accumulated
            # before the credits skip so unpriced rows still count.
            ob = by_origin.setdefault(ev.origin or "turns", _blank())
            tier = tier_of(ev.model)
            buckets = [sb, ob]
            if tier is not None:
                buckets.append(by_tier.setdefault(tier, _blank()))
            for bucket in buckets:
                bucket["usd"] += ev.usd
                bucket["input_tokens"] += ev.input_tokens
                bucket["output_tokens"] += ev.output_tokens
                # UsageEvent.calls folds a turn's model sub-calls into one row,
                # so these call counts line up with the mined `call_count`.
                bucket["calls"] += ev.calls
            credits = float(ev.credits or 0.0)
            if not credits:
                continue
            for bucket in buckets:
                bucket["credits"] += credits
            by_agent[ev.agent] = by_agent.get(ev.agent, 0.0) + credits
            by_model[ev.model] = by_model.get(ev.model, 0.0) + credits
            by_day[ev.date] = by_day.get(ev.date, 0.0) + credits
            total += credits
    except Exception:  # pragma: no cover - defensive
        logger.debug("threadcost: credits overlay failed", exc_info=True)
    return {
        "by_agent": by_agent,
        "by_model": by_model,
        "by_day": by_day,
        "by_source": by_source,
        "by_origin": by_origin,
        "by_tier": by_tier,
        "total": total,
    }


_EMPTY_INDEX_STATUS: dict[str, Any] = {
    "total": 0,
    "stale": 0,
    "too_large": 0,
    "refreshed": 0,
    "remaining": 0,
    "errors": 0,
    "last_error": "",
}


@contextmanager
def _warm_cache_conn(home: str | Path | None = None) -> Iterator[tuple[Any, dict[str, Any]]]:
    """Yield ``(cache_conn | None, index_status)``, freshest available.

    Stale rows are re-mined first when the checkpoint store is reachable; when
    it isn't, the derived cache is still opened on its own — an unreachable
    store should cost freshness, not erase the rankings already mined out of it
    (same rule as :func:`_cached_rows`).
    """
    if _has_thread_db(home):
        index = resolver.build_thread_agent_index(home)
        with closing(reader.open_thread_db(home)) as tc, closing(cache.connect(home)) as cc:
            yield cc, warm_cache(cc, tc, index, home=home)
        return
    if get_olano_paths(home).threadcost_db_path.exists():
        with closing(cache.connect(home)) as cc:
            yield cc, dict(_EMPTY_INDEX_STATUS)
        return
    yield None, dict(_EMPTY_INDEX_STATUS)


def _cached_rows(
    home: str | Path | None = None, agents: set[str] | None = None
) -> list[dict[str, Any]]:
    """Every cached thread row, scoped to ``agents``, freshest available.

    Stale rows are re-mined first when the checkpoint store is reachable; when it
    isn't, the derived cache is still read on its own — an unreachable store
    should cost freshness, not erase the history already mined out of it.
    """
    rows: list[dict[str, Any]] = []
    try:
        if _has_thread_db(home):
            index = resolver.build_thread_agent_index(home)
            with closing(reader.open_thread_db(home)) as tc, closing(cache.connect(home)) as cc:
                warm_cache(cc, tc, index, home=home)
                rows = cache.all_rows(cc)
        elif get_olano_paths(home).threadcost_db_path.exists():
            with closing(cache.connect(home)) as cc:
                rows = cache.all_rows(cc)
    except Exception:  # pragma: no cover - defensive
        logger.debug("threadcost: cached rows unavailable", exc_info=True)
        return []
    if agents is not None:
        rows = [r for r in rows if (r.get("agent") or "unknown") in agents]
    return rows


def _blank_bucket() -> dict[str, float]:
    return {"usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0}


def _mined_prehistory(
    since: str,
    today: str,
    boundary: str | None,
    *,
    home: str | Path | None = None,
    agents: set[str] | None = None,
) -> dict[str, Any] | None:
    """Checkpoint-mined usage for the window days that predate the usage ledger.

    ``boundary`` is the ledger's earliest recorded day (None = an empty ledger,
    i.e. everything in the window is prehistory). Only threads last active
    strictly before it are folded in — never a day the ledger's era covers, even
    an idle one. That single rule is the whole contract:

    - **No double counting.** A thread that stopped before the ledger existed has
      no ledger rows to collide with, so every day in the window is fed by
      exactly one store and both rollups stay additive over days.
    - **Window-independent.** The seam is a property of the ledger, not of the
      window being asked for, so every window classifies a given day the same
      way — which is what makes a shorter window a true subset of a longer one.
      (Excluding merely *quiet* days instead would break both: a thread's last
      checkpoint can land a day after its last model call, so an idle day inside
      the ledger's era can carry a mined row whose calls the ledger already has.)
    - **The ledger wins wherever it has a say.** Checkpoint state is a snapshot,
      not a log — ``SummarizationMiddleware`` compaction drops the very messages
      the mining reads back, so the mined view of a busy thread is a fraction of
      what it actually spent. That is why this is prehistory backfill and not a
      co-equal source. The cost of the rule is that a thread straddling the seam
      contributes only its ledgered half; a whole thread cannot be split by day.

    Returns None when nothing was mined for those days.
    """
    by_agent: dict[str, dict[str, float]] = {}
    by_model: dict[str, dict[str, float]] = {}
    by_day: dict[str, dict[str, float]] = {}
    totals = _blank_bucket()
    days_used: set[str] = set()

    # `all_rows` already drops threads with no calls, so every row that survives
    # the window filter below is real activity worth a day of its own.
    for r in _cached_rows(home, agents):
        # A row with no last-active day counts as today, mirroring
        # cache.since_clause(); a future day (clock skew) clamps into the window
        # so totals can never carry a bucket the series has no slot for.
        day = r.get("day") or today
        if day > today:
            day = today
        if day < since or (boundary is not None and day >= boundary):
            continue
        inp, out = int(r.get("input_tokens") or 0), int(r.get("output_tokens") or 0)
        usd, calls = float(r.get("usd") or 0.0), int(r.get("call_count") or 0)
        agent = r.get("agent") or "unknown"
        days_used.add(day)
        for bucket in (
            by_agent.setdefault(agent, _blank_bucket()),
            by_day.setdefault(day, _blank_bucket()),
            totals,
        ):
            bucket["usd"] += usd
            bucket["input_tokens"] += inp
            bucket["output_tokens"] += out
            bucket["calls"] += calls
        try:
            for model, mv in json.loads(r.get("model_json") or "{}").items():
                mb = by_model.setdefault(model, _blank_bucket())
                mb["usd"] += float(mv.get("usd") or 0.0)
                mb["input_tokens"] += int(mv.get("input_tokens") or 0)
                mb["output_tokens"] += int(mv.get("output_tokens") or 0)
                mb["calls"] += int(mv.get("calls") or 1)
        except (json.JSONDecodeError, AttributeError):
            pass

    if not days_used:
        return None
    return {"by_agent": by_agent, "by_model": by_model, "by_day": by_day, "totals": totals}


def _merge_mined_rows(
    rows: list[dict[str, Any]], key_name: str, extra: dict[str, dict[str, float]]
) -> list[dict[str, Any]]:
    """Fold mined per-key buckets into emitted ledger rows, re-sorted by spend.

    Mined buckets only ever cover days the ledger never recorded, so a key
    present on both sides is being credited two disjoint sets of days.
    Credits are never touched: a checkpoint carries no gateway charge, and the
    mined model names are the gateway's wire names rather than the ``olano:*``
    aliases, so a mined row cannot be classified into a billing book at all.
    """
    from olano.analytics.aggregate import _round_usd

    merged: dict[str, dict[str, Any]] = {str(r[key_name]): dict(r) for r in rows}
    for key, agg in extra.items():
        row = merged.setdefault(
            key,
            {
                key_name: key,
                "usd": 0.0,
                "credits": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "calls": 0,
            },
        )
        row["usd"] = _round_usd(float(row["usd"]) + agg["usd"])
        row["input_tokens"] = int(row["input_tokens"]) + int(agg["input_tokens"])
        row["output_tokens"] = int(row["output_tokens"]) + int(agg["output_tokens"])
        row["total_tokens"] = int(row["input_tokens"]) + int(row["output_tokens"])
        row["calls"] = int(row["calls"]) + int(agg["calls"])
    out_rows = list(merged.values())
    out_rows.sort(key=lambda r: (float(r["usd"]), float(r["credits"])), reverse=True)
    return out_rows


def usage_rollup(
    days: int = 30, home: str | Path | None = None, *, agents: set[str] | None = None
) -> dict[str, Any]:
    """Cost rollup for a trailing ``days``-day window, sourced from the usage ledger.

    **One source per day, and the ledger owns every day it recorded.** The ledger
    is the per-call log — written at ``core/runner._record_usage`` and by every
    background writer — so it is what the cloud usage reporter ships, what the
    budget counters seed from, and the only store that can split a call by
    billing source, work origin or managed tier. Reading it here makes the whole
    payload agree with itself (``totals`` vs ``by_source`` vs ``daily`` vs
    ``by_agent``) and makes the dashboard's windows comparable: the 1-day view is
    the same ledger over a rolling 24 h, so it can no longer report *more* model
    calls or spend than the 7- or 30-day view above it.

    That inversion is deliberate. This used to mine ``threads.db`` for usd/tokens/
    calls and graft only the credits on from the ledger, which meant the same page
    quoted two different books: checkpoint state is a *snapshot*, not a log, so
    every message compaction dropped erases the calls that produced it, and the
    mined figure for a busy agent came back orders of magnitude under its real
    spend — while the 1-day window, already ledger-sourced, showed the truth.

    Checkpoint mining survives only for window days that predate the ledger's
    first row (see :func:`_mined_prehistory`), so a long window on a younger
    ledger still shows that history instead of a cliff of zeros. ``source`` says
    which stores fed the payload. ``agents`` restricts everything (rows, totals,
    models, series) to those agents (member scoping).
    """
    from olano.analytics import aggregate
    from olano.analytics.aggregate import _round_usd, _window_bounds
    from olano.analytics.usage_ledger import ledger_start_day

    _, since, all_days = _window_bounds(days)
    today = all_days[-1]

    payload = aggregate.usage_rollup(days, agents=agents)
    payload["source"] = "usage_ledger"

    # The ledger's era starts at its earliest row and runs to now; the mined
    # cache fills only what came before it. `agents` deliberately does NOT scope
    # the boundary — where the ledger's history begins is a property of the box,
    # and per-member seams would put two viewers on different day boundaries.
    # A window that starts inside that era has no prehistory to fetch, so it
    # skips the mining pass entirely (the drill-down warms the cache on its own
    # requests; this one no longer re-mines up to 150 threads to discard them).
    boundary = ledger_start_day()
    mined = (
        None
        if boundary is not None and boundary <= since
        else _mined_prehistory(since, today, boundary, home=home, agents=agents)
    )
    if mined is None:
        return payload

    payload["source"] = (
        "checkpoint_cache" if boundary is None else "usage_ledger+checkpoint_prehistory"
    )
    payload["by_agent"] = _merge_mined_rows(payload["by_agent"], "agent", mined["by_agent"])
    payload["by_model"] = _merge_mined_rows(payload["by_model"], "model", mined["by_model"])

    # Re-walk the (zero-filled, ordered) series so the running cumulatives stay
    # consistent with the grafted days.
    cum_usd = cum_credits = 0.0
    cum_tokens = 0
    for row in payload["daily"]:
        add = mined["by_day"].get(str(row.get("date")))
        if add is not None:
            row["usd"] = _round_usd(float(row["usd"]) + add["usd"])
            row["input_tokens"] = int(row["input_tokens"]) + int(add["input_tokens"])
            row["output_tokens"] = int(row["output_tokens"]) + int(add["output_tokens"])
            row["total_tokens"] = int(row["input_tokens"]) + int(row["output_tokens"])
            row["calls"] = int(row["calls"]) + int(add["calls"])
        cum_usd += float(row["usd"])
        cum_credits += float(row["credits"])
        cum_tokens += int(row["total_tokens"])
        row["cum_usd"] = _round_usd(cum_usd)
        row["cum_credits"] = _round_usd(cum_credits)
        row["cum_tokens"] = cum_tokens

    t, mt = payload["totals"], mined["totals"]
    t["usd"] = _round_usd(float(t["usd"]) + mt["usd"])
    t["input_tokens"] = int(t["input_tokens"]) + int(mt["input_tokens"])
    t["output_tokens"] = int(t["output_tokens"]) + int(mt["output_tokens"])
    t["total_tokens"] = int(t["input_tokens"]) + int(t["output_tokens"])
    t["calls"] = int(t["calls"]) + int(mt["calls"])
    t["agents"] = len(payload["by_agent"])
    t["models"] = len(payload["by_model"])
    return payload


def overview(
    days: int = 30,
    home: str | Path | None = None,
    *,
    hours: int | None = None,
    agents: set[str] | None = None,
) -> dict[str, Any]:
    """Full Insights overview: usage from the per-call usage ledger, tool-call
    counts and A2A edges from the transcript / A2A logs.

    ``hours`` switches to a rolling last-``hours`` window with hourly resolution
    (the dashboard's "1 day" view sends ``hours=24``); otherwise the window is
    ``days`` trailing UTC calendar days. **Both read the same ledger**, which is
    what makes the dashboard's window buttons comparable — a rolling 24 h is a
    subset of the last 7 calendar days, so the shorter window can never report
    more calls or more spend than the longer one. It did exactly that while the
    daily windows mined ``threads.db`` instead (see :func:`usage_rollup`).
    ``agents`` scopes every rollup (usage/tools/a2a — A2A keeps exchanges with at
    least one granted participant) for the multi-user member view.
    """
    from datetime import datetime, timezone

    from olano.analytics import aggregate

    payload: dict[str, Any]
    if hours is not None:
        hours = max(1, min(int(hours or 24), 168))
        _, since, _ = aggregate._hour_window_bounds(hours)
        payload = {
            "window_days": max(1, -(-hours // 24)),
            "window_hours": hours,
            "since": since,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "usage": aggregate.usage_rollup_hourly(hours, agents=agents),
            "tools": aggregate.tool_rollup(hours=hours, agents=agents),
            "services": aggregate.service_rollup(hours=hours, agents=agents),
            "a2a": aggregate.a2a_rollup(hours=hours, agents=agents),
        }
    else:
        days = max(1, min(int(days or 30), 366))
        _, since, _ = aggregate._window_bounds(days)
        payload = {
            "window_days": days,
            "since": since,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "usage": usage_rollup(days, home=home, agents=agents),
            "tools": aggregate.tool_rollup(days, agents=agents),
            # Non-model service spend (image / speech / managed web) — always from
            # the service ledger; the checkpoint store has no notion of a call that
            # produced no tokens.
            "services": aggregate.service_rollup(days, agents=agents),
            "a2a": aggregate.a2a_rollup(days, agents=agents),
        }
    # The one combined olano-credits figure for the window: managed model spend
    # (usage ledger) + managed service spend (service ledger). Same unit, same
    # book — this is the local twin of what the cloud bills for the window, and
    # the number the dashboard headlines. (BYOK usd never folds in.)
    usage_totals = (payload["usage"] or {}).get("totals") or {}
    service_totals = (payload["services"] or {}).get("totals") or {}
    model_credits = float(usage_totals.get("credits") or 0.0)
    service_credits = float(service_totals.get("credits") or 0.0)
    payload["credits_summary"] = {
        "model": round(model_credits, 6),
        "services": round(service_credits, 6),
        "total": round(model_credits + service_credits, 6),
    }
    return payload


def _aggregate_day_usage(rows: list[dict]) -> dict[str, Any]:
    """Aggregate cached thread rows into the journal's per-day usage shape.

    Pure (no I/O) so it's unit-testable: ``{by_agent, by_model, totals}`` with each
    bucket = ``{usd, input_tokens, output_tokens, calls}``.
    """

    def _blank() -> dict[str, float]:
        return {"usd": 0.0, "credits": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0}

    by_agent: dict[str, dict[str, float]] = {}
    by_model: dict[str, dict[str, float]] = {}
    totals = _blank()
    for r in rows:
        agent = r.get("agent") or "unknown"
        inp, out = int(r.get("input_tokens") or 0), int(r.get("output_tokens") or 0)
        usd, calls = float(r.get("usd") or 0.0), int(r.get("call_count") or 0)
        ab = by_agent.setdefault(agent, _blank())
        for b in (ab, totals):
            b["usd"] += usd
            b["input_tokens"] += inp
            b["output_tokens"] += out
            b["calls"] += calls
        try:
            for model, mv in json.loads(r.get("model_json") or "{}").items():
                mb = by_model.setdefault(model, _blank())
                mb["usd"] += float(mv.get("usd") or 0.0)
                mb["input_tokens"] += int(mv.get("input_tokens") or 0)
                mb["output_tokens"] += int(mv.get("output_tokens") or 0)
                mb["calls"] += int(mv.get("calls") or 0)
        except (json.JSONDecodeError, AttributeError):
            pass
    return {"by_agent": by_agent, "by_model": by_model, "totals": totals}


def _ledger_day_usage(day: str) -> dict[str, Any] | None:
    """One day's usage from the usage ledger (same shape as ``_aggregate_day_usage``),
    or None when the ledger has nothing for that day."""
    from olano.analytics.usage_ledger import iter_usage_events

    def _blank() -> dict[str, float]:
        return {"usd": 0.0, "credits": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0}

    by_agent: dict[str, dict[str, float]] = {}
    by_model: dict[str, dict[str, float]] = {}
    totals = _blank()
    try:
        for ev in iter_usage_events(since=day):
            if ev.date != day:
                continue
            for b in (
                by_agent.setdefault(ev.agent, _blank()),
                by_model.setdefault(ev.model, _blank()),
                totals,
            ):
                b["usd"] += ev.usd
                b["credits"] += float(ev.credits or 0.0)
                b["input_tokens"] += ev.input_tokens
                b["output_tokens"] += ev.output_tokens
                # One row folds a turn's model sub-calls (UsageEvent.calls), so
                # counting rows instead of calls under-reports every tool-heavy
                # turn — and makes this day disagree with the window rollups,
                # which have always summed `ev.calls`.
                b["calls"] += ev.calls
    except Exception:  # pragma: no cover - defensive
        logger.debug("threadcost: ledger day read failed", exc_info=True)
        return None
    if totals["calls"] <= 0:
        return None
    return {"by_agent": by_agent, "by_model": by_model, "totals": totals}


def day_usage(day: str, home: str | Path | None = None) -> dict[str, Any]:
    """Per-day usage (by_agent / by_model / totals) for the work journal.

    Same rule as :func:`usage_rollup`, one day wide: the usage ledger owns the
    day whenever it recorded anything on it, so the journal's per-agent spend is
    the same number the Insights window quotes over that day. Checkpoint mining
    covers only a day the ledger never saw — it under-reports whatever
    compaction has since dropped out of the thread state, which is exactly the
    busiest agents (the journal used to show a fleet's top spender at a few
    dollars on a day the ledger had it at four figures).
    """
    empty = {
        "by_agent": {},
        "by_model": {},
        "totals": {"usd": 0.0, "credits": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0},
    }
    from olano.analytics.usage_ledger import ledger_start_day

    ledger = _ledger_day_usage(day)
    if ledger is not None:
        return ledger
    # Same seam as usage_rollup: inside the ledger's era, a day it recorded
    # nothing on was genuinely idle. Mining it would resurrect a thread whose
    # calls the ledger has already counted on a neighbouring day (a thread's
    # last checkpoint can outlive its last model call) and put the journal back
    # out of step with the window totals.
    boundary = ledger_start_day()
    if boundary is not None and day >= boundary:
        return empty
    rows = [r for r in _cached_rows(home) if (r.get("day") or "") == day]
    if not rows:
        return empty
    # Pre-ledger, so there are no gateway charges to graft either — a mined day
    # is a dollars-only day by construction.
    return _aggregate_day_usage(rows)


def diagnostics(home: str | Path | None = None) -> dict[str, Any]:
    """One-call health check for the Insights/threadcost pipeline.

    Answers, in a single payload, the questions a blank Insights page raises:
    which checkpoint backend is active, whether the store is reachable, how many
    threads the session index / checkpoint store / derived cache each see, whether
    a live checkpoint read round-trips (and the exact error when it doesn't), and
    whether the checkpointed AI messages actually carry ``usage_metadata`` to mine.
    Served at ``GET /api/analytics/threadcost/diagnostics``.
    """
    from datetime import datetime, timezone

    from olano.analytics.usage_ledger import iter_usage_events
    from olano.config.home import load_home_config
    from olano.config.home import resolve_checkpoint_backend as _resolve_ckpt

    out: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "backend": None,
        "store_available": False,
        "session_threads": 0,
        "checkpoint_threads": 0,
        "indexed_candidates": 0,
        "cached_threads": 0,
        "cached_with_activity": 0,
        "probe": None,
        "ledger": {"events": 0, "latest_ts": ""},
    }
    try:
        backend, _url = _resolve_ckpt(load_home_config(home))
        out["backend"] = backend
    except Exception as exc:
        out["backend_error"] = f"{type(exc).__name__}: {exc}"

    index = resolver.build_thread_agent_index(home)
    out["session_threads"] = len(index)

    try:
        out["store_available"] = reader.thread_store_available(home)
    except Exception as exc:
        out["store_error"] = f"{type(exc).__name__}: {exc}"

    if out["store_available"]:
        try:
            with closing(reader.open_thread_db(home)) as tc:
                versions = reader.all_thread_versions(tc)
                out["checkpoint_threads"] = len(versions)
                candidates = _mining_candidates(index, versions)
                out["indexed_candidates"] = len(candidates)
                out["cognition_threads"] = sum(1 for t in versions if t.startswith("cog::"))
                # Probe the most recently checkpointed candidate (any thread when
                # the index matches nothing) with a live state read.
                pool = candidates or list(versions)
                if pool:
                    target = max(pool, key=lambda t: versions.get(t, ""))
                    # A diagnostics page must not be the request that loads
                    # the one thread every other reader refuses.
                    too_large, size = history_too_large(tc, target)
                    if too_large:
                        out["probe"] = {
                            "thread_id": target,
                            "ok": False,
                            "error": "history_too_large",
                            "messages": 0,
                            "ai_messages": 0,
                            "ai_with_usage": 0,
                            "skipped": _skipped_payload(size),
                        }
                    else:
                        messages, error = reader.read_namespace_messages_ex(tc.saver, target, "")
                        ai = [m for m in messages if reader._is_ai(m)]
                        with_usage = [
                            m for m in ai if reader._as_dict(getattr(m, "usage_metadata", None))
                        ]
                        out["probe"] = {
                            "thread_id": target,
                            "ok": error is None,
                            "error": error,
                            "messages": len(messages),
                            "ai_messages": len(ai),
                            "ai_with_usage": len(with_usage),
                        }
        except Exception as exc:
            out["store_error"] = f"{type(exc).__name__}: {exc}"

    mined_window = {"calls": 0, "usd": 0.0}
    try:
        with closing(cache.connect(home)) as cc:
            row = cc.execute(
                "SELECT COUNT(*), COALESCE(SUM(call_count > 0), 0) FROM thread_cost"
            ).fetchone()
            out["cached_threads"] = int(row[0] or 0)
            out["cached_with_activity"] = int(row[1] or 0)
            # Read (never warm) the 30-day slice for the drift figure below.
            row = cc.execute(
                "SELECT COALESCE(SUM(call_count), 0), COALESCE(SUM(usd), 0) "
                "FROM thread_cost WHERE day >= ?",
                [_since_bound(30)],
            ).fetchone()
            mined_window = {"calls": int(row[0] or 0), "usd": round(float(row[1] or 0.0), 6)}
    except Exception as exc:  # pragma: no cover - defensive
        out["cache_error"] = f"{type(exc).__name__}: {exc}"

    try:
        events = 0
        latest = ""
        for ev in iter_usage_events():
            events += 1
            latest = ev.ts
        out["ledger"] = {"events": events, "latest_ts": latest}
    except Exception as exc:  # pragma: no cover - defensive
        out["ledger_error"] = f"{type(exc).__name__}: {exc}"

    # How far the two stores have drifted apart over the last 30 days. The
    # ledger is what Insights reports; the mined cache is what the drill-down
    # can still attribute to a thread, and compaction only ever takes calls out
    # of it — so `mined` below the ledger is expected, and the ratio says by how
    # much. This is the number that names the class of bug where a page mixed
    # the two: a fleet reading 38k calls one way and 3k the other.
    try:
        from olano.analytics import aggregate

        led = aggregate.usage_rollup(30)["totals"]
        out["window_30d"] = {
            "ledger": {"calls": int(led["calls"]), "usd": float(led["usd"])},
            "mined": mined_window,
            "mined_call_ratio": (
                round(mined_window["calls"] / led["calls"], 4) if led["calls"] else None
            ),
        }
    except Exception as exc:  # pragma: no cover - defensive
        out["window_30d_error"] = f"{type(exc).__name__}: {exc}"

    # What migration 0014 found and corrected in the usage ledger. Surfaced
    # here because the operators who need it have no shell on the box — the
    # correction rewrites billing history, so "what changed, and where is the
    # backup" has to be answerable from the dashboard.
    audit_path = get_olano_paths(home).analytics_dir / "rebill-audit.json"
    if audit_path.exists():
        try:
            out["rebill_audit"] = json.loads(audit_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):  # pragma: no cover - defensive
            logger.debug("threadcost: rebill audit unreadable", exc_info=True)

    # Cloud usage reporting (feeds cloud.olano.ai → Usage): whether the
    # per-deployment token landed on this box (cloud-provisioned boxes get it
    # automatically via /opt/olano/olano.env) and how far the reporter has
    # shipped the ledger — backlog_bytes > 0 persistently means shipping fails.
    try:
        from olano.deployment import usage_reporter as _rep
        from olano.deployment.sources import cloud_api_url, deployment_token

        ledger_path = get_olano_paths(home).usage_ledger_path
        ledger_bytes = ledger_path.stat().st_size if ledger_path.exists() else 0
        cursor = _rep._read_cursor(home)
        out["cloud_reporting"] = {
            "enabled": _rep.reporting_enabled(),
            "token_present": deployment_token() is not None,
            "cloud_api_url": cloud_api_url(),
            "cursor_offset": cursor,
            "backlog_bytes": max(0, ledger_bytes - cursor),
        }
    except Exception as exc:  # pragma: no cover - defensive
        out["cloud_reporting_error"] = f"{type(exc).__name__}: {exc}"
    return out


def thread_cost_map(thread_ids: list[str], home: str | Path | None = None) -> dict[str, dict]:
    """Cached per-thread cost for a set of threads (for the chat thread list).

    Does NOT mine — returns only what's already cached so the chat rail stays fast;
    uncached threads are simply absent (UI shows "—").
    """
    with closing(cache.connect(home)) as cc:
        return cache.get_many(cc, thread_ids)
