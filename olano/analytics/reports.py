"""Weekly / monthly rollup reports over the daily work-journal highlights.

A report is a map-reduce over the per-agent daily highlights: each day's stored
``agent_highlights`` (see :mod:`olano.analytics.journal`) are the substance,
and a single LLM pass rolls the period's per-agent lines up into a per-agent
summary — stored in the SAME marker-structured shape as a daily entry so the
exact same scoping applies (a member sees only their granted agents; admins /
"show all" mode see the whole fleet; see :func:`journal.scoped_narrative`).

Reports live at ``<home>/journal/reports/<weekly|monthly>/<key>.json`` where the
key is the ISO week (``2026-W30``) or the month (``2026-07``).
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from olano.analytics import journal
from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)

PERIODS = ("weekly", "monthly")
# Cap the per-agent day lines fed into a rollup prompt so a very busy month
# can't blow up the context (newest days win — they're the most relevant).
_MAX_DAYS_PER_AGENT = 40


def normalize_period(period: str | None) -> str:
    p = (period or "weekly").strip().lower()
    if p not in PERIODS:
        raise ValueError(f"Unknown report period {period!r} (expected one of {PERIODS}).")
    return p


def _parse_anchor(anchor: str | None) -> date:
    if not anchor:
        return datetime.now(timezone.utc).date()
    return date.fromisoformat(anchor)


def period_key(period: str, anchor: date) -> str:
    """Stable storage key for the period containing ``anchor``."""
    if period == "weekly":
        iso = anchor.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    return f"{anchor.year:04d}-{anchor.month:02d}"


def period_bounds(period: str, anchor: date) -> tuple[date, date]:
    """(start, end) inclusive dates of the period containing ``anchor``."""
    if period == "weekly":
        start = anchor - timedelta(days=anchor.weekday())  # Monday
        return start, start + timedelta(days=6)
    start = anchor.replace(day=1)
    nxt = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start, nxt - timedelta(days=1)


def _range_days(start: date, end: date) -> list[str]:
    out: list[str] = []
    d = start
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def report_dir(period: str) -> Path:
    d = get_olano_paths().reports_dir / period
    d.mkdir(parents=True, exist_ok=True)
    return d


def report_path(period: str, key: str) -> Path:
    return report_dir(period) / f"{key}.json"


def read_report(period: str, key: str) -> dict[str, Any] | None:
    path = report_path(period, key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def list_reports(period: str) -> list[str]:
    """Available report keys for a period, newest first."""
    d = get_olano_paths().reports_dir / period
    if not d.exists():
        return []
    return sorted((p.stem for p in d.glob("*.json")), reverse=True)


def gather_range_highlights(start: date, end: date) -> dict[str, list[dict[str, str]]]:
    """``agent -> [{date, text}]`` from the stored daily journal highlights in
    the range (newest day first). Reuses the daily per-agent highlights as the
    substance of the rollup — no re-reading of raw transcripts."""
    out: dict[str, list[dict[str, str]]] = defaultdict(list)
    for day in reversed(_range_days(start, end)):
        entry = journal.read_journal(day)
        ah = (entry or {}).get("agent_highlights") or {}
        if not isinstance(ah, dict):
            continue
        for agent, text in ah.items():
            if text:
                out[agent].append({"date": day, "text": str(text)})
    return dict(out)


def _report_prompt(period: str, start: date, end: date, per_agent: dict[str, list[dict]]) -> str:
    span = "week" if period == "weekly" else "month"
    lines = [f"Reporting {span}: {start.isoformat()} to {end.isoformat()}", ""]
    for agent, days in per_agent.items():
        lines.append(f"Agent {agent} — daily highlights this {span}:")
        for entry in days[:_MAX_DAYS_PER_AGENT]:
            lines.append(f"  {entry['date']}: {entry['text']}")
        lines.append("")
    body = "\n".join(lines).strip()
    if not per_agent:
        body += f"\n(No agent did notable work during this {span}.)"
    return body


def report_scaffold(period: str, key: str, start: date, end: date) -> dict[str, Any]:
    return {
        "period": period,
        "key": key,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "narrative": None,
        "agent_highlights": {},
        "narrative_lead": None,
        "narrative_model": None,
    }


async def generate_report(
    period: str,
    anchor: str | None = None,
    *,
    model: str | None = None,
    save: bool = True,
) -> dict[str, Any]:
    """Roll the period's daily per-agent highlights up into one report.

    Same marker-structured, per-agent output shape as a daily entry, so
    :func:`journal.scoped_narrative` scopes it to a member's agents unchanged.
    Best-effort: failures return the scaffold with a transient ``narrative_error``.
    """
    period = normalize_period(period)
    anchor_date = _parse_anchor(anchor)
    key = period_key(period, anchor_date)
    start, end = period_bounds(period, anchor_date)
    entry = report_scaffold(period, key, start, end)

    per_agent = gather_range_highlights(start, end)
    if not per_agent:
        # Nothing generated for any day in range — persist an empty report so
        # the UI shows "quiet period" instead of an error, without an LLM call.
        if save:
            _write(period, key, entry)
        return entry

    resolved = model or journal._resolve_highlights_model()
    if not resolved:
        entry["narrative_error"] = "No model configured for report generation."
        return entry

    prompt = (
        f"{journal.HIGHLIGHTS_FORMAT_INSTRUCTIONS}\n{_report_prompt(period, start, end, per_agent)}"
    )
    try:
        from olano.models import build_model

        result = await build_model(resolved).ainvoke(prompt)
        # Outside any AgentRunner turn — ledger the spend (see journal.py).
        from olano.analytics.usage_recorder import record_result_usage

        record_result_usage("system", resolved, result, origin="reports")
        text = journal._extract_text(result)
        if text:
            journal._apply_highlights(entry, text, resolved)
            if save:
                _write(period, key, entry)
        else:
            entry["narrative_error"] = f"Model '{resolved}' returned no usable text."
    except Exception as exc:
        logger.warning("reports: %s report generation failed for %s", period, key, exc_info=True)
        detail = " ".join(str(exc).split())[:300] or type(exc).__name__
        entry["narrative_error"] = f"{resolved}: {detail}"
    return entry


def _write(period: str, key: str, entry: dict[str, Any]) -> None:
    try:
        report_path(period, key).write_text(
            json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        logger.warning("reports: failed to write %s/%s", period, key, exc_info=True)


def last_completed_anchor(period: str) -> str:
    """ISO date inside the most recently *completed* period (for the cron job):
    yesterday's week/month, i.e. the one that just ended."""
    today = datetime.now(timezone.utc).date()
    if period == "weekly":
        return (today - timedelta(days=today.weekday() + 1)).isoformat()  # last Sunday
    first_of_month = today.replace(day=1)
    return (first_of_month - timedelta(days=1)).isoformat()  # last day of prev month
