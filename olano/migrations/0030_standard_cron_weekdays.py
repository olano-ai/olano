"""Keep every existing schedule on the days it runs today, in weekday names.

Until this release a cron's day-of-week field was handed to the scheduler
library as written, and that library counts the week from Monday = 0. The
runtime now reads the field as standard cron (Sunday = 0 or 7, so ``1-5`` is
Monday to Friday). Without this migration every stored schedule with a numeric
day of week would move by one day on upgrade, including the ones built with
the dashboard's weekday picker, which were correct.

So each numeric day-of-week field is rewritten once into the names of the days
it has actually been firing on (``0-4`` becomes ``mon,tue,wed,thu,fri``, and
``1-5`` becomes ``tue,wed,thu,fri,sat``). Names mean the same thing before and
after, so nothing changes when a task runs; a schedule that was written as
standard cron and has been running a day late keeps doing so, but now says so
in plain words wherever it is shown, and is one edit away from being right.

Covered: saved tasks in the scheduler database, ``schedule`` entries and the
Cortex ``*_cron`` overrides in each ``agent.json5``, and ``when:`` lines in
each workspace ``HEARTBEAT.md``. A field that is already names, a wildcard, or
not something the old scheduler could parse is left alone. The built-in Cortex
presets are not stored anywhere, so they simply start running on the days the
guide has always named.

The old numbering is spelled out here rather than imported: a migration is a
historical record and must keep meaning the same thing.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

ID = "0030_standard_cron_weekdays"

_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WHEN_LINE = re.compile(r"^(\s*when:\s+)([\"']?)(.+?)([\"']?)(\s*(?:#.*)?)$")


def _legacy_days(field: str) -> set[int] | None:
    """Day indexes (0=Mon .. 6=Sun) the old scheduler fired a field on."""
    days: set[int] = set()
    for piece in field.strip().lower().split(","):
        base, _, step_text = piece.partition("/")
        try:
            step = int(step_text) if step_text else 1
        except ValueError:
            return None
        if step < 1 or not base:
            return None
        if base == "*":
            days.update(range(0, 7, step))
            continue
        first, dash, last = base.partition("-")
        ends = []
        for token in (first, last) if dash else (first,):
            if token in _NAMES:
                ends.append(_NAMES.index(token))
            elif token.isdigit() and int(token) <= 6:
                ends.append(int(token))
            else:
                return None
        if not dash:
            days.update(range(ends[0], 7, step) if step_text else [ends[0]])
        elif ends[0] <= ends[1]:
            days.update(range(ends[0], ends[1] + 1, step))
        else:
            return None  # the old scheduler rejected a backwards range
    return days or None


def legacy_weekday_names(field: str) -> str:
    """A numeric day-of-week field as the names of the days it used to mean."""
    text = str(field or "").strip()
    if not any(ch.isdigit() for ch in text):
        return text
    days = _legacy_days(text)
    if days is None:
        return text
    if len(days) == 7:
        return "*"
    return ",".join(_NAMES[d] for d in sorted(days))


def convert_cron(expr: str) -> str:
    """A 5-field cron with its day-of-week field converted; else unchanged."""
    parts = str(expr or "").split()
    if len(parts) != 5:
        return expr
    converted = legacy_weekday_names(parts[4])
    if converted == parts[4]:
        return expr
    return " ".join([*parts[:4], converted])


def _migrate_tasks(home: Path) -> None:
    db_path = home / "scheduler" / "scheduler.db"
    if not db_path.is_file():
        return
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
    except sqlite3.Error as exc:
        logger.warning("%s: cannot open %s: %s", ID, db_path, exc)
        return
    try:
        rows = conn.execute(
            "SELECT id, schedule FROM tasks WHERE schedule IS NOT NULL AND schedule != ''"
        ).fetchall()
        changed = 0
        for task_id, schedule in rows:
            converted = convert_cron(schedule)
            if converted != schedule:
                conn.execute("UPDATE tasks SET schedule = ? WHERE id = ?", (converted, task_id))
                changed += 1
        conn.commit()
        if changed:
            logger.info("%s: rewrote the weekdays of %d saved task(s)", ID, changed)
    except sqlite3.Error as exc:
        logger.warning("%s: scheduler database not migrated: %s", ID, exc)
    finally:
        conn.close()


def _repair_surrogates(text: str) -> str:
    try:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    except Exception:
        return text


def _migrate_agent_configs(home: Path) -> None:
    from olano.config.loader import _load_json5, write_agent_config_text

    agents_dir = home / "agents"
    if not agents_dir.is_dir():
        return
    for config_path in sorted(agents_dir.glob("*/agent.json5")):
        try:
            data = _load_json5(config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("%s: skipping unparseable %s: %s", ID, config_path, exc)
            continue
        if not isinstance(data, dict):
            continue
        changed = False
        entries = data.get("schedule")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            cron = entry.get("cron")
            if isinstance(cron, str) and convert_cron(cron) != cron:
                entry["cron"] = convert_cron(cron)
                changed = True
            dow = entry.get("day_of_week")
            if isinstance(dow, str) and legacy_weekday_names(dow) != dow:
                entry["day_of_week"] = legacy_weekday_names(dow)
                changed = True
        cognition = data.get("cognition")
        if isinstance(cognition, dict):
            for key, value in list(cognition.items()):
                if (
                    key.endswith("_cron")
                    and isinstance(value, str)
                    and convert_cron(value) != value
                ):
                    cognition[key] = convert_cron(value)
                    changed = True
        if not changed:
            continue
        payload = _repair_surrogates(json.dumps(data, indent=2, ensure_ascii=False)) + "\n"
        write_agent_config_text(config_path, payload)
        logger.info("%s: rewrote schedule weekdays on %s", ID, config_path.parent.name)


def convert_heartbeat_text(text: str) -> str:
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        match = _WHEN_LINE.match(line)
        if not match:
            continue
        head, open_quote, value, close_quote, tail = match.groups()
        converted = convert_cron(value)
        if converted != value:
            lines[index] = f"{head}{open_quote}{converted}{close_quote}{tail}"
    return "\n".join(lines)


def _migrate_heartbeats(home: Path) -> None:
    candidates = [home / "workspace" / "HEARTBEAT.md"]
    agents_dir = home / "agents"
    if agents_dir.is_dir():
        candidates += sorted(agents_dir.glob("*/workspace/HEARTBEAT.md"))
    for path in candidates:
        try:
            if not path.is_file() or path.is_symlink():
                continue
            text = path.read_text(encoding="utf-8")
            converted = convert_heartbeat_text(text)
            if converted == text:
                continue
            # Written in place, so the file keeps its owner (the sandbox agent).
            path.write_text(converted, encoding="utf-8")
            logger.info("%s: rewrote heartbeat weekdays in %s", ID, path)
        except Exception as exc:
            logger.warning("%s: %s not migrated: %s", ID, path, exc)


def apply(home: Path) -> None:
    _migrate_tasks(home)
    _migrate_agent_configs(home)
    _migrate_heartbeats(home)
