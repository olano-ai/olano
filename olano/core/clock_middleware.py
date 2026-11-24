"""Per-request date awareness: today's date and weekday on every model call.

The system prompt is assembled when an agent boots and refreshed by a sweep
that diffs its stable sections, so it never carried the date; the only
date-shaped facts a model saw were the daily-log file names and whatever its
own files mentioned. An autonomous run with a weekday rule in its brief then
had to guess, and on 2026-11-02 one guessed Saturday on a Monday and skipped
its work. The scheduler now stamps every autonomous prompt with the full
time (``scheduler/clock.run_stamp``); this middleware is the belt for every
other turn, chat included.

Day granularity, appended at the end of the system message, on purpose: the
block changes once per UTC day, so a provider's prefix cache of the system
prompt is invalidated once a day instead of on every call in a new minute.
The time of day is one ``get_current_time`` call away and the block says so.

``LIONCLAW_CLOCK_STAMP=0`` removes the middleware (a test that pins a system
prompt byte for byte, or an operator who prefers the old behaviour).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from langchain.agents.middleware import AgentMiddleware

from olano.scheduler.clock import date_section

logger = logging.getLogger(__name__)

STAMP_ENV = "LIONCLAW_CLOCK_STAMP"


def clock_stamp_enabled(env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return (source.get(STAMP_ENV) or "1").strip().lower() not in ("0", "false", "no", "off")


class ClockMiddleware(AgentMiddleware):
    """Append ``## Current date`` to the system message of every model request."""

    def _inject(self, request: Any) -> Any:
        try:
            from deepagents.middleware._utils import append_to_system_message

            section = date_section()
            return request.override(
                system_message=append_to_system_message(request.system_message, section)
            )
        except Exception:
            logger.debug("clock stamp injection failed", exc_info=True)
            return request

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(self._inject(request))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(self._inject(request))


def build_clock_middleware() -> list[AgentMiddleware]:
    """The middleware, or nothing when the stamp is switched off."""
    if not clock_stamp_enabled():
        return []
    return [ClockMiddleware()]
