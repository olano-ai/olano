"""Current date/time tool for agents."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Annotated

from langchain_core.tools import tool

# Cap on a single wait_n_seconds call. The point of this tool is to bridge
# brief gaps in an a2a polling pattern — not to schedule long-running work.
# Anything longer should go through the scheduler (`scheduler_create`),
# which survives runtime restarts and shows up in the dashboard.
_MAX_WAIT_SECONDS = 300


@tool
def get_current_time(
    timezone_name: Annotated[
        str,
        "IANA timezone name (e.g. 'Asia/Singapore', 'America/New_York', 'Europe/London', 'UTC'). "
        "Leave empty to get both UTC and local system time.",
    ] = "",
) -> str:
    """
    Get the current date and time in UTC and the local system timezone (or a specified timezone).
    Always call this before creating scheduled tasks so you can compute the correct run_at time.
    Returns time in a format ready to use as run_at (e.g. '2026-05-16T10:05:00').
    """
    utc_now = datetime.now(timezone.utc)

    lines = [f"UTC:       {utc_now.strftime('%Y-%m-%d %H:%M:%S UTC')}"]

    requested = timezone_name.strip()
    if requested:
        try:
            from zoneinfo import ZoneInfo

            tz = ZoneInfo(requested)
            local_now = utc_now.astimezone(tz)
            lines.append(f"{requested}: {local_now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
            lines.append(f"run_at (local): {local_now.strftime('%Y-%m-%dT%H:%M:%S')}")
        except Exception:
            lines.append(
                f"Error: unknown timezone '{requested}'. "
                "Use IANA names like 'Asia/Singapore', 'America/New_York', 'Europe/London'."
            )
    else:
        local_now = utc_now.astimezone()
        tz_label = local_now.strftime("%Z")
        lines.append(f"Local ({tz_label}): {local_now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}")
        lines.append(f"run_at (UTC):   {utc_now.strftime('%Y-%m-%dT%H:%M:%S')}")
        lines.append(f"run_at (local): {local_now.strftime('%Y-%m-%dT%H:%M:%S')}")

    return "\n".join(lines)


# Alias so agents can call either name
get_current_date = get_current_time


@tool
async def wait_n_seconds(
    seconds: Annotated[
        int,
        "How long to wait, in seconds. Clamped to the inclusive range [1, 300]; "
        "for longer delays, use `scheduler_create` instead so the work survives "
        "a runtime restart.",
    ],
    reason: Annotated[
        str,
        "One short sentence explaining why you're waiting. Surfaces in logs "
        "and helps debugging if a wait blocks longer than expected.",
    ] = "",
) -> str:
    """Pause for `seconds` seconds without blocking other agents or the event loop.

    This is the polling primitive for the common a2a pattern where a peer
    needs time to do work:

        1. You: `talk_to_agent(kevin, "place TSLA order")`
        2. kevin: "submitted; the broker takes ~10s to confirm"
        3. You: `wait_n_seconds(10, reason="broker fill")`
        4. You: `talk_to_agent(kevin, "did the TSLA order fill?")`

    Implementation detail: `await asyncio.sleep(...)` yields control to the
    event loop, so other agents' turns continue to make progress while you
    wait. Your own turn pauses — that is the intent of the tool.

    Rules:
      • Maximum 300 seconds per call. Longer waits should use the scheduler.
      • Provide a `reason` so logs are searchable when a wait surprises you.
      • Don't use this to "throttle" tool calls; if you find yourself
        sleeping between unrelated actions, that's a sign your control flow
        belongs in a scheduled job instead.
    """
    clamped = max(1, min(int(seconds), _MAX_WAIT_SECONDS))
    clamp_note = f" (clamped from {seconds})" if clamped != int(seconds) else ""
    await asyncio.sleep(clamped)
    suffix = f" — {reason}" if reason.strip() else ""
    return f"Waited {clamped}s{clamp_note}{suffix}"


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
