"""Usage analytics, work journal, and cross-agent metric rollups.

This package turns the raw data Olano already produces — the per-call LLM
usage ledger, session transcripts, and the A2A activity log — into the
aggregates the dashboard's Insights view renders, plus a daily *work journal*
(deterministic metrics + optional LLM highlights) that summarises what the
agents have been doing.

Layers:

- :mod:`olano.analytics.usage_ledger` — append-only event log of every LLM
  call (agent, model, tokens, USD, timestamp). Written from
  ``core/runner._record_usage`` so it captures *every* turn regardless of which
  workspace/agent produced it.
- :mod:`olano.analytics.service_ledger` — the same for the non-model services
  an agent spends credits on (image generation, TTS/STT, managed web
  search/fetch). Written at each tool's own chokepoint, since none of them
  passes through the runner's usage accounting.
- :mod:`olano.analytics.aggregate` — pure rollups over the ledger, session
  JSONL, and the A2A log: tokens/$ by agent, $ by model, daily time series
  (with cumulative), tool calls + tool usage per agent, and A2A comms.
- :mod:`olano.analytics.journal` — per-day journal JSON builder and reader.
"""

from __future__ import annotations

from olano.analytics.service_ledger import (
    ServiceEvent,
    iter_service_events,
    read_service_events,
    record_service_event,
)
from olano.analytics.usage_ledger import (
    UsageEvent,
    iter_usage_events,
    read_usage_events,
    record_usage_event,
)

__all__ = [
    "ServiceEvent",
    "UsageEvent",
    "iter_service_events",
    "iter_usage_events",
    "read_service_events",
    "read_usage_events",
    "record_service_event",
    "record_usage_event",
]
