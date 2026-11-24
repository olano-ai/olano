"""Wall-clock facts for autonomous runs, and the arithmetic of a missed firing.

Two defects shaped this module, both found on managed boxes on 2026-11-02.

**An autonomous run did not know what day it was.** The system prompt carries
no current date or time, a scheduled task's prompt says nothing about when it
fired, and nothing tells the model to call ``get_current_time`` before it
applies a "Monday to Friday" rule. An agent whose campaign brief said
"weekdays only" read the newest dates in its own files, decided it was
Saturday, and logged a skip on a Monday morning. Every autonomous prompt
built here now opens with :func:`run_stamp` (the weekday, the date and the
time in the task's own timezone, with UTC beside it), and
``core/clock_middleware`` puts :func:`date_section` (day granularity, so a
cached prompt prefix changes once a day and not once a minute) at the end of
every model request's system message.

**A firing more than five minutes late was dropped, never run.** APScheduler
decides lateness against ``misfire_grace_time``; a daily job due while the
event loop was busy, or while the runtime was restarting, was recorded as
skipped (or, across a restart, not recorded at all, because the job was
re-added with its next fire computed from "now"). :func:`missed_fire` is the
boot-time half of the fix: given a task's trigger and what its history says
about the last run, it names the fire time inside the grace window that no
run covered, so ``OlanoScheduler._load_db_tasks`` can run it at once instead
of waiting for tomorrow. :func:`latest_due_fire` is the half a late run uses
to tell the agent how late it is.

Pure: no scheduler, no I/O, no logging. Every function takes ``now`` so tests
pin a clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc

#: The directive every scheduled DB task's prompt opens with. Shared with the
#: config-schedule path so both autonomous entry points say the same thing.
AUTONOMOUS_DIRECTIVE = (
    "[Autonomous scheduled task — execute directly and completely. "
    "Do NOT ask the user for confirmation or approval before acting.]"
)

#: A run that starts this much after its due time tells the agent so. Under
#: it, a few seconds of loop jitter is not worth a sentence of prompt.
LATE_AFTER = timedelta(minutes=2)

#: Upper bound on the fire times :func:`latest_due_fire` walks through. A
#: per-minute job over a six-hour grace is 360 steps; the bound only exists
#: so a trigger that never advances cannot spin.
_MAX_WALK = 5000


def resolve_zone(name: str | None) -> tuple[str, tzinfo]:
    """``(label, tzinfo)`` for an IANA name; an empty or unknown name is UTC.

    Never raises: a task row's ``timezone`` is user input and a stamp that
    cannot be rendered must not stop the run it decorates.
    """
    raw = (name or "").strip()
    if not raw or raw.upper() == "UTC":
        return "UTC", UTC
    try:
        return raw, ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return "UTC", UTC


def _aware(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now


def format_stamp(now: datetime | None = None, tz_name: str | None = None) -> str:
    """``Monday 2 November 2026, 08:00 Asia/Singapore (UTC+08:00); 00:00 UTC``.

    The weekday comes first because it is the fact the model got wrong; the
    local wall clock second because a task's schedule is written in it; UTC
    last so a log line and the stamp can be matched. For a UTC task the local
    half is omitted rather than printed twice.
    """
    moment = _aware(now)
    label, zone = resolve_zone(tz_name)
    utc_part = moment.astimezone(UTC).strftime("%H:%M UTC")
    if zone is UTC:
        return moment.astimezone(UTC).strftime("%A %-d %B %Y, %H:%M UTC")
    local = moment.astimezone(zone)
    offset = local.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
    local_part = local.strftime(f"%A %-d %B %Y, %H:%M {label} (UTC{offset})")
    # The UTC date is only worth a mention when it differs from the local one,
    # which is exactly the case where "today" is ambiguous.
    if local.date() != moment.astimezone(UTC).date():
        utc_part = moment.astimezone(UTC).strftime("%A %-d %B %Y, %H:%M UTC")
    return f"{local_part}; {utc_part}"


def _humanize(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total < 90:
        return f"{total} seconds"
    minutes = total // 60
    if minutes < 90:
        return f"{minutes} minutes"
    hours, rem = divmod(minutes, 60)
    if rem < 5:
        return f"{hours} hours"
    return f"{hours} hours {rem} minutes"


def run_stamp(
    tz_name: str | None,
    *,
    due_at: datetime | None = None,
    now: datetime | None = None,
) -> str:
    """The time lines an autonomous run's prompt opens with.

    ``due_at`` is the fire time the schedule asked for; when the run starts
    more than :data:`LATE_AFTER` behind it (the loop was busy, or the runtime
    was restarting and ``missed_fire`` is catching the task up), the second
    line says so, with the due time in the same timezone, so the agent can
    decide whether a 08:00 report still makes sense at 10:40 instead of
    silently dating its work to the wrong moment.
    """
    moment = _aware(now)
    lines = [f"Current date and time: {format_stamp(moment, tz_name)}."]
    if due_at is not None:
        due = _aware(due_at)
        behind = moment - due
        if behind > LATE_AFTER:
            lines.append(
                f"This run was due at {format_stamp(due, tz_name)} and is starting "
                f"{_humanize(behind)} late because the runtime was busy or restarting. "
                "Treat the current date and time above as the run's date and time."
            )
    lines.append(
        "Use this as the authoritative date and weekday for any rule in your "
        "instructions that depends on them; call get_current_time if you need the "
        "time again later in the run."
    )
    return "\n".join(lines)


def autonomous_preamble(
    prompt: str,
    *,
    tz_name: str | None,
    due_at: datetime | None = None,
    now: datetime | None = None,
    directive: str = AUTONOMOUS_DIRECTIVE,
) -> str:
    """``directive`` + the time stamp + a blank line + the task's own prompt."""
    head = directive.strip()
    stamp = run_stamp(tz_name, due_at=due_at, now=now)
    return f"{head}\n{stamp}\n\n{prompt}" if head else f"{stamp}\n\n{prompt}"


def display_timezone(env: dict[str, str] | None = None) -> str | None:
    """The timezone the per-turn date line is also rendered in, or None.

    ``LIONCLAW_CLOCK_TIMEZONE`` (its ``OLANO_`` twin is mirrored onto it by
    ``env_aliases``) when set, else ``TZ``; None means "whatever offset the
    host clock carries", which in a container is UTC.
    """
    import os

    source = os.environ if env is None else env
    for key in ("LIONCLAW_CLOCK_TIMEZONE", "OLANO_CLOCK_TIMEZONE", "TZ"):
        value = (source.get(key) or "").strip()
        if value:
            return value
    return None


_DATE_SECTION_TAIL = (
    "The time of day is not in this prompt: call get_current_time before any "
    "decision that depends on the hour, or on the weekday in another timezone."
)


def date_section(now: datetime | None = None, tz_name: str | None = None) -> str:
    """The system-prompt block: today's date and weekday, no time of day.

    Day granularity is deliberate. The block sits at the end of a system
    prompt that providers cache by prefix; a time of day would invalidate that
    cache on every call made in a new minute, a date does it once a day. The
    time of day is one ``get_current_time`` call away and the block says so.
    ``tz_name`` None means :func:`display_timezone`.
    """
    moment = _aware(now)
    utc_day = moment.astimezone(UTC).strftime("%A %-d %B %Y")
    name = tz_name if tz_name is not None else display_timezone()
    if name:
        label, zone = resolve_zone(name)
        local = moment.astimezone(zone)
    else:
        local = moment.astimezone()
        label = local.tzname() or "local time"
        zone = local.tzinfo or UTC
    local_day = local.strftime("%A %-d %B %Y")
    if zone is UTC or label.upper() in ("UTC", "GMT"):
        line = f"Today is {utc_day} (UTC)."
    elif local_day == utc_day:
        line = f"Today is {utc_day} (UTC), the same date in {label}."
    else:
        line = f"Today is {utc_day} (UTC); in {label} it is {local_day}."
    return f"## Current date\n{line}\n{_DATE_SECTION_TAIL}"


# --------------------------------------------------------------------- fires


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def latest_due_fire(trigger: Any, *, now: datetime, not_before: datetime) -> datetime | None:
    """The most recent fire time of ``trigger`` in ``(not_before, now]``.

    Walks the trigger forward from ``not_before`` with APScheduler's own
    ``get_next_fire_time``, so cron fields, the task's timezone and a DST
    fold are all the trigger's business, never re-implemented here. A
    ``DateTrigger`` answers its one date (it ignores the walk's cursor, which
    is why it is handled by hand).
    """
    now = _aware(now)
    not_before = _aware(not_before)
    run_date = getattr(trigger, "run_date", None)
    if isinstance(run_date, datetime):
        run_date = _aware(run_date)
        return run_date if not_before < run_date <= now else None
    latest: datetime | None = None
    cursor = not_before
    for _ in range(_MAX_WALK):
        try:
            nxt = trigger.get_next_fire_time(None, cursor)
        except Exception:
            return latest
        if nxt is None:
            break
        nxt = _aware(nxt)
        if nxt > now or (latest is not None and nxt <= latest):
            break
        latest = nxt
        cursor = nxt + timedelta(microseconds=1)
    return latest


def missed_fire(
    trigger: Any,
    *,
    now: datetime,
    grace: timedelta,
    last_run: Any = None,
    created_at: Any = None,
) -> datetime | None:
    """A fire time inside the grace window that no run covered, else None.

    ``last_run`` and ``created_at`` are the task row's ISO strings (either
    may be missing). A fire counts as missed when it is newer than the last
    recorded run (a skipped or failed run counts as recorded: the history
    row is the trace, and re-running a failure is a retry policy this is
    not) and not older than the task itself, so a task created at 09:00 with
    an 08:00 schedule is not "owed" this morning's run.
    """
    now = _aware(now)
    if grace <= timedelta(0):
        return None
    due = latest_due_fire(trigger, now=now, not_before=now - grace)
    if due is None:
        return None
    last = _parse_iso(last_run)
    if last is not None and last >= due:
        return None
    created = _parse_iso(created_at)
    if created is not None and created > due:
        return None
    return due


#: Weekday names in APScheduler's order (Monday first), which is also the
#: order every readout lists them in.
WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
#: Standard cron's number for each name: Sunday is 0 (and 7), Monday is 1.
_STANDARD_NUMBER = {name: (index + 1) % 7 for index, name in enumerate(WEEKDAY_NAMES)}


def _standard_weekday_set(field: str) -> set[int] | None:
    """Monday-first day indexes (0=Mon .. 6=Sun) a standard-cron field names.

    Accepts what cron accepts: ``*``, ``*/n``, a number (0 or 7 is Sunday), a
    three-letter name, a range of either (``1-5``, ``mon-fri``, wrapping
    ``fri-mon``), a step on a range, and comma lists of those. None when the
    field is not one of those shapes.
    """
    days: set[int] = set()
    for piece in field.strip().lower().split(","):
        base, _, step_text = piece.partition("/")
        try:
            step = int(step_text) if step_text else 1
        except ValueError:
            return None
        if step < 1 or not base:
            return None
        if base in ("*", "?"):
            numbers = list(range(0, 7, step))
        else:
            first, dash, last = base.partition("-")
            ends = []
            for token in (first, last) if dash else (first,):
                if token in _STANDARD_NUMBER:
                    ends.append(_STANDARD_NUMBER[token])
                elif token.isdigit() and int(token) <= 7:
                    ends.append(int(token))
                else:
                    return None
            if not dash:
                # "n/step" is "from n to the end of the week, every step".
                numbers = list(range(ends[0], 7, step)) if step_text else [ends[0]]
            elif ends[0] <= ends[1]:
                numbers = list(range(ends[0], ends[1] + 1, step))
            else:  # fri-mon: through the end of the week and around
                numbers = [n % 7 for n in range(ends[0], ends[1] + 8, step)]
        days.update((n - 1) % 7 for n in numbers)
    return days or None


def standard_weekdays(field: str | None) -> str:
    """A standard-cron day-of-week field in the form APScheduler runs correctly.

    Cron counts the week from **Sunday = 0** (7 is Sunday too), so ``1-5`` is
    Monday to Friday. APScheduler counts from Monday = 0, and the scheduler
    used to hand it the field as written: six campaign tasks an agent created
    as ``* * 1-5`` ran Tuesday to Saturday and never on a Monday (2026-11-23,
    one managed box), and the Cortex presets written ``* * 0`` for Sunday ran
    on Mondays. Every model, every person and the guide write standard cron,
    so that is the contract now, and this is the one place it is translated:
    numbers and ranges come out as weekday names, which APScheduler reads the
    same way everyone else does. A field that is not cron is returned
    unchanged, so APScheduler still rejects it where it did.
    """
    text = str(field or "").strip()
    if text in ("", "*", "?"):
        return "*"
    days = _standard_weekday_set(text)
    if days is None:
        return text
    if len(days) == 7:
        return "*"
    return ",".join(WEEKDAY_NAMES[d] for d in sorted(days))


def cron_weekday_field(expr: str | None) -> str | None:
    """The day-of-week field of a 5-field cron, or None when it restricts nothing."""
    parts = str(expr or "").split()
    if len(parts) != 5 or parts[4] in ("*", "?"):
        return None
    return parts[4]


def cron_weekdays(expr: str | None) -> list[str] | None:
    """The weekdays a cron fires on (``["Mon", ...]``), or None.

    None for a field that restricts nothing or is not cron. Read through the
    same translation the triggers use, so what it says is what fires.
    """
    field = cron_weekday_field(expr)
    if field is None:
        return None
    days = _standard_weekday_set(field)
    if days is None or len(days) == 7:
        return None
    return [_WEEKDAY_LABELS[d] for d in sorted(days)]


def weekday_note(expr: str | None) -> str:
    """One line naming the days a cron fires on, empty when it has no weekday rule.

    Every agent-facing readout of a task carries it, so a schedule that does
    not mean what its author intended is visible the moment it is written.
    """
    days = cron_weekdays(expr)
    return f"Runs on: {', '.join(days)}" if days else ""


def format_next_run(value: Any, tz_name: str | None = None) -> str | None:
    """A next fire time with its weekday, in the task's timezone; None if unparseable."""
    moment = value if isinstance(value, datetime) else _parse_iso(value)
    if moment is None:
        return None
    label, zone = resolve_zone(tz_name)
    return f"{moment.astimezone(zone).strftime('%a %Y-%m-%d %H:%M')} {label}"
