"""Append-only ledger of per-call NON-model service usage.

The sibling of :mod:`olano.analytics.usage_ledger`. That ledger records LLM
calls, whose unit of account is tokens; this one records the services an agent
spends money on that produce no tokens at all:

- **image** — image generation (``image_generate``)
- **vision** — image understanding (``image_describe``; token-based under the
  hood, but ``olano:vision`` is a gateway utility group, not a chat tier, so
  the usage ledger would misprice it — it belongs with the services, which is
  also where the cloud's own per-service accounting puts it)
- **tts** / **stt** — speech synthesis / transcription (:mod:`olano.speech`)
- **web_search** / **web_fetch** — the managed search/extract services
- **linkedin**: managed LinkedIn outreach (profile visits, follows,
  connection requests and the monthly account seat)

On a managed deployment these all bill in **olano credits** through the cloud
(the gateway for image/speech, ``/api/v1/services/*`` for web), which is why
they belong on the dashboard next to model spend: a credits account that spends
a third of its balance on image generation reads as unexplained drift without
them. On a BYOK deployment the same tools run against the operator's own
provider key — Olano sells nothing, so the row carries no charge and only the
call itself is counted.

``billing_source`` mirrors the LLM ledger's vocabulary — ``"olano"`` when Olano
paid, ``"customer"`` for BYOK — but is **stored**, not derived: unlike a model
string, "which provider served this TTS call" is not recoverable from the row
after the fact.

Each line at ``<home>/analytics/services.jsonl``::

    {
        "ts": "2026-08-12T12:34:56.789+00:00",
        "agent": "merli",
        "service": "tts",
        "provider": "olano",
        "billing_source": "olano",
        "units": 1,
        "credits": 0.4,  # managed calls only
    }

Writes never raise — accounting must never break a tool call.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Canonical service names. Anything else is recorded verbatim (a new managed
# service must show up in the dashboard before this list is updated, not after).
SERVICES: tuple[str, ...] = (
    "image",
    "vision",
    "tts",
    "stt",
    "web_search",
    "web_fetch",
    "linkedin",
)


@dataclass(frozen=True)
class ServiceEvent:
    """One recorded non-model service call."""

    ts: str
    agent: str
    service: str
    provider: str
    billing_source: str
    units: int = 1
    # Olano credits charged. None for BYOK calls — which is not the same as
    # 0.0: "Olano sold nothing here" versus "a free managed call". The web
    # services get it from the cloud response body (`credits_charged`); the
    # gateway-billed calls (image, vision, tts, stt) read it off the response's
    # cost headers (`usage_recorder.credits_from_headers`) — these are plain
    # non-streamed HTTP calls, so the header is present. A None on a managed
    # row means the header was missing that once, not a policy.
    credits: float | None = None

    @property
    def date(self) -> str:
        """Calendar date (UTC) of the event, ``YYYY-MM-DD``."""
        return self.ts[:10]

    def to_json(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "ts": self.ts,
            "agent": self.agent,
            "service": self.service,
            "provider": self.provider,
            "billing_source": self.billing_source,
            "units": self.units,
            **({"credits": self.credits} if self.credits is not None else {}),
        }

    @classmethod
    def from_json(cls, raw: dict) -> ServiceEvent | None:
        try:
            return cls(
                ts=str(raw["ts"]),
                agent=str(raw.get("agent") or "unknown"),
                service=str(raw.get("service") or "unknown"),
                provider=str(raw.get("provider") or "unknown"),
                billing_source=(
                    "olano" if str(raw.get("billing_source") or "") == "olano" else "customer"
                ),
                units=int(raw.get("units") or 1),
                credits=(float(raw["credits"]) if raw.get("credits") is not None else None),
            )
        except (KeyError, TypeError, ValueError):
            return None


def service_ledger_path() -> Path:
    return get_olano_paths().service_ledger_path


def record_service_event(
    service: str,
    *,
    provider: str,
    managed: bool,
    agent: str | None = None,
    units: int = 1,
    credits: float | None = None,
    ts: str | None = None,
) -> None:
    """Append one service-usage event. Best-effort — never raises.

    ``agent`` defaults to the agent bound for the current turn, so tool call
    sites don't have to thread it through. ``managed`` says who paid: True when
    the call went through Olano's cloud (gateway or ``/api/v1/services/*``).
    """
    try:
        if not agent:
            try:
                from olano.tools.talk_to import current_agent

                agent = current_agent.get() or ""
            except Exception:
                agent = ""
        if agent:
            try:
                # Inside a cognition worker the bound agent is the ephemeral
                # child (`merli__cog__pulse_agent__ab12cd`) — file the row
                # under its owner. Identity for real agent names.
                from olano.analytics.threadcost.resolver import owning_agent_of

                agent = owning_agent_of(agent)
            except Exception:
                pass
        path = service_ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        event = ServiceEvent(
            ts=ts or datetime.now(timezone.utc).isoformat(),
            agent=agent or "unknown",
            service=service or "unknown",
            provider=provider or "unknown",
            billing_source="olano" if managed else "customer",
            units=max(1, int(units or 1)),
            credits=float(credits) if credits is not None else None,
        )
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.to_json(), ensure_ascii=False) + "\n")
    except Exception:  # pragma: no cover - defensive
        logger.debug("service_ledger: record failed", exc_info=True)


def iter_service_events(
    *, since: str | None = None, path: Path | None = None
) -> Iterator[ServiceEvent]:
    """Yield parsed events, optionally filtered to ``ts >= since``.

    ``since`` is compared lexicographically against the ISO timestamp, which is
    correct for ISO-8601 (and for plain ``YYYY-MM-DD`` date bounds).
    """
    from olano.analytics.usage_ledger import _line_ts_before

    p = path or service_ledger_path()
    if not p.exists():
        return
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                # Same raw-line window prefilter as the usage ledger — rows
                # serialize through json.dumps with `"ts": "` verbatim, and
                # the post-parse check below stays the authority.
                if since is not None and _line_ts_before(line, since):
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(raw, dict):
                    continue
                event = ServiceEvent.from_json(raw)
                if event is None:
                    continue
                if since is not None and event.ts < since:
                    continue
                yield event
    except OSError:
        return


def read_service_events(
    *, since: str | None = None, path: Path | None = None
) -> list[ServiceEvent]:
    """Eager list form of :func:`iter_service_events`."""
    return list(iter_service_events(since=since, path=path))
