"""Paper Kite Games, a small board-game studio in Singapore: the team in the kiwi video.

The studio is planning a team offsite in Tokyo. In the take the agent's MCP tab connects
Kiwi.com's public flight search MCP server (``https://mcp.kiwi.com``, streamable HTTP, no account)
as a custom server, and every ``search-flight`` step of the scripted turns is a real call to that
server with the official ``mcp`` Python SDK. The reply text is built from what the server returned
(prices, times, airlines and the booking links it hands back), so nothing in the answer is written
before the take.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

URL = "https://mcp.kiwi.com"
TRAVELLERS = 4
FIRST = {
    "flyFrom": "Singapore",
    "flyTo": "Tokyo",
    "departureDate": "13/01/2027",
    "returnDate": "17/01/2027",
    "adults": TRAVELLERS,
    "currency": "SGD",
}
FLEX = {**FIRST, "departureDateFlexDays": 1, "returnDateFlexDays": 1}

# What each call returned, for the reply that follows it (and the follow-up's comparison).
_RESULTS: dict[str, dict[str, Any]] = {}


async def _call(tool: str, args: dict[str, Any]) -> str:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(tool, args)
    return "\n".join(str(getattr(c, "text", "")) for c in res.content)


def _search(key: str, args: dict[str, Any]):
    """A scripted tool step whose result is the server's own answer."""

    def run(ctx: dict[str, Any]) -> str:
        try:
            text = asyncio.run(_call("search-flight", args))
        except Exception as exc:  # the step shows the failure, the reply says so
            _RESULTS[key] = {}
            return f"Error: {type(exc).__name__}: {exc}"
        try:
            _RESULTS[key] = json.loads(text)
        except ValueError:
            _RESULTS[key] = {}
        return text

    return run


def _clock(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(str(iso)).strftime("%H:%M")
    except ValueError:
        return "?"


def _day(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(str(iso)).strftime("%a %d %b").replace(" 0", " ")
    except ValueError:
        return "?"


def _hours(seconds: Any) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "?"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _airlines(leg: dict[str, Any]) -> str:
    names: list[str] = []
    for seg in leg.get("segments") or []:
        name = seg.get("carrierName") or seg.get("carrier")
        if name and name not in names:
            names.append(name)
    return " + ".join(names) or "?"


def _leg(leg: dict[str, Any]) -> str:
    """Departure time and route, e.g. ``17:35 SIN → HND`` (a stop shows as a middle airport)."""
    if not leg:
        return "?"
    route = leg.get("route") or [leg.get("from", "?"), leg.get("to", "?")]
    return f"{_clock(leg.get('departureTime'))} {' → '.join(str(r) for r in route)}"


def _price(it: dict[str, Any], currency: str) -> str:
    if it.get("priceFormatted"):
        return str(it["priceFormatted"])
    if it.get("price") is not None:
        return f"{it['price']:.0f} {currency}"
    return "?"


def _booked(it: dict[str, Any], currency: str, link: Any) -> str:
    """The price, linked to the booking page the server returned for it."""
    return f"[{_price(it, currency)}]({link})" if link else _price(it, currency)


def _itineraries(key: str) -> tuple[list[dict[str, Any]], str]:
    data = _RESULTS.get(key) or {}
    items = [i for i in data.get("itineraries") or [] if isinstance(i, dict) and i.get("outbound")]
    return items, str(data.get("currency") or "")


def _num(it: dict[str, Any], field: str) -> float:
    try:
        return float(it.get(field))
    except (TypeError, ValueError):
        return float("inf")


def _pick(items: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Cheapest, shortest, and one more with a different airline or flight, as the server asks."""
    cheapest = min(items, key=lambda i: _num(i, "price"))
    shortest = min(items, key=lambda i: (_num(i, "totalDurationSeconds"), _num(i, "price")))
    picked = [("Cheapest", cheapest)]
    if shortest is not cheapest:
        picked.append(("Quickest", shortest))
    seen = {(_airlines(p["outbound"]), p["outbound"].get("departureTime")) for _, p in picked}
    for it in sorted(items, key=lambda i: _num(i, "price")):
        if len(picked) >= 3:
            break
        sig = (_airlines(it["outbound"]), it["outbound"].get("departureTime"))
        if sig not in seen:
            picked.append(("Also good", it))
            seen.add(sig)
    return picked


def _first_reply(ctx: dict[str, Any]) -> str:
    items, currency = _itineraries("first")
    if not items:
        return (
            "Kiwi.com's search came back without any flights for those dates, so I have nothing "
            "to compare yet. Shall I try a day either side?"
        )
    picked = _pick(items)
    out_day = _day(picked[0][1]["outbound"].get("departureTime"))
    back_day = _day((picked[0][1].get("inbound") or {}).get("departureTime"))
    rows = []
    for label, it in picked:
        rows.append(
            f"- **{label}, {_airlines(it['outbound'])}: {_booked(it, currency, it.get('bookingUrl'))}** "
            f"for {TRAVELLERS}. Out {out_day} {_leg(it['outbound'])}, "
            f"back {back_day} {_leg(it.get('inbound') or {})}."
        )
    cheapest = min(items, key=lambda i: _num(i, "price"))
    shortest = min(items, key=lambda i: (_num(i, "totalDurationSeconds"), _num(i, "price")))
    head = (
        f"Kiwi.com found {len(items)} return trips from Singapore to Tokyo for {TRAVELLERS} people. "
        f"Here are {len(picked)} worth a look:\n\n"
    )
    tail = (
        f"\n\nThe cheapest is {_price(cheapest, currency)} in total on {_airlines(cheapest['outbound'])}. "
        + (
            "It is also the quickest. "
            if shortest is cheapest
            else f"The quickest takes {_hours(shortest.get('totalDurationSeconds'))} of travel there and "
            f"back, for {_price(shortest, currency)}. "
        )
        + "Prices are what Kiwi.com showed just now; each price links to its booking on Kiwi.com."
    )
    return head + "\n".join(rows) + tail


def _flex_reply(ctx: dict[str, Any]) -> str:
    items, currency = _itineraries("flex")
    before, _ = _itineraries("first")
    if not items:
        return "Kiwi.com returned no flights for a day either side, so the dates you have are the ones to book."
    by_dates: dict[tuple[str, str], dict[str, Any]] = {}
    for it in items:
        key = (
            _day(it["outbound"].get("departureTime")),
            _day((it.get("inbound") or {}).get("departureTime")),
        )
        if key not in by_dates or _num(it, "price") < _num(by_dates[key], "price"):
            by_dates[key] = it
    best = sorted(by_dates.items(), key=lambda kv: _num(kv[1], "price"))[:4]
    rows = [
        f"| Fly out | Fly back | Total for {TRAVELLERS} |",
        "|---|---|---|",
    ]
    for (out_day, back_day), it in best:
        link = it.get("bookingUrl")
        rows.append(f"| {out_day} | {back_day} | {_booked(it, currency, link)} |")
    cheapest = best[0][1]
    note = ""
    if before:
        was = min(before, key=lambda i: _num(i, "price"))
        diff = _num(was, "price") - _num(cheapest, "price")
        if diff > 0 and diff != float("inf"):
            note = (
                f" That is {diff:.0f} {currency} less than the cheapest trip on your original dates "
                f"({_price(was, currency)})."
            )
        elif diff <= 0:
            note = " Your original dates are already the cheapest."
    return (
        "With a day of give either way, these are the cheapest date pairs Kiwi.com returned "
        "(each price links to its booking):\n\n"
        + "\n".join(rows)
        + f"\n\nBest: out {best[0][0][0]}, back {best[0][0][1]}, on {_airlines(cheapest['outbound'])}, "
        f"{_price(cheapest, currency)} in total." + note
    )


TEAM = {
    "default_agent": "sora",
    "agents": [
        {
            "name": "sora",
            "emoji": "🪁",
            "description": "Office, travel and events for the studio.",
            "title": "Studio Operations",
            "manager": None,
        },
    ],
    "files": {
        "sora": {"AGENTS.md": "# sora\n\nStudio operations at Paper Kite Games, Singapore.\n"},
    },
    "turns": {
        "sora": [
            {
                "match": "a day",
                "steps": [
                    {"wait": 0.4},
                    {
                        "tool": "search-flight",
                        "args": FLEX,
                        "result": _search("flex", FLEX),
                        "took": 0.4,
                    },
                    {"text": _flex_reply, "wps": 60},
                ],
            },
            {
                "steps": [
                    {"wait": 0.4},
                    {
                        "tool": "search-flight",
                        "args": FIRST,
                        "result": _search("first", FIRST),
                        "took": 0.4,
                    },
                    {"text": _first_reply, "wps": 60},
                ],
            },
        ],
    },
}
