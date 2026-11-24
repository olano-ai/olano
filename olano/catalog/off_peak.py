"""Time-of-day pricing: which rate a call made at a given moment pays.

DeepSeek bills half its list price outside its peak hours. The cloud catalog
carries that next to the (peak) price columns: ``metadata.off_peak`` on a model
row, in USD per 1M, and ``credit_pricing.off_peak`` on an ``olano:<tier>`` row,
in credits per 1M. Both use LiteLLM's ``off_peak_pricing`` window shape, which
is what the managed gateway bills by, so the rule here mirrors LiteLLM's
exactly (``litellm_core_utils/llm_cost_calc/utils.py``, v1.102.0):

- ``windows`` is a list of ``{"weekdays": [...], "hours_utc": [...]}``;
- weekdays are ISO (1 = Monday ... 7 = Sunday) on the UTC calendar;
- a range is ``HH:MM-HH:MM`` UTC, start inclusive, end exclusive; start after
  end wraps past midnight (``10:00-00:00`` runs to midnight) and start equal
  to end is the whole day.

Anything malformed reads as "not off-peak", so a bad block costs the peak
rate, never a discount nobody published. Pure and stdlib-only on purpose: it
runs on every costed call, and litellm's own helper is private and slow to
import.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


def _minutes(hhmm: str) -> int | None:
    try:
        hours, minutes = hhmm.strip().split(":")
        h, m = int(hours), int(minutes)
    except (ValueError, AttributeError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def _in_range(span: object, minute: int) -> bool:
    if not isinstance(span, str) or span.count("-") != 1:
        return False
    start_s, end_s = span.split("-")
    start, end = _minutes(start_s), _minutes(end_s)
    if start is None or end is None:
        return False
    if start == end:
        return True
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def is_off_peak(windows: object, at: datetime | None = None) -> bool:
    """True when ``at`` (default: now) falls inside one of the off-peak windows.

    A naive ``at`` is taken to be UTC, as LiteLLM does.
    """
    if not isinstance(windows, list | tuple):
        return False
    moment = at or datetime.now(UTC)
    moment = moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
    day = moment.isoweekday()
    minute = moment.hour * 60 + moment.minute
    for rule in windows:
        if not isinstance(rule, Mapping):
            continue
        days = rule.get("weekdays")
        if not isinstance(days, list | tuple) or day not in days:
            continue
        hours = rule.get("hours_utc")
        spans = [hours] if isinstance(hours, str) else hours
        if isinstance(spans, list | tuple) and any(_in_range(s, minute) for s in spans):
            return True
    return False


def active_off_peak(block: object, at: datetime | None = None) -> Mapping[str, Any] | None:
    """The off-peak block when it applies at ``at``, else None."""
    if not isinstance(block, Mapping):
        return None
    return block if is_off_peak(block.get("windows"), at) else None


def parse_timestamp(value: object) -> datetime | None:
    """A call's recorded time as an aware UTC datetime, or None.

    Accepts what message metadata carries: epoch seconds (or milliseconds)
    as a number or numeric string, and ISO-8601 strings (``Z`` included).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float) or (isinstance(value, str) and value.strip().isdigit()):
        seconds = float(value)
        if seconds > 1e12:  # milliseconds
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None
