"""Brightwater Swim School: the team in the weekend video.

Remi is the owner's assistant. In the video Remi plans a family weekend: it reads what is on at
Gardens by the Bay (the calendar of events page) and at NParks (the events page), two real public
websites with no API and no account, picks a few events that fall on the coming Saturday and
Sunday, and puts a reminder for each on Olano's calendar. Every browser step of the scripted turn
runs the product's own browser tool for the agent in the product's headless browser against the
live sites (``browser_navigate``, ``browser_click`` to close the site's notice and cookie banner,
``browser_snapshot``); the reminders are the product's own ``scheduler_create`` tool, so the
calendar shows what it saved. The reply is built from what the pages showed during the take:
event names, dates, times and places are the sites' own.

The way out to the internet is the same as in ``teams/pricecheck.py`` (``BROWSER_PROXY_URL``
pointed at this machine's HTTPS proxy); the browser is set to Headless before the take.
"""

from __future__ import annotations

import atexit
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from teams.pricecheck import close_browsers, tool

SGT = ZoneInfo("Asia/Singapore")
GBTB = "https://www.gardensbythebay.com.sg/en/things-to-do/calendar-of-events.html"
NPARKS = "https://www.nparks.gov.sg/visit/events"
MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
_ICON = re.compile(r"[-\U000f0000-\U000fffff]")

# Arguments the chat shows for steps whose target is only known once the page is open (the
# refs of the site's notice and cookie banner); filled in by the step before them.
CLOSE_NOTICE: dict[str, str] = {"ref": ""}
CLOSE_COOKIES: dict[str, str] = {"ref": ""}
PAGES: dict[str, str] = {}
PLAN: list[dict[str, Any]] = []
REMINDERS: list[dict[str, Any]] = [{}, {}, {}]  # the scheduler_create arguments, filled in


def weekend(today: date | None = None) -> tuple[date, date]:
    """The coming Saturday and Sunday (this weekend's, from Monday to Saturday)."""
    today = today or datetime.now(SGT).date()
    sat = today + timedelta(days=(5 - today.weekday()) % 7)
    if today.weekday() == 6:
        sat = today - timedelta(days=1)
    return sat, sat + timedelta(days=1)


def parse_dates(text: str, default_year: int) -> tuple[date, date] | None:
    found = re.findall(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?(?:\s+(\d{4}))?", text)
    days = [(int(d), MONTHS.get(m.lower())) for d, m, _ in found]
    years = [int(y) if y else None for _, _, y in found]
    if not days or any(m is None for _, m in days):
        return None
    end_year = years[-1] or default_year
    end = date(end_year, days[-1][1], days[-1][0])  # type: ignore[arg-type]
    start_year = years[0] or (end_year - 1 if days[0][1] > days[-1][1] else end_year)  # type: ignore[operator]
    start = date(start_year, days[0][1], days[0][0])  # type: ignore[arg-type]
    return start, end


def parse_time(text: str) -> tuple[int, int] | None:
    m = re.search(r"(\d{1,2})(?:[.:](\d{2}))?\s*([ap])\.?m", text, re.I)
    if not m:
        return None
    hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
    return hour, int(m.group(2) or 0)


def _clean(text: str) -> str:
    return " ".join(_ICON.sub("", text).replace("\\'", "'").split()).strip(' "')


def gbtb_events(snapshot: str) -> list[dict[str, Any]]:
    """Events on the Gardens by the Bay calendar page, from its ``browser_navigate`` snapshot."""
    out = []
    for block in re.split(r"\n- link ", snapshot):
        title = re.search(r'heading "(.+?)" \[level=6\]', block)
        url = re.search(r"/url: (/en/things-to-do/calendar-of-events/\S+\.html)", block)
        if not (title and url):
            continue
        facts = [_clean(v) for v in re.findall(r"generic \[ref=e\d+\]: (.+)", block)]
        facts = [f for f in facts if f]
        when = next((f for f in facts if re.search(r"\d{1,2}\s+[A-Z][a-z]{2}", f)), "")
        hours = next((f for f in facts if parse_time(f) or "timing" in f.lower()), "")
        place = facts[-1] if facts and facts[-1] not in (when, hours) else ""
        out.append(
            {
                "title": _clean(title.group(1)),
                "when": when,
                "hours": hours,
                "place": place,
                "site": "Gardens by the Bay",
                "url": "https://www.gardensbythebay.com.sg" + url.group(1),
            }
        )
    return out


def nparks_events(snapshot: str) -> list[dict[str, Any]]:
    """Events on the NParks events page, from its ``browser_snapshot``."""
    out = []
    for block in re.split(r'\n- img "event-card"', snapshot)[1:]:
        title = re.search(r'- (?:\'link "(.+?)"|link "(.+?)") \[ref', block)
        url = re.search(r"/url: (/visit/events/event-detail/\S+)", block)
        texts = [_clean(t) for t in re.findall(r"\n- text: (.+)", block)]
        place = re.search(r"paragraph \[ref=e\d+\]: (.+)", block)
        if not (title and url and texts):
            continue
        name = _clean(title.group(1) or title.group(2)).replace("''", "'")
        out.append(
            {
                "title": name,
                "when": texts[0],
                "hours": texts[1] if len(texts) > 1 else "",
                "place": _clean(place.group(1)) if place else "",
                "site": "NParks",
                "url": "https://www.nparks.gov.sg" + url.group(1),
            }
        )
    return out


def choose(events: list[dict[str, Any]], sat: date, sun: date, n: int) -> list[dict[str, Any]]:
    """Up to *n* events on the weekend with a start time, shortest-running first."""
    found = []
    for ev in events:
        span = parse_dates(ev["when"], sat.year)
        start = parse_time(ev["hours"])
        if span is None or start is None or "promotion" in ev["title"].lower():
            continue
        days = [d for d in (sat, sun) if span[0] <= d <= span[1]]
        if not days:
            continue
        found.append({**ev, "days": days, "start": start, "length": (span[1] - span[0]).days})
    found.sort(key=lambda e: (e["length"], e["start"]))
    return found[:n]


def make_plan() -> list[dict[str, Any]]:
    sat, sun = weekend()
    picked = choose(gbtb_events(PAGES.get("gbtb", "")), sat, sun, 2)
    picked += choose(nparks_events(PAGES.get("nparks", "")), sat, sun, 1)
    load: dict[date, list[tuple[int, int]]] = {sat: [], sun: []}
    for ev in sorted(picked, key=lambda e: len(e["days"])):  # single-day events first
        day = min(ev["days"], key=lambda d: (len(load[d]), d))
        load[day].append(ev["start"])
        ev["day"] = day
    picked.sort(key=lambda e: (e["day"], e["start"]))
    return picked


def _short(title: str) -> str:
    return re.split(r" - | by |: ", title)[0].strip()[:48]


def _remind_at(ev: dict[str, Any]) -> datetime:
    hour, minute = ev["start"]
    at = datetime.combine(ev["day"], datetime.min.time()).replace(hour=hour, minute=minute)
    return at - timedelta(hours=1)


def reminder_args(ev: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": f"Weekend: {_short(ev['title'])}",
        "prompt": (
            f"Remind the family: {ev['title']} at {ev['place'] or ev['site']} "
            f"({ev['site']}), {ev['hours']}. Details: {ev['url']}"
        ),
        "run_at": _remind_at(ev).strftime("%Y-%m-%dT%H:%M:%S"),
        "timezone": "Asia/Singapore",
    }


def open_gbtb(ctx: dict) -> str:
    out = tool("browser_navigate", {"url": GBTB, "wait_for": "networkidle"})(ctx)
    PAGES["gbtb"] = out
    notice = re.search(r'- dialog \[active\].*?link "Close" \[ref=(e\d+)\]', out, re.S)
    cookies = re.search(
        r'button "[^"]*" \[ref=(e\d+)\]:\n\s+- generic.*\n- paragraph \[ref=e\d+\]:\n\s+- text: We use cookies',
        out,
    )
    CLOSE_NOTICE["ref"] = notice.group(1) if notice else ""
    CLOSE_COOKIES["ref"] = cookies.group(1) if cookies else ""
    return out


def click(args: dict[str, str]):
    def run(ctx: dict) -> str:
        if not args.get("ref"):
            return "Nothing to close: the page showed no such banner."
        return tool("browser_click", dict(args))(ctx)

    return run


def open_nparks(ctx: dict) -> str:
    return tool("browser_navigate", {"url": NPARKS})(ctx)


def read_nparks(ctx: dict) -> str:
    out = tool("browser_snapshot", {})(ctx)
    PAGES["nparks"] = out
    PLAN[:] = make_plan()
    for i, slot in enumerate(REMINDERS):
        slot.clear()
        if i < len(PLAN):
            slot.update(reminder_args(PLAN[i]))
    return out


def remind(i: int):
    def run(ctx: dict) -> str:
        if not REMINDERS[i]:
            return "Skipped: no event for this slot."
        return tool("scheduler_create", dict(REMINDERS[i]))(ctx)

    return run


def _day(d: date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def summary(ctx: dict) -> str:
    sat, sun = weekend()
    if not PLAN:
        return (
            f"I could not find events with a set time on {_day(sat)} or {_day(sun)} on "
            "either page, so I have not put anything on the calendar."
        )
    lines = []
    for ev in PLAN:
        place = ev["place"] or ev["site"]
        lines.append(
            f"- **{_day(ev['day'])}, {ev['hours']}**: [{ev['title']}]({ev['url']}), "
            f"{place} ({ev['site']})"
        )
    n = {1: "one reminder", 2: "two reminders", 3: "three reminders"}[len(PLAN)]
    return (
        f"Here is a plan for {_day(sat)} and {_day(sun)}, from the two calendars:\n\n"
        + "\n".join(lines)
        + f"\n\nI put {n} on the calendar, each an hour before the start. "
        "Dates and times are as the sites list them."
    )


TEAM = {
    "default_agent": "remi",
    "agents": [
        {
            "name": "remi",
            "emoji": "🗓️",
            "description": "The owner's assistant: diary, errands and family plans.",
            "title": "Owner's Assistant",
            "manager": None,
            "reports": ["dara", "kai"],
        },
        {
            "name": "dara",
            "emoji": "🏊",
            "description": "Front desk: enrolments, make-up lessons and parents' questions.",
            "title": "Front Desk",
            "manager": "remi",
        },
        {
            "name": "kai",
            "emoji": "⏱️",
            "description": "Builds the coaches' timetable and pool bookings.",
            "title": "Timetable",
            "manager": "remi",
        },
    ],
    "files": {
        "remi": {
            "AGENTS.md": "# remi\n\nAssistant to Priya Nair, owner of Brightwater Swim School.\n",
            "family.md": "# Family\n\n- Priya and Arun, children Maya (9) and Dev (6)\n"
            "- Free this weekend; prefers mornings, outdoors or gardens\n",
        },
        "dara": {"AGENTS.md": "# dara\n\nFront desk at Brightwater Swim School.\n"},
        "kai": {"AGENTS.md": "# kai\n\nTimetable at Brightwater Swim School.\n"},
    },
    "tasks": [
        {
            "name": "Pool booking check",
            "prompt": "Confirm next week's lane bookings with the pool.",
            "schedule": "30 8 * * mon",
            "agent_name": "kai",
        },
    ],
    "turns": {
        "remi": [
            {
                "steps": [
                    {"wait": 0.6},
                    {
                        "tool": "browser_navigate",
                        "args": {"url": GBTB, "wait_for": "networkidle"},
                        "result": open_gbtb,
                        "took": 0.3,
                    },
                    {"wait": 0.6},
                    {
                        "tool": "browser_click",
                        "args": CLOSE_NOTICE,
                        "result": click(CLOSE_NOTICE),
                        "took": 0.3,
                    },
                    {
                        "tool": "browser_click",
                        "args": CLOSE_COOKIES,
                        "result": click(CLOSE_COOKIES),
                        "took": 0.3,
                    },
                    {"wait": 0.6},
                    {
                        "tool": "browser_navigate",
                        "args": {"url": NPARKS},
                        "result": open_nparks,
                        "took": 0.3,
                    },
                    {"wait": 2.5},
                    {"tool": "browser_snapshot", "args": {}, "result": read_nparks, "took": 0.3},
                    {"wait": 0.8},
                    *[
                        {
                            "tool": "scheduler_create",
                            "args": REMINDERS[i],
                            "result": remind(i),
                            "took": 1.1,
                        }
                        for i in range(3)
                    ],
                    {"wait": 0.5},
                    {"text": summary, "wps": 16},
                ]
            },
        ],
    },
}


def after_seed(home: Path, **_: object) -> None:
    """The browser in Headless mode, as an administrator set it in Config, Browser."""
    cfg = home / "config.yaml"
    cfg.write_text(
        cfg.read_text(encoding="utf-8") + "browser:\n  mode: headless\n", encoding="utf-8"
    )
    atexit.register(close_browsers)
