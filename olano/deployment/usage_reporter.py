"""Background reporter that ships per-call usage to the cloud control plane.

The cloud management dashboard (cloud.olano.ai → Usage) renders day-partitioned
rollups fed exclusively by ``POST {cloud}/api/v1/usage`` — batched events
authenticated by the per-deployment runtime token (``olano_dep_…``, the same
credential the signed-settings pull uses). This module is the instance-side
client of that contract:

- **Source**: the append-only usage ledger (``<home>/analytics/usage.jsonl``,
  written per turn by ``core/runner._record_usage``). Each ledger line becomes
  up to three cloud events: ``tokens_input``, ``tokens_output``, ``request``
  (one per turn), carrying ``provider``/``model`` split from the
  ``provider:model`` string, ``occurred_at``, and ``{agent, usd,
  cache_read_tokens}`` metadata.
- **Durable cursor**: a byte offset into the ledger persisted at
  ``<home>/deployment/usage_report_cursor.json`` — only bytes past the cursor
  are ever shipped, so restarts neither drop nor double-send. A truncated /
  replaced ledger resets the cursor to the file's end (report forward, never
  re-bill history).
- **Idempotency**: the ``Idempotency-Key`` header is derived from the byte
  range (``<start>-<end>-<n>``), so a retry after a network failure is a
  server-side no-op (first writer wins per deployment).
- **Failure posture**: transient errors (network / 5xx / 429) leave the cursor
  in place and retry on the next tick; ``401/403`` (revoked token) logs and
  backs off; a ``422`` (schema-rejected batch) is logged loudly and **skipped**
  so one poison line can never wedge the pipeline. Reporting must never affect
  the runtime — every failure is caught.

Lifecycle mirrors :class:`olano.deployment.poller.SettingsPoller`
(``start()``/``stop()``, a tracked asyncio task, jittered interval). Enabled
whenever a deployment token is configured; kill-switch via
``LIONCLAW_USAGE_REPORT=0``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from olano.deployment.sources import CloudConfig, cloud_settings_config

logger = logging.getLogger(__name__)

USAGE_PATH = "/api/v1/usage"
CURSOR_FILE = "usage_report_cursor.json"

DEFAULT_INTERVAL_SECONDS = 5 * 60
MIN_INTERVAL_SECONDS = 60
INTERVAL_ENV = "LIONCLAW_USAGE_REPORT_INTERVAL"
DISABLE_ENV = "LIONCLAW_USAGE_REPORT"

POST_TIMEOUT_SECONDS = 15.0
# Cloud BodySchema caps a batch at 1000 events; ≤4 events per ledger line
# (tokens_input, tokens_output, ai_credit for Olano-managed turns, request) —
# so 200 lines stays comfortably under it.
_LEDGER_LINES_PER_BATCH = 200
# Bound how much catch-up a single pass performs (a huge backlog spreads over
# successive ticks instead of monopolizing one).
_MAX_BATCHES_PER_PASS = 20


def reporting_enabled() -> bool:
    """True when a deployment token exists and the kill-switch is not set."""
    if os.environ.get(DISABLE_ENV, "").strip().lower() in ("0", "false", "no", "off"):
        return False
    return cloud_settings_config() is not None


def _cursor_path(home: str | Path | None = None) -> Path:
    from olano.config.home import get_olano_paths

    d = get_olano_paths(home).deployment_dir
    d.mkdir(parents=True, exist_ok=True)
    return d / CURSOR_FILE


def _read_cursor(home: str | Path | None = None) -> int:
    try:
        raw = json.loads(_cursor_path(home).read_text(encoding="utf-8"))
        return max(0, int(raw.get("offset") or 0))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0


def _write_cursor(offset: int, home: str | Path | None = None) -> None:
    path = _cursor_path(home)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"offset": int(offset)}), encoding="utf-8")
    tmp.replace(path)


def _occurred_at(ts: str) -> str | None:
    """Normalise a ledger timestamp to the Z-suffixed RFC3339 the ingest accepts.

    The cloud validates ``occurred_at`` with a UTC-only datetime schema, so the
    ledger's ``+00:00`` suffix must become ``Z``; anything unparseable is omitted
    (the server stamps ``now()``).
    """
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _events_for_line(raw: dict) -> list[dict[str, Any]]:
    """Map one usage-ledger line to cloud usage events (empty when unusable)."""
    # Backfilled rows are reconstructed HISTORY (analytics/backfill.py): the
    # gateway already metered and billed that spend when it happened, so
    # re-shipping it would double the cloud's Reported Activity. Local-only.
    if raw.get("backfill"):
        return []
    from olano.analytics.usage_ledger import UsageEvent

    ev = UsageEvent.from_json(raw)
    if ev is None:
        return []
    provider, _, model = ev.model.partition(":")
    if not model:  # bare model string with no provider prefix
        provider, model = "", ev.model
    base: dict[str, Any] = {
        "metadata": {
            "agent": ev.agent,
            "usd": ev.usd,
            "cache_read_tokens": ev.cache_read_tokens,
            # Billing attribution rides METADATA, not a new event kind: the
            # cloud's kind enum is closed and a 422-rejected batch is skipped
            # permanently (see the 422 handler below), so an unknown kind
            # during rollout skew would silently drop billing data.
            "billing_source": ev.billing_source,
            **({"credits": ev.credits} if ev.credits is not None else {}),
            # What kind of work spent it — lets the cloud's Reported Activity
            # split agent turns from cortex/highlights/etc. like the box does.
            **({"origin": ev.origin} if ev.origin else {}),
            "calls": ev.calls,
        },
    }
    if provider:
        base["provider"] = provider[:64]
    if model:
        base["model"] = model[:128]
    occurred = _occurred_at(ev.ts)
    if occurred:
        base["occurred_at"] = occurred
    events: list[dict[str, Any]] = []
    if ev.input_tokens > 0:
        events.append({**base, "kind": "tokens_input", "quantity": ev.input_tokens})
    if ev.output_tokens > 0:
        events.append({**base, "kind": "tokens_output", "quantity": ev.output_tokens})
    # Olano-managed turns also report the gateway's charge as its own metered
    # quantity — `ai_credit` is already in the cloud's accepted-kind enum and
    # the customer Usage page already tiles it. (The gateway's spend-log sync
    # deliberately does NOT write usage rollups, so this is the one feed and
    # never double counts.)
    if ev.credits:
        events.append({**base, "kind": "ai_credit", "quantity": ev.credits})
    # One "request" per ledger line = one completed agent turn.
    events.append({**base, "kind": "request", "quantity": 1})
    return events


@dataclass
class ShipResult:
    """Outcome of one :func:`ship_pending_usage` pass (for logs/tests)."""

    shipped_events: int = 0
    batches: int = 0
    pending: bool = False  # more data remained (batch cap) or a transient error
    error: str = ""


def _read_batch(path: Path, offset: int) -> tuple[list[str], int]:
    """Complete ledger lines after ``offset`` (bounded), and the end offset.

    Only fully-terminated lines are consumed — a partially-written last line
    stays for the next pass.
    """
    with path.open("rb") as fh:
        fh.seek(offset)
        chunk_lines: list[str] = []
        end = offset
        for _ in range(_LEDGER_LINES_PER_BATCH):
            line = fh.readline()
            if not line or not line.endswith(b"\n"):
                break
            end += len(line)
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                chunk_lines.append(text)
    return chunk_lines, end


def ship_pending_usage(
    home: str | Path | None = None,
    *,
    config: CloudConfig | None = None,
    transport: httpx.BaseTransport | None = None,
) -> ShipResult:
    """Ship unreported ledger events to the cloud ingest. Never raises.

    ``config``/``transport`` are injectable for tests; by default the cloud
    channel config (env/vault deployment token) and a real HTTP transport are
    used.
    """
    result = ShipResult()
    try:
        cfg = config if config is not None else cloud_settings_config()
        if cfg is None:
            return result
        from olano.config.home import get_olano_paths

        ledger = get_olano_paths(home).usage_ledger_path
        if not ledger.exists():
            return result

        offset = _read_cursor(home)
        size = ledger.stat().st_size
        if offset > size:
            # Ledger was truncated/replaced — report forward from here, never
            # re-bill history under recycled idempotency keys.
            logger.info(
                "usage-reporter: ledger shrank (%d -> %d bytes); resetting cursor", offset, size
            )
            _write_cursor(size, home)
            return result

        url = f"{cfg.url}{USAGE_PATH}"
        headers = {"Authorization": f"Bearer {cfg.token}"}
        client_kwargs: dict[str, Any] = {"timeout": POST_TIMEOUT_SECONDS}
        if transport is not None:
            client_kwargs["transport"] = transport

        with httpx.Client(**client_kwargs) as client:
            for _ in range(_MAX_BATCHES_PER_PASS):
                lines, end = _read_batch(ledger, offset)
                if not lines or end == offset:
                    break
                events: list[dict[str, Any]] = []
                for text in lines:
                    try:
                        raw = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(raw, dict):
                        events.extend(_events_for_line(raw))
                if not events:
                    # Nothing usable in this byte range — just advance past it.
                    _write_cursor(end, home)
                    offset = end
                    continue
                idem = f"{offset}-{end}-{len(events)}"
                try:
                    resp = client.post(
                        url,
                        headers={**headers, "Idempotency-Key": idem},
                        json={"events": events},
                    )
                except httpx.HTTPError as exc:
                    result.pending = True
                    result.error = f"network: {exc}"
                    break
                if resp.status_code in (401, 403):
                    result.pending = True
                    result.error = f"unauthorized (HTTP {resp.status_code})"
                    logger.warning(
                        "usage-reporter: deployment token rejected (HTTP %d) — "
                        "usage will retry; check the deployment token",
                        resp.status_code,
                    )
                    break
                if resp.status_code == 422:
                    # Schema-rejected batch: skipping it loses those lines but a
                    # poison batch must never wedge the pipeline. Log loudly.
                    logger.error(
                        "usage-reporter: cloud rejected a usage batch (422): %s — skipping",
                        resp.text[:500],
                    )
                    _write_cursor(end, home)
                    offset = end
                    continue
                if resp.status_code not in (200, 202):
                    result.pending = True
                    result.error = f"HTTP {resp.status_code}"
                    break
                _write_cursor(end, home)
                offset = end
                result.shipped_events += len(events)
                result.batches += 1
            else:
                result.pending = True  # batch cap hit with backlog remaining
        if result.shipped_events:
            logger.info(
                "usage-reporter: shipped %d event(s) in %d batch(es)%s",
                result.shipped_events,
                result.batches,
                " (backlog remains)" if result.pending else "",
            )
    except Exception as exc:  # pragma: no cover - reporting must never break the runtime
        logger.exception("usage-reporter: pass failed")
        result.pending = True
        result.error = f"{type(exc).__name__}: {exc}"
    return result


class UsageReporter:
    """Periodically ships the usage ledger to the cloud ingest endpoint.

    Lifecycle mirrors :class:`olano.deployment.poller.SettingsPoller` —
    ``start()``/``stop()``, one tracked asyncio task, never crashes the runtime.
    """

    def __init__(self, ship: Callable[[], ShipResult] | None = None) -> None:
        # Injectable for tests; the default ships the real ledger.
        self._ship = ship if ship is not None else ship_pending_usage
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="deployment_usage_reporter")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    @staticmethod
    def _interval() -> float:
        raw = os.environ.get(INTERVAL_ENV, "").strip()
        seconds = float(raw) if raw.replace(".", "", 1).isdigit() else DEFAULT_INTERVAL_SECONDS
        seconds = max(seconds, MIN_INTERVAL_SECONDS)
        # Jitter so a fleet doesn't thundering-herd the ingest endpoint.
        return seconds * random.uniform(0.9, 1.1)

    async def _loop(self) -> None:
        while True:
            try:
                if reporting_enabled():
                    await asyncio.to_thread(self._ship)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive
                logger.exception("usage-reporter: tick failed")
            await asyncio.sleep(self._interval())
