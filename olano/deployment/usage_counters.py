"""Durable usage counters — the enforcement backing for token/cost budgets.

Why not the JSONL analytics ledger? Budget *enforcement* needs transactional,
interprocess-safe counters: two concurrent turns must not both observe
"remaining budget" and both proceed past it, and log rotation must never
erase spend history. This module keeps integer counters (tokens; cost in
**micro-USD** — no floating-point currency) in a small SQLite database at
``<home>/deployment/usage_counters.db`` (WAL + busy_timeout, the same
discipline as ``core/checkpoints.py``). The analytics ledger remains the
*reporting* source of truth; on first managed use the counters are seeded
from it so history predating this module still counts.

Enforcement contract (from the deployment-settings design record):

- Budgets are enforced at **turn boundaries**. A turn is refused when a
  budget is already exhausted; a running call may overshoot by at most the
  in-flight amount.
- ``try_reserve`` atomically checks every limit and books a provisional
  estimate in the same transaction, so N concurrent turns cannot jointly
  slip past a nearly-exhausted budget.
- ``reconcile`` replaces the estimate with actuals (in a ``finally``-safe
  way: reconciling twice or for an unknown reservation is a no-op).
- Reservations abandoned by a crashed/errored turn are auto-released after
  ``STALE_RESERVATION_SECONDS`` — usage that never reached the recording
  path was never counted by the legacy tracker either, so releasing matches
  historical behavior while staying conservative in the window where the
  call might still be running.
- Unknown model cost ⇒ tokens still count; cost is reconciled with whatever
  the shared cost helper reports (0 when the model is unknown).
- The **open** posture performs no reservation and no writes — unmanaged
  installs keep today's behavior and pay no per-turn cost.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from olano.deployment import resolver
from olano.deployment.enforcement import ALLOW, Decision

if TYPE_CHECKING:
    from olano.config.home import OlanoHomePaths
    from olano.deployment.schema import UsageLimit

logger = logging.getLogger(__name__)

DB_FILE = "usage_counters.db"
STALE_RESERVATION_SECONDS = 3600
BUSY_TIMEOUT_MS = 30_000

# Default per-call reservation estimate (tokens). Deliberately modest: large
# enough that a stampede of concurrent turns is throttled near the cap, small
# enough not to starve legitimate parallelism. Overridable for testing/tuning.
DEFAULT_ESTIMATE_INPUT = int(os.environ.get("LIONCLAW_BUDGET_RESERVE_INPUT", "10000"))
DEFAULT_ESTIMATE_OUTPUT = int(os.environ.get("LIONCLAW_BUDGET_RESERVE_OUTPUT", "2000"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_usage (
    agent           TEXT NOT NULL,
    day             TEXT NOT NULL,      -- YYYY-MM-DD (UTC)
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_micro_usd  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (agent, day)
);
CREATE TABLE IF NOT EXISTS reservations (
    id              TEXT PRIMARY KEY,
    agent           TEXT NOT NULL,
    day             TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    cost_micro_usd  INTEGER NOT NULL,
    created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_alerts (
    limit_key    TEXT NOT NULL,
    scope        TEXT NOT NULL,      -- agent name, or "" for deployment-wide
    window_start TEXT NOT NULL,      -- "" for the lifetime window
    percent      INTEGER NOT NULL,
    fired_at     INTEGER NOT NULL,
    PRIMARY KEY (limit_key, scope, window_start, percent)
);
"""


def _db_path(paths: OlanoHomePaths | None = None):
    if paths is None:
        from olano.config.home import get_olano_paths

        paths = get_olano_paths()
    d = paths.deployment_dir
    d.mkdir(parents=True, exist_ok=True)
    return d / DB_FILE


def _connect(paths: OlanoHomePaths | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(_db_path(paths)), timeout=BUSY_TIMEOUT_MS / 1000)
    # Autocommit mode: transactions are managed explicitly (BEGIN IMMEDIATE)
    # so check + book happen atomically across processes; `with conn:` still
    # commits/rolls back any open transaction on exit.
    conn.isolation_level = None
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    return conn


def _day(now: int) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


def window_start_day(window: str, now: int) -> str | None:
    """First UTC day (inclusive) of the window containing *now*; None = lifetime."""
    dt = datetime.fromtimestamp(now, tz=timezone.utc)
    if window == "daily":
        return dt.strftime("%Y-%m-%d")
    if window == "weekly":
        monday = dt - timedelta(days=dt.isoweekday() - 1)
        return monday.strftime("%Y-%m-%d")
    if window == "monthly":
        return dt.strftime("%Y-%m-01")
    return None  # lifetime


def _totals(
    conn: sqlite3.Connection, agent: str | None, window: str, now: int
) -> tuple[int, int, int]:
    start = window_start_day(window, now)
    clauses, params = [], []
    if start is not None:
        clauses.append("day >= ?")
        params.append(start)
    if agent is not None:
        clauses.append("agent = ?")
        params.append(agent)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),"
        f" COALESCE(SUM(cost_micro_usd),0) FROM daily_usage {where}",
        params,
    ).fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def _release_stale_reservations(conn: sqlite3.Connection, now: int) -> None:
    cutoff = now - STALE_RESERVATION_SECONDS
    rows = conn.execute(
        "SELECT id, agent, day, input_tokens, output_tokens, cost_micro_usd"
        " FROM reservations WHERE created_at < ?",
        (cutoff,),
    ).fetchall()
    for rid, agent, day, inp, out, cost in rows:
        conn.execute(
            "UPDATE daily_usage SET input_tokens = MAX(0, input_tokens - ?),"
            " output_tokens = MAX(0, output_tokens - ?),"
            " cost_micro_usd = MAX(0, cost_micro_usd - ?) WHERE agent = ? AND day = ?",
            (inp, out, cost, agent, day),
        )
        conn.execute("DELETE FROM reservations WHERE id = ?", (rid,))
    if rows:
        logger.info("released %d stale budget reservation(s)", len(rows))


def _add_usage(
    conn: sqlite3.Connection,
    agent: str,
    day: str,
    input_tokens: int,
    output_tokens: int,
    cost_micro_usd: int,
) -> None:
    conn.execute(
        "INSERT INTO daily_usage (agent, day, input_tokens, output_tokens, cost_micro_usd)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(agent, day) DO UPDATE SET"
        " input_tokens = input_tokens + excluded.input_tokens,"
        " output_tokens = output_tokens + excluded.output_tokens,"
        " cost_micro_usd = cost_micro_usd + excluded.cost_micro_usd",
        (agent, day, input_tokens, output_tokens, cost_micro_usd),
    )


def _budget_checks(agent: str) -> list[tuple[str, str, UsageLimit, str | None, int]]:
    """(limit key, human label, limit, scope-agent, metric index) per budget.

    The limits come from :func:`usage_budgets.effective_usage_limits`, not
    straight off the artifact, so the ceiling enforced here is always the one
    the dashboard and the CLI display.
    """
    from olano.deployment.usage_budgets import effective_usage_limits

    usage = resolver.get_effective_settings().usage
    return [
        (eff.key, eff.label, eff.limit, agent if eff.per_agent else None, eff.metric)
        for eff in effective_usage_limits(usage)
    ]


def _remediation_for(key: str, source: str) -> str:
    """What to actually do about a spent budget — which differs by budget.

    A token ceiling is a runaway-loop guard the operator owns, so the useful
    advice names the knob. A cost ceiling bounds olano's own upstream bill and
    is not settable from the box, so there the plan is the lever.
    """
    if key.startswith("cost"):
        return "The budget resets at the next window, or upgrade your olano plan to raise it."
    where = "config.yaml" if source == "config" else "Config → Usage budgets"
    return (
        f"The budget resets at the next window. To raise or lift it now, set "
        f"`usage_budgets.{key}` in {where} (a number, or `unlimited`)."
    )


def _evaluate_budgets(conn: sqlite3.Connection, agent: str, now: int) -> Decision:
    from olano.deployment.usage_budgets import effective_usage_limits

    usage = resolver.get_effective_settings().usage
    for eff in effective_usage_limits(usage):
        limit = eff.limit
        if limit.value is None:
            continue
        totals = _totals(conn, agent if eff.per_agent else None, limit.window, now)
        if totals[eff.metric] >= limit.value:
            unit = "µ$" if eff.metric == 2 else "tokens"
            return Decision(
                allowed=False,
                code=f"deployment_budget_{eff.key}",
                message=(
                    f"The {limit.window} budget for {eff.label} is exhausted "
                    f"({totals[eff.metric]:,}/{limit.value:,} {unit})."
                ),
                remediation=_remediation_for(eff.key, eff.source),
                limit=limit.value,
                current=totals[eff.metric],
            )
    return ALLOW


@dataclass(frozen=True)
class BudgetCrossing:
    """One budget passing a threshold it had not passed before, this window."""

    key: str
    label: str
    scope: str  # the agent for a per-agent budget, "" for deployment-wide
    percent: int
    current: int
    limit: int
    window: str
    unit: str
    source: str


def _record_crossings(conn: sqlite3.Connection, agent: str, now: int) -> list[BudgetCrossing]:
    """Claim every alert threshold this write pushed a budget past.

    Runs inside the caller's transaction and INSERTs the claim, so two
    concurrent turns crossing 90% together raise one notification, not two.
    Returns the crossings for the caller to announce *after* the transaction —
    a notification must never hold a write lock.
    """
    from olano.config.system_settings import load_usage_budget_settings
    from olano.deployment.usage_budgets import effective_usage_limits

    settings = load_usage_budget_settings()
    if not settings.alerts_enabled:
        return []
    # 100 rides along unconditionally whenever alerts are on: a spent budget
    # refuses turns, and an operator who asked for warnings at 50% certainly
    # wants the row that says the thing actually stopped.
    marks = sorted({*settings.alert_percents, 100})
    crossings: list[BudgetCrossing] = []
    usage = resolver.get_effective_settings().usage
    for eff in effective_usage_limits(usage):
        limit = eff.limit
        if not limit.value:  # unlimited, or a zero ceiling nothing can fill
            continue
        scope = agent if eff.per_agent else ""
        totals = _totals(conn, agent if eff.per_agent else None, limit.window, now)
        current = totals[eff.metric]
        pct = current * 100 // limit.value
        if pct < marks[0]:
            continue
        window_start = window_start_day(limit.window, now) or ""
        for mark in marks:
            if pct < mark:
                break
            row = conn.execute(
                "INSERT OR IGNORE INTO budget_alerts"
                " (limit_key, scope, window_start, percent, fired_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (eff.key, scope, window_start, mark, now),
            )
            if row.rowcount:
                crossings.append(
                    BudgetCrossing(
                        key=eff.key,
                        label=eff.label,
                        scope=scope,
                        percent=mark,
                        current=current,
                        limit=limit.value,
                        window=limit.window,
                        unit=eff.unit,
                        source=eff.source,
                    )
                )
    # Only the highest mark per budget is worth a row — a box that jumps from
    # 40% to 96% in one turn should say 95%, not five things at once.
    highest: dict[tuple[str, str], BudgetCrossing] = {}
    for crossing in crossings:
        seen = highest.get((crossing.key, crossing.scope))
        if seen is None or crossing.percent > seen.percent:
            highest[(crossing.key, crossing.scope)] = crossing
    return sorted(highest.values(), key=lambda c: c.percent, reverse=True)


def _fmt_amount(value: int, unit: str) -> str:
    return f"${value / 1_000_000:,.2f}" if unit == "micro_usd" else f"{value:,}"


def _announce(crossings: list[BudgetCrossing]) -> None:
    """Raise one dashboard notification per crossing. Never raises.

    Deduped per (budget, scope, window) so the row updates in place as a
    budget climbs rather than stacking one per threshold, and levelled so 100%
    reads as the outage it is.
    """
    if not crossings:
        return
    try:
        from olano.runtime import notifications
    except Exception:  # pragma: no cover - CLI/test processes without the API
        return
    for c in crossings:
        try:
            spent = f"{_fmt_amount(c.current, c.unit)} of {_fmt_amount(c.limit, c.unit)}"
            scope = f" for agent '{c.scope}'" if c.scope else ""
            if c.percent >= 100:
                title = f"{c.window.capitalize()} budget exhausted: {c.label}"
                body = (
                    f"{spent} used{scope}. Turns on this budget are being refused until "
                    f"the window resets. {_remediation_for(c.key, c.source)}"
                )
            else:
                title = f"{c.window.capitalize()} budget {c.percent}% used: {c.label}"
                body = (
                    f"{spent} used{scope}. {_remediation_for(c.key, c.source)}"
                    if c.percent >= 90
                    else f"{spent} used{scope}."
                )
            notifications.notify(
                kind="usage_budget",
                level="error" if c.percent >= 100 else ("warn" if c.percent >= 90 else "info"),
                title=title,
                body=body,
                agent=c.scope or None,
                dedupe_key=f"usage_budget:{c.key}:{c.scope}:{c.window}",
                action={"label": "Open Config", "href": "/config"},
            )
        except Exception:  # pragma: no cover - a notification never breaks a turn
            logger.debug("budget alert failed", exc_info=True)


def try_reserve(
    agent: str,
    *,
    now: int | None = None,
    estimate_input: int | None = None,
    estimate_output: int | None = None,
) -> tuple[str | None, Decision]:
    """Atomically check every budget and book a provisional estimate.

    Returns ``(reservation_id, ALLOW)`` on success, ``(None, ALLOW)`` for the
    open posture (no write), or ``(None, denial)`` when a budget is exhausted.
    Failure to *evaluate* rejects (managed never fails open).
    """
    status = resolver.deployment_status()
    if status.posture == "open":
        return None, ALLOW
    ts = now if now is not None else int(time.time())
    est_in = DEFAULT_ESTIMATE_INPUT if estimate_input is None else estimate_input
    est_out = DEFAULT_ESTIMATE_OUTPUT if estimate_output is None else estimate_output
    try:
        with contextlib.closing(_connect()) as conn:
            _seed_from_ledger_locked(conn)
            with conn:  # one transaction: cleanup + check + book
                conn.execute("BEGIN IMMEDIATE")
                _release_stale_reservations(conn, ts)
                decision = _evaluate_budgets(conn, agent, ts)
                if not decision.allowed:
                    return None, decision
                rid = uuid.uuid4().hex
                day = _day(ts)
                _add_usage(conn, agent, day, est_in, est_out, 0)
                conn.execute(
                    "INSERT INTO reservations (id, agent, day, input_tokens, output_tokens,"
                    " cost_micro_usd, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (rid, agent, day, est_in, est_out, 0, ts),
                )
                return rid, ALLOW
    except Exception:
        logger.exception("budget reservation failed")
        return None, Decision(
            allowed=False,
            code="policy_unavailable",
            message="Could not evaluate usage budgets; the turn was rejected. Retry shortly.",
            remediation="Retry; if this persists, check the runtime logs.",
        )


def reconcile(
    reservation_id: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cost_micro_usd: int,
) -> None:
    """Replace a reservation's estimate with actuals. Idempotent; never raises."""
    crossings: list[BudgetCrossing] = []
    try:
        with contextlib.closing(_connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT agent, day, input_tokens, output_tokens, cost_micro_usd"
                " FROM reservations WHERE id = ?",
                (reservation_id,),
            ).fetchone()
            if row is None:
                return  # already reconciled / released
            agent, day, est_in, est_out, est_cost = row
            _add_usage(
                conn,
                agent,
                day,
                input_tokens - est_in,
                output_tokens - est_out,
                cost_micro_usd - est_cost,
            )
            # Clamp any negative drift from the estimate delta.
            conn.execute(
                "UPDATE daily_usage SET input_tokens = MAX(0, input_tokens),"
                " output_tokens = MAX(0, output_tokens),"
                " cost_micro_usd = MAX(0, cost_micro_usd) WHERE agent = ? AND day = ?",
                (agent, day),
            )
            conn.execute("DELETE FROM reservations WHERE id = ?", (reservation_id,))
            crossings = _record_crossings(conn, agent, int(time.time()))
    except Exception:
        logger.exception("budget reconcile failed")
    # Outside the transaction: a notification must never hold the write lock.
    _announce(crossings)


def record(
    agent: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cost_micro_usd: int,
    now: int | None = None,
) -> None:
    """Unconditionally add actual usage (no reservation). Never raises."""
    status = resolver.deployment_status()
    if status.posture == "open":
        return
    ts = now if now is not None else int(time.time())
    crossings: list[BudgetCrossing] = []
    try:
        with contextlib.closing(_connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            _seed_from_ledger_locked(conn)
            _add_usage(conn, agent, _day(ts), input_tokens, output_tokens, cost_micro_usd)
            crossings = _record_crossings(conn, agent, ts)
    except Exception:
        logger.exception("budget record failed")
    _announce(crossings)


def check_budget(agent: str, *, now: int | None = None) -> Decision:
    """Read-only budget evaluation (post-call hard-stop). Rejects on failure."""
    status = resolver.deployment_status()
    if status.posture == "open":
        return ALLOW
    ts = now if now is not None else int(time.time())
    try:
        with contextlib.closing(_connect()) as conn:
            _seed_from_ledger_locked(conn)
            return _evaluate_budgets(conn, agent, ts)
    except Exception:
        logger.exception("budget check failed")
        return Decision(
            allowed=False,
            code="policy_unavailable",
            message="Could not evaluate usage budgets; the turn was rejected. Retry shortly.",
            remediation="Retry; if this persists, check the runtime logs.",
        )


def current_usage_for_limit(key: str, *, now: int | None = None) -> int | None:
    """Current spend for one budget key (admin display). None when unknown.

    Reads the window off the EFFECTIVE limit, so a locally overridden budget
    is measured over the window it is actually enforced on.
    """
    from olano.deployment.usage_budgets import effective_usage_limits

    ts = now if now is not None else int(time.time())
    settings = resolver.get_effective_settings()
    eff = next((e for e in effective_usage_limits(settings.usage) if e.key == key), None)
    if eff is None:
        return None
    limit, metric, per_agent = eff.limit, eff.metric, eff.per_agent
    try:
        with contextlib.closing(_connect()) as conn:
            _seed_from_ledger_locked(conn)
            if not per_agent:
                return _totals(conn, None, limit.window, ts)[metric]
            # Per-agent limits: show the biggest consumer.
            agents = [
                r[0] for r in conn.execute("SELECT DISTINCT agent FROM daily_usage").fetchall()
            ]
            return max((_totals(conn, a, limit.window, ts)[metric] for a in agents), default=0)
    except Exception:
        return None


# ── One-time backfill from the analytics ledger ──────────────────────────────


def _ledger_event_cost_micro(event: Any) -> int:
    """What one historical ledger row cost *Olano*, in micro-USD.

    The cost budget bounds our own upstream bill, so a BYOK row contributes
    nothing however large its provider-price estimate: that money is the
    customer's, spent on the customer's own account. A gateway row's charge
    arrives in credits (``usd`` is deliberately 0.0 for ``olano:*`` — see
    analytics/threadcost/cost.py), converted here at the published rate.
    """
    credits = getattr(event, "credits", None)
    if credits is None:
        return 0
    try:
        from olano.catalog.credits_registry import credits_per_usd

        return round(float(credits) * 1_000_000 / credits_per_usd())
    except Exception:  # pragma: no cover - a rate lookup must not break the seed
        logger.debug("credit rate unavailable while seeding", exc_info=True)
        return 0


def _seed_from_ledger_locked(conn: sqlite3.Connection) -> None:
    """Seed counters from the JSONL analytics ledger, once per home.

    Runs inside the caller's connection (cheap marker check per call). Keeps
    history from before this module existed counting toward lifetime/monthly
    budgets. Best-effort: a broken ledger seeds nothing but marks done.
    """
    done = conn.execute("SELECT value FROM meta WHERE key = 'seeded_from_ledger'").fetchone()
    if done is not None:
        return
    try:
        from olano.analytics.usage_ledger import iter_usage_events

        per_day: dict[tuple[str, str], list[int]] = {}
        for event in iter_usage_events():
            # Backfilled rows are reconstructed history (analytics/backfill.py)
            # — already spent long before these budgets existed. Counting them
            # would retroactively exhaust lifetime/monthly caps and start
            # refusing live turns over old cognition runs.
            if getattr(event, "backfill", False):
                continue
            agent = getattr(event, "agent", None) or ""
            ts = str(getattr(event, "ts", "") or "")
            day = ts[:10]
            if not agent or len(day) != 10:
                continue
            bucket = per_day.setdefault((agent, day), [0, 0, 0])
            bucket[0] += int(getattr(event, "input_tokens", 0) or 0)
            bucket[1] += int(getattr(event, "output_tokens", 0) or 0)
            bucket[2] += _ledger_event_cost_micro(event)
        for (agent, day), (inp, out, cost) in per_day.items():
            _add_usage(conn, agent, day, inp, out, cost)
        if per_day:
            logger.info("seeded usage counters from ledger: %d agent-days", len(per_day))
    except Exception:
        logger.exception("usage-ledger backfill failed; starting counters empty")
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('seeded_from_ledger', '1')")
