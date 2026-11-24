"""Pure rollups over the usage ledger, session transcripts, and the A2A log.

Everything here is read-only and side-effect free so it's cheap to call from API
handlers and easy to unit-test. The three data sources:

- **Usage ledger** (``<home>/analytics/usage.jsonl``) → tokens / USD / call counts
  sliced by agent, by model, and by day (with a cumulative series).
- **Session transcripts** (``<home>/sessions/<agent>/<thread>.jsonl``) → tool-call
  counts per agent and per tool.
- **A2A activity** (``<home>/logs/a2a_activity.jsonl``) → agent↔agent message edges.

All windowing is by UTC calendar day. ``days=30`` means the last 30 days
inclusive of today.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from olano.analytics.a2a_log import read_a2a_events
from olano.analytics.usage_ledger import iter_usage_events
from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)

# Defensive caps so a runaway transcript history can't stall a request.
_MAX_SESSION_FILES = 2000
_MAX_TOP_TOOLS = 25


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _window_bounds(days: int) -> tuple[date, str, list[str]]:
    """Return ``(start_date, since_iso, all_days)`` for a ``days``-day window.

    ``since_iso`` is a ``YYYY-MM-DD`` string usable as a lexicographic lower
    bound against full ISO-8601 timestamps. ``all_days`` is every day in the
    window (oldest→newest) so time series can be zero-filled.
    """
    days = max(1, min(int(days or 30), 366))
    end = _today()
    start = end - timedelta(days=days - 1)
    all_days = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    return start, start.isoformat(), all_days


def _hour_window_bounds(hours: int) -> tuple[datetime, str, list[str]]:
    """Return ``(start_dt, since_iso, all_hours)`` for a rolling ``hours``-hour window.

    The newest bucket is the current (partial) UTC hour, so ``hours=24`` means
    "the last 24 hours". ``since_iso`` is a full ISO-8601 timestamp usable as a
    lexicographic lower bound against event timestamps (both are UTC).
    ``all_hours`` keys are ``YYYY-MM-DDTHH`` (oldest→newest) so series can be
    zero-filled; an event's bucket is ``ts[:13]``.
    """
    hours = max(1, min(int(hours or 24), 168))
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=hours - 1)
    all_hours = [(start + timedelta(hours=i)).isoformat()[:13] for i in range(hours)]
    return start, start.isoformat(), all_hours


def _round_usd(value: float) -> float:
    return round(float(value), 6)


def _emit_breakdown(key_name: str, mapping: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    """usd/token/call buckets → rows sorted by spend (shared by the usage rollups)."""
    rows: list[dict[str, Any]] = []
    for key, agg in mapping.items():
        rows.append(
            {
                key_name: key,
                "usd": _round_usd(agg["usd"]),
                "credits": _round_usd(agg.get("credits", 0.0)),
                "input_tokens": int(agg["input_tokens"]),
                "output_tokens": int(agg["output_tokens"]),
                "total_tokens": int(agg["input_tokens"] + agg["output_tokens"]),
                "calls": int(agg["calls"]),
            }
        )
    # Managed (olano) rows carry usd==0 by design, so sort by whichever number
    # the row actually has — otherwise every managed agent ties at $0 and the
    # breakdown order is meaningless in credits mode.
    rows.sort(key=lambda r: (float(r["usd"]), float(r["credits"])), reverse=True)
    return rows


def _accumulate(ev: Any, *buckets: dict[str, float]) -> None:
    """Fold one ledger event into usd/credits/token/call buckets.

    ``credits`` is the Olano gateway's authoritative charge (None on BYOK
    rows — folded as 0, the split lives in the by_source dimension).
    """
    credits = float(ev.credits or 0.0)
    for bucket in buckets:
        bucket["usd"] += ev.usd
        bucket["credits"] += credits
        bucket["input_tokens"] += ev.input_tokens
        bucket["output_tokens"] += ev.output_tokens
        # One row can fold several model sub-calls (see UsageEvent.calls); a
        # pre-`calls` row reads as the 1 it was.
        bucket["calls"] += ev.calls


def tier_of(model: str) -> str | None:
    """Bare olano tier for a ledger model string (``"olano:pro"`` → ``"pro"``),
    or None for anything that isn't a managed chat tier. Aliases normalize
    (``flash`` → ``turbo``); utility groups like ``olano:vision`` return None —
    they bill through the service ledger, not this one."""
    provider, _, rest = (model or "").partition(":")
    if provider.strip().lower() != "olano" or not rest:
        return None
    try:
        from olano.models.olano_gateway import normalize_tier

        return normalize_tier(rest)
    except Exception:  # pragma: no cover - defensive
        return None


# ───────────────────────────────────── usage ─────────────────────────────────


def usage_rollup(days: int = 30, *, agents: set[str] | None = None) -> dict[str, Any]:
    """Tokens / USD / calls sliced by agent, model, and day.

    ``agents`` restricts the rollup to events attributed to those agents
    (multi-user member scoping — totals, models, and the daily series are all
    computed from the restricted event set, so nothing cross-agent leaks).
    """
    _, since, all_days = _window_bounds(days)

    def _blank() -> dict[str, float]:
        return {"usd": 0.0, "credits": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0}

    by_agent: dict[str, dict[str, float]] = defaultdict(_blank)
    by_model: dict[str, dict[str, float]] = defaultdict(_blank)
    by_source: dict[str, dict[str, float]] = defaultdict(_blank)
    by_origin: dict[str, dict[str, float]] = defaultdict(_blank)
    by_tier: dict[str, dict[str, float]] = defaultdict(_blank)
    by_day: dict[str, dict[str, float]] = {d: _blank() for d in all_days}
    totals = _blank()

    for ev in iter_usage_events(since=since):
        if agents is not None and ev.agent not in agents:
            continue
        _accumulate(
            ev,
            by_agent[ev.agent],
            by_model[ev.model],
            by_source[ev.billing_source],
            # Pre-origin rows land in "turns" (they were all runner turns).
            by_origin[ev.origin or "turns"],
            totals,
        )
        # Managed tiers only ("olano:pro" → "pro") — the turbo/pro/max split.
        tier = tier_of(ev.model)
        if tier is not None:
            _accumulate(ev, by_tier[tier])
        # The ledger may legitimately hold a stray event dated before `since`
        # only if clocks skew; guard the day bucket lookup either way.
        day_bucket = by_day.get(ev.date)
        if day_bucket is not None:
            _accumulate(ev, day_bucket)

    # Daily series, zero-filled and ordered, with running cumulatives.
    daily: list[dict[str, Any]] = []
    cum_usd = 0.0
    cum_credits = 0.0
    cum_tokens = 0
    for d in all_days:
        agg = by_day[d]
        tokens = int(agg["input_tokens"] + agg["output_tokens"])
        cum_usd += agg["usd"]
        cum_credits += agg["credits"]
        cum_tokens += tokens
        daily.append(
            {
                "date": d,
                "usd": _round_usd(agg["usd"]),
                "credits": _round_usd(agg["credits"]),
                "input_tokens": int(agg["input_tokens"]),
                "output_tokens": int(agg["output_tokens"]),
                "total_tokens": tokens,
                "calls": int(agg["calls"]),
                "cum_usd": _round_usd(cum_usd),
                "cum_credits": _round_usd(cum_credits),
                "cum_tokens": cum_tokens,
            }
        )

    return {
        "totals": {
            "usd": _round_usd(totals["usd"]),
            "credits": _round_usd(totals["credits"]),
            "input_tokens": int(totals["input_tokens"]),
            "output_tokens": int(totals["output_tokens"]),
            "total_tokens": int(totals["input_tokens"] + totals["output_tokens"]),
            "calls": int(totals["calls"]),
            "agents": len(by_agent),
            "models": len(by_model),
        },
        "by_agent": _emit_breakdown("agent", by_agent),
        "by_model": _emit_breakdown("model", by_model),
        # "olano" (Olano-managed, charged in credits) vs "customer" (BYOK,
        # provider-billed USD). The two are different accounting systems — the
        # UI must never sum a credits number into a USD number, which is why
        # they ship as separate rows rather than a merged total.
        "by_source": _emit_breakdown("source", by_source),
        # What kind of work spent it (interactive / scheduled / cognition /
        # highlights / …) — the "where did my credits go" dimension.
        "by_origin": _emit_breakdown("origin", by_origin),
        # Managed chat tiers only — the "credits by turbo/pro/max" split.
        "by_tier": _emit_breakdown("tier", by_tier),
        "daily": daily,
        "source": "usage_ledger",
    }


def usage_rollup_hourly(hours: int = 24, *, agents: set[str] | None = None) -> dict[str, Any]:
    """Tokens / USD / calls over a rolling last-``hours`` window, bucketed by UTC hour.

    Sourced from the per-call usage ledger — the only store with a real timestamp
    on every call (checkpoint mining buckets whole threads to a day, and stored
    ``response_metadata`` rarely carries a creation time). Shape mirrors
    :func:`usage_rollup` with an ``hourly`` series instead of ``daily`` (``daily``
    is kept as an empty list so consumers of the common shape never break).
    ``agents`` restricts to those agents' events (member scoping).
    """
    _, since, all_hours = _hour_window_bounds(hours)

    def _blank() -> dict[str, float]:
        return {"usd": 0.0, "credits": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0}

    by_agent: dict[str, dict[str, float]] = defaultdict(_blank)
    by_model: dict[str, dict[str, float]] = defaultdict(_blank)
    by_source: dict[str, dict[str, float]] = defaultdict(_blank)
    by_origin: dict[str, dict[str, float]] = defaultdict(_blank)
    by_tier: dict[str, dict[str, float]] = defaultdict(_blank)
    by_hour: dict[str, dict[str, float]] = {h: _blank() for h in all_hours}
    totals = _blank()

    for ev in iter_usage_events(since=since):
        if agents is not None and ev.agent not in agents:
            continue
        _accumulate(
            ev,
            by_agent[ev.agent],
            by_model[ev.model],
            by_source[ev.billing_source],
            by_origin[ev.origin or "turns"],
            totals,
        )
        tier = tier_of(ev.model)
        if tier is not None:
            _accumulate(ev, by_tier[tier])
        # ts >= since ⇒ the hour bucket exists, but guard clock skew anyway.
        hour_bucket = by_hour.get(ev.ts[:13])
        if hour_bucket is not None:
            _accumulate(ev, hour_bucket)

    hourly: list[dict[str, Any]] = []
    cum_usd = 0.0
    cum_credits = 0.0
    cum_tokens = 0
    for h in all_hours:
        agg = by_hour[h]
        tokens = int(agg["input_tokens"] + agg["output_tokens"])
        cum_usd += agg["usd"]
        cum_credits += agg["credits"]
        cum_tokens += tokens
        hourly.append(
            {
                "hour": h,
                "usd": _round_usd(agg["usd"]),
                "credits": _round_usd(agg["credits"]),
                "input_tokens": int(agg["input_tokens"]),
                "output_tokens": int(agg["output_tokens"]),
                "total_tokens": tokens,
                "calls": int(agg["calls"]),
                "cum_usd": _round_usd(cum_usd),
                "cum_credits": _round_usd(cum_credits),
                "cum_tokens": cum_tokens,
            }
        )

    return {
        "totals": {
            "usd": _round_usd(totals["usd"]),
            "credits": _round_usd(totals["credits"]),
            "input_tokens": int(totals["input_tokens"]),
            "output_tokens": int(totals["output_tokens"]),
            "total_tokens": int(totals["input_tokens"] + totals["output_tokens"]),
            "calls": int(totals["calls"]),
            "agents": len(by_agent),
            "models": len(by_model),
        },
        "by_agent": _emit_breakdown("agent", by_agent),
        "by_model": _emit_breakdown("model", by_model),
        "by_source": _emit_breakdown("source", by_source),
        "by_origin": _emit_breakdown("origin", by_origin),
        "by_tier": _emit_breakdown("tier", by_tier),
        "hourly": hourly,
        "daily": [],
        "source": "usage_ledger",
    }


# ───────────────────────────────────── tools ─────────────────────────────────


def _iter_session_files(sessions_dir: Path, start: date | datetime) -> list[Path]:
    """Transcript files plausibly touched on/after ``start`` (mtime pre-filter)."""
    if not sessions_dir.exists():
        return []
    if isinstance(start, datetime):  # datetime is a date subclass — check it first
        cutoff = start.timestamp()
    else:
        cutoff = datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()
    files: list[Path] = []
    for agent_dir in sorted(sessions_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        for f in agent_dir.glob("*.jsonl"):
            try:
                if f.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            files.append(f)
            if len(files) >= _MAX_SESSION_FILES:
                logger.debug("tool_rollup: hit session-file cap (%d)", _MAX_SESSION_FILES)
                return files
    return files


# The deepagents dispatch tool used to delegate to a subagent. Its calls are
# tracked separately (keyed by subagent_type), not lumped in with real tools.
_SUBAGENT_TOOL = "task"
# Recover a tool name from a legacy "<name>: <args>" content blob (older entries
# stored no structured `name`, so they all collapsed to a generic "tool").
_TOOL_NAME_RE = re.compile(r"^([\w.\-]{1,40}):\s")
# subagent_type sits at the front of the task args JSON, so a regex survives even
# when the args/content blob is truncated.
_SUBAGENT_RE = re.compile(r'"subagent_type"\s*:\s*"([^"]+)"')
_SUBAGENT_RE2 = re.compile(r'"subagent"\s*:\s*"([^"]+)"')


def tool_name_of(entry: dict[str, Any]) -> str:
    """Best-effort tool name for a tool_call entry (structured field → legacy
    content prefix → "(unnamed)")."""
    name = str(entry.get("name") or "").strip()
    if name:
        return name
    m = _TOOL_NAME_RE.match(str(entry.get("content") or ""))
    return m.group(1) if m else "(unnamed)"


def is_subagent_call(entry: dict[str, Any]) -> bool:
    """True if the entry is a ``task`` dispatch to a subagent."""
    return tool_name_of(entry) == _SUBAGENT_TOOL or str(entry.get("message_id") or "").startswith(
        "task::"
    )


def subagent_name_of(entry: dict[str, Any]) -> str:
    """subagent_type for a ``task`` entry, parsed from its args (robust to
    truncation). "unknown" if absent."""
    raw = str(entry.get("args") or entry.get("content") or "")
    m = _SUBAGENT_RE.search(raw) or _SUBAGENT_RE2.search(raw)
    return m.group(1) if m else "unknown"


def tool_rollup(
    days: int = 30, *, hours: int | None = None, agents: set[str] | None = None
) -> dict[str, Any]:
    """Tool-call and subagent-dispatch counts from session transcripts.

    The deepagents ``task`` tool (subagent dispatch) is split into its own
    ``by_subagent`` breakdown keyed by ``subagent_type``; "Top tools" excludes
    it. Real tool names are recovered from legacy content where the structured
    name is missing. ``hours`` switches the window from trailing calendar days
    to a rolling last-``hours`` one (entry timestamps are full ISO, so the
    finer bound is just a longer lexicographic ``since``). ``agents``
    restricts to those agents' transcripts (member scoping).
    """
    start: date | datetime
    if hours is not None:
        start, since, _ = _hour_window_bounds(hours)
    else:
        start, since, _ = _window_bounds(days)
    sessions_dir = get_olano_paths().sessions_dir

    per_agent: dict[str, int] = defaultdict(int)  # real (non-task) tool calls
    per_tool: dict[str, int] = defaultdict(int)
    per_agent_tool: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    per_subagent: dict[str, int] = defaultdict(int)
    tool_calls = 0
    subagent_calls = 0
    truncated = False

    files = _iter_session_files(sessions_dir, start)
    if len(files) >= _MAX_SESSION_FILES:
        truncated = True

    for f in files:
        agent = f.parent.name
        if agents is not None and agent not in agents:
            continue
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or '"tool_call"' not in line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or entry.get("role") != "tool_call":
                continue
            ts = str(entry.get("ts") or "")
            if ts and ts < since:
                continue
            if is_subagent_call(entry):
                per_subagent[subagent_name_of(entry)] += 1
                subagent_calls += 1
                continue
            tool = tool_name_of(entry)
            per_agent[agent] += 1
            per_tool[tool] += 1
            per_agent_tool[agent][tool] += 1
            tool_calls += 1

    by_agent = [
        {"agent": a, "tool_calls": c}
        for a, c in sorted(per_agent.items(), key=lambda kv: kv[1], reverse=True)
    ]
    by_tool = [
        {"tool": t, "count": c}
        for t, c in sorted(per_tool.items(), key=lambda kv: kv[1], reverse=True)
    ][:_MAX_TOP_TOOLS]
    by_subagent = [
        {"subagent": s, "count": c}
        for s, c in sorted(per_subagent.items(), key=lambda kv: kv[1], reverse=True)
    ][:_MAX_TOP_TOOLS]
    by_agent_tool: list[dict[str, Any]] = []
    for a, tools in per_agent_tool.items():
        ranked = sorted(tools.items(), key=lambda kv: kv[1], reverse=True)
        by_agent_tool.append(
            {
                "agent": a,
                "tool_calls": per_agent[a],
                "tools": [{"tool": t, "count": c} for t, c in ranked[:_MAX_TOP_TOOLS]],
            }
        )
    by_agent_tool.sort(key=lambda r: int(r["tool_calls"]), reverse=True)

    return {
        "totals": {
            "tool_calls": tool_calls,
            "subagent_calls": subagent_calls,
            "distinct_tools": len(per_tool),
            "distinct_subagents": len(per_subagent),
            "agents": len(per_agent),
            "truncated": truncated,
        },
        "by_agent": by_agent,
        "by_tool": by_tool,
        "by_subagent": by_subagent,
        "by_agent_tool": by_agent_tool,
    }


# ──────────────────────────────────── services ───────────────────────────────


def service_rollup(
    days: int = 30, *, hours: int | None = None, agents: set[str] | None = None
) -> dict[str, Any]:
    """Non-model service calls (image / TTS / STT / web) from the service ledger.

    The credits-side companion to :func:`usage_rollup`: these calls spend olano
    credits without producing a token, so nothing in the LLM ledger or the
    checkpoint store knows they happened. ``credits`` is only ever the charge
    the cloud reported per call — gateway-metered services report none, so a
    row can legitimately be counted without being priced (``priced_calls``
    says how much of the count carries a charge).

    ``hours`` switches to a rolling last-``hours`` window; ``agents`` restricts
    to those agents' calls (member scoping).
    """
    from olano.analytics.service_ledger import iter_service_events

    if hours is not None:
        _, since, all_keys = _hour_window_bounds(hours)
        key_name = "hour"
    else:
        _, since, all_keys = _window_bounds(days)
        key_name = "date"

    def _blank() -> dict[str, float]:
        return {"calls": 0, "credits": 0.0, "priced_calls": 0}

    by_service: dict[str, dict[str, float]] = defaultdict(_blank)
    by_agent: dict[str, dict[str, float]] = defaultdict(_blank)
    by_source: dict[str, dict[str, float]] = defaultdict(_blank)
    by_bucket: dict[str, dict[str, float]] = {k: _blank() for k in all_keys}
    totals = _blank()

    for ev in iter_service_events(since=since):
        if agents is not None and ev.agent not in agents:
            continue
        bucket = by_bucket.get(ev.ts[:13] if hours is not None else ev.date)
        targets = [by_service[ev.service], by_agent[ev.agent], by_source[ev.billing_source], totals]
        if bucket is not None:
            targets.append(bucket)
        for target in targets:
            target["calls"] += ev.units
            if ev.credits is not None:
                target["credits"] += ev.credits
                target["priced_calls"] += ev.units

    def _rows(name: str, mapping: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
        ranked = sorted(
            mapping.items(), key=lambda kv: (kv[1]["calls"], kv[1]["credits"]), reverse=True
        )
        return [
            {
                name: key,
                "calls": int(agg["calls"]),
                "credits": _round_usd(agg["credits"]),
                "priced_calls": int(agg["priced_calls"]),
            }
            for key, agg in ranked
        ]

    series = [
        {
            key_name: k,
            "calls": int(by_bucket[k]["calls"]),
            "credits": _round_usd(by_bucket[k]["credits"]),
        }
        for k in all_keys
    ]

    return {
        "totals": {
            "calls": int(totals["calls"]),
            "credits": _round_usd(totals["credits"]),
            "priced_calls": int(totals["priced_calls"]),
            "services": len(by_service),
            "agents": len(by_agent),
        },
        "by_service": _rows("service", by_service),
        "by_agent": _rows("agent", by_agent),
        "by_source": _rows("source", by_source),
        # Same naming contract as the usage rollups: `daily` for a calendar
        # window, `hourly` for the rolling one, with the other left empty so a
        # consumer of the common shape never trips over a missing key.
        "daily": [] if hours is not None else series,
        "hourly": series if hours is not None else [],
    }


# ────────────────────────────────────── a2a ──────────────────────────────────


def _read_a2a_events(limit: int = 5000) -> list[dict[str, Any]]:
    """Thin alias kept for the existing call sites / test patching points."""
    return read_a2a_events(limit)


def a2a_rollup(
    days: int = 30,
    *,
    recent_limit: int = 40,
    hours: int | None = None,
    agents: set[str] | None = None,
) -> dict[str, Any]:
    """Agent↔agent message edges from the A2A activity log.

    Counts one message per ``phase == "send"`` event (a ``reply`` echoes the same
    pair, so counting both would double everything). Events without an explicit
    phase are treated as a single message. ``hours`` switches to a rolling
    last-``hours`` window (full-ISO ``since`` bound). ``agents`` (member
    scoping) keeps an exchange when at least one participant is in the set —
    exchanges strictly between non-granted agents are dropped.
    """
    if hours is not None:
        _, since, _ = _hour_window_bounds(hours)
    else:
        _, since, _ = _window_bounds(days)

    edges: dict[tuple[str, str], int] = defaultdict(int)
    sent: dict[str, int] = defaultdict(int)
    received: dict[str, int] = defaultdict(int)
    total = 0
    recent: list[dict[str, Any]] = []

    for ev in _read_a2a_events():
        if str(ev.get("type") or "") != "agent_message":
            continue
        ts = str(ev.get("ts") or "")
        if ts and ts < since:
            continue
        raw_data = ev.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        phase = str(data.get("phase") or "send")
        if phase not in ("send", ""):
            continue
        src = str(data.get("from") or "unknown")
        dst = str(data.get("to") or "unknown")
        if agents is not None and src not in agents and dst not in agents:
            continue
        edges[(src, dst)] += 1
        sent[src] += 1
        received[dst] += 1
        total += 1
        recent.append(
            {
                "from": src,
                "to": dst,
                "kind": str(data.get("kind") or "peer"),
                "message": str(data.get("message") or "")[:280],
                "ts": ts,
            }
        )

    edge_rows = [
        {"from": s, "to": d, "count": c}
        for (s, d), c in sorted(edges.items(), key=lambda kv: kv[1], reverse=True)
    ]
    seen_agents = sorted(set(sent) | set(received))
    by_agent: list[dict[str, Any]] = [
        {"agent": a, "sent": sent.get(a, 0), "received": received.get(a, 0)} for a in seen_agents
    ]
    by_agent.sort(key=lambda r: int(r["sent"]) + int(r["received"]), reverse=True)

    return {
        "totals": {"messages": total, "agents": len(seen_agents), "edges": len(edge_rows)},
        "edges": edge_rows,
        "by_agent": by_agent,
        "recent": recent[-recent_limit:][::-1],
    }


# ──────────────────────────────────── overview ───────────────────────────────


def overview(
    days: int = 30, *, hours: int | None = None, agents: set[str] | None = None
) -> dict[str, Any]:
    """Everything the Insights view needs in a single payload.

    ``hours`` switches to a rolling last-``hours`` window with hourly resolution
    (``window_hours`` set, ``since`` a full ISO timestamp, ``usage.hourly`` the
    series); otherwise the window is trailing UTC calendar days. ``agents``
    scopes every rollup to those agents (multi-user member view).
    """
    if hours is not None:
        hours = max(1, min(int(hours or 24), 168))
        _, since, _ = _hour_window_bounds(hours)
        return {
            "window_days": max(1, -(-hours // 24)),
            "window_hours": hours,
            "since": since,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "usage": usage_rollup_hourly(hours, agents=agents),
            "tools": tool_rollup(hours=hours, agents=agents),
            "services": service_rollup(hours=hours, agents=agents),
            "a2a": a2a_rollup(hours=hours, agents=agents),
        }
    days = max(1, min(int(days or 30), 366))
    _, since, _ = _window_bounds(days)
    usage = usage_rollup(days, agents=agents)
    tools = tool_rollup(days, agents=agents)
    a2a = a2a_rollup(days, agents=agents)
    return {
        "window_days": days,
        "since": since,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "usage": usage,
        "tools": tools,
        "services": service_rollup(days, agents=agents),
        "a2a": a2a,
    }
