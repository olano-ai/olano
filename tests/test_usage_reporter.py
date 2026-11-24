"""Tests for the instance→cloud usage reporter (deployment/usage_reporter.py).

The cloud usage dashboard is fed exclusively by ``POST {cloud}/api/v1/usage``;
these pin the instance-side client: ledger→event mapping, the durable byte
cursor (no drops, no double-sends), idempotency keys, and the failure posture
(transient errors retry, poison batches are skipped, revoked tokens back off).
"""

from __future__ import annotations

import json

import httpx
import pytest

from olano.analytics.usage_ledger import record_usage_event
from olano.deployment import usage_reporter
from olano.deployment.sources import CloudConfig

_CFG = CloudConfig(url="https://cloud.example", token="olano_dep_test")


class _Capture:
    """MockTransport handler capturing requests and serving scripted responses."""

    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.requests: list[dict] = []
        self._responses = responses or []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {
                "url": str(request.url),
                "auth": request.headers.get("authorization"),
                "idempotency": request.headers.get("idempotency-key"),
                "body": json.loads(request.content.decode("utf-8")),
            }
        )
        if self._responses:
            return self._responses.pop(0)
        return httpx.Response(202, json={"status": "ok"})


def _ship(capture: _Capture) -> usage_reporter.ShipResult:
    return usage_reporter.ship_pending_usage(config=_CFG, transport=httpx.MockTransport(capture))


def _seed_ledger(n: int = 1, **overrides) -> None:
    for _ in range(n):
        record_usage_event(
            agent=overrides.get("agent", "merli"),
            model=overrides.get("model", "openai-codex:gpt-5.6-luna"),
            input_tokens=overrides.get("input_tokens", 1000),
            output_tokens=overrides.get("output_tokens", 200),
            usd=overrides.get("usd", 0.01),
            cache_read_tokens=overrides.get("cache_read_tokens", 50),
        )


def test_disabled_without_token(monkeypatch) -> None:
    monkeypatch.delenv("LIONCLAW_DEPLOYMENT_TOKEN", raising=False)
    assert usage_reporter.reporting_enabled() is False
    monkeypatch.setenv("LIONCLAW_DEPLOYMENT_TOKEN", "olano_dep_x")
    assert usage_reporter.reporting_enabled() is True
    monkeypatch.setenv("LIONCLAW_USAGE_REPORT", "0")
    assert usage_reporter.reporting_enabled() is False


def test_no_ledger_ships_nothing() -> None:
    capture = _Capture()
    result = _ship(capture)
    assert result.shipped_events == 0
    assert capture.requests == []


def test_ships_ledger_events_with_contract_shape() -> None:
    _seed_ledger(1)
    capture = _Capture()
    result = _ship(capture)

    assert result.shipped_events == 3 and result.batches == 1
    req = capture.requests[0]
    assert req["url"] == "https://cloud.example/api/v1/usage"
    assert req["auth"] == "Bearer olano_dep_test"
    assert req["idempotency"]

    events = req["body"]["events"]
    by_kind = {e["kind"]: e for e in events}
    assert by_kind["tokens_input"]["quantity"] == 1000
    assert by_kind["tokens_output"]["quantity"] == 200
    assert by_kind["request"]["quantity"] == 1
    for e in events:
        assert e["provider"] == "openai-codex"
        assert e["model"] == "gpt-5.6-luna"
        assert e["metadata"]["agent"] == "merli"
        assert e["metadata"]["cache_read_tokens"] == 50
        # The ingest schema only accepts Z-suffixed UTC datetimes.
        assert e["occurred_at"].endswith("Z") and "+00:00" not in e["occurred_at"]


def test_cursor_advances_and_prevents_resend() -> None:
    _seed_ledger(2)
    first = _Capture()
    assert _ship(first).shipped_events == 6

    # Nothing new → nothing shipped.
    second = _Capture()
    assert _ship(second).shipped_events == 0
    assert second.requests == []

    # New events after the cursor → only the new ones ship.
    _seed_ledger(1)
    third = _Capture()
    assert _ship(third).shipped_events == 3


def test_transient_failure_keeps_cursor_and_retries_same_key() -> None:
    _seed_ledger(1)
    failing = _Capture(responses=[httpx.Response(500)])
    result = _ship(failing)
    assert result.shipped_events == 0 and result.pending is True

    retry = _Capture()
    assert _ship(retry).shipped_events == 3
    # Identical byte range → identical idempotency key (server-side no-op safe).
    assert retry.requests[0]["idempotency"] == failing.requests[0]["idempotency"]


def test_unauthorized_stops_pass_without_advancing() -> None:
    _seed_ledger(1)
    denied = _Capture(responses=[httpx.Response(401)])
    result = _ship(denied)
    assert result.pending is True and "unauthorized" in result.error
    # Cursor untouched — a later pass (fixed token) ships the events.
    assert _ship(_Capture()).shipped_events == 3


def test_poison_batch_is_skipped_not_wedged() -> None:
    _seed_ledger(1)
    rejecting = _Capture(responses=[httpx.Response(422, json={"error": "invalid_body"})])
    result = _ship(rejecting)
    assert result.shipped_events == 0 and result.pending is False
    # The rejected range was skipped: nothing re-ships.
    assert _ship(_Capture()).shipped_events == 0


def test_truncated_ledger_resets_cursor_forward() -> None:
    from olano.analytics.usage_ledger import usage_ledger_path

    _seed_ledger(2)
    assert _ship(_Capture()).shipped_events == 6

    # Simulate rotation: the file shrinks below the cursor.
    usage_ledger_path().write_text("", encoding="utf-8")
    reset = _Capture()
    assert _ship(reset).shipped_events == 0 and reset.requests == []

    # Events written after the reset ship normally (no historical re-bill).
    _seed_ledger(1)
    assert _ship(_Capture()).shipped_events == 3


def test_malformed_lines_are_skipped() -> None:
    from olano.analytics.usage_ledger import usage_ledger_path

    path = usage_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("not-json\n")
    _seed_ledger(1)
    capture = _Capture()
    assert _ship(capture).shipped_events == 3  # only the valid line's events


def test_model_without_provider_prefix() -> None:
    _seed_ledger(1, model="llama3")
    capture = _Capture()
    _ship(capture)
    e = capture.requests[0]["body"]["events"][0]
    assert e["model"] == "llama3"
    assert "provider" not in e


@pytest.mark.parametrize(
    ("ts", "expected_suffix"),
    [
        ("2026-07-18T11:53:08.770928+00:00", "Z"),
        ("2026-07-18T19:53:08+08:00", "Z"),
    ],
)
def test_occurred_at_normalized_to_utc_z(ts: str, expected_suffix: str) -> None:
    out = usage_reporter._occurred_at(ts)
    assert out is not None and out.endswith(expected_suffix)
    assert "+".join(out.split("+")[:1]) == out  # no offset form remains
    assert usage_reporter._occurred_at("garbage") is None


async def test_reporter_lifecycle_start_stop(monkeypatch) -> None:
    import asyncio

    calls: list[int] = []

    def _fake_ship() -> usage_reporter.ShipResult:
        calls.append(1)
        return usage_reporter.ShipResult()

    monkeypatch.setenv("LIONCLAW_DEPLOYMENT_TOKEN", "olano_dep_x")
    monkeypatch.setenv(usage_reporter.INTERVAL_ENV, "60")
    reporter = usage_reporter.UsageReporter(ship=_fake_ship)
    reporter.start()
    try:
        await asyncio.sleep(0.05)  # first tick runs immediately
        assert calls
    finally:
        reporter.stop()


def test_token_flows_from_bootstrap_env_file(tmp_path, monkeypatch) -> None:
    """The provisioning chain end-to-end (instance side): cloud-init writes the
    token into the machine bootstrap env file (/opt/olano/olano.env — here via
    the LIONCLAW_BOOTSTRAP_ENV override), every olano entrypoint overlays it
    onto os.environ at startup, and the usage reporter turns on from it — no
    manual configuration."""
    import os

    from olano.config.bootstrap_env import load_bootstrap_env

    monkeypatch.delenv("LIONCLAW_DEPLOYMENT_TOKEN", raising=False)
    assert usage_reporter.reporting_enabled() is False

    env_file = tmp_path / "olano.env"
    env_file.write_text(
        "# Machine bootstrap env — written by cloud-init\n"
        "LIONCLAW_CLOUD_API_URL=https://cloud.olano.ai\n"
        "LIONCLAW_DEPLOYMENT_TOKEN=olano_dep_bootstrap\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LIONCLAW_BOOTSTRAP_ENV", str(env_file))
    try:
        applied = load_bootstrap_env()
        assert "LIONCLAW_DEPLOYMENT_TOKEN" in applied
        assert usage_reporter.reporting_enabled() is True
    finally:
        os.environ.pop("LIONCLAW_DEPLOYMENT_TOKEN", None)
        os.environ.pop("LIONCLAW_CLOUD_API_URL", None)


def test_diagnostics_exposes_cloud_reporting(monkeypatch) -> None:
    """`GET /api/analytics/threadcost/diagnostics` must say whether the
    deployment token landed and how much ledger backlog remains unshipped."""
    from olano.analytics.threadcost import service

    monkeypatch.delenv("LIONCLAW_DEPLOYMENT_TOKEN", raising=False)
    diag = service.diagnostics()
    cr = diag["cloud_reporting"]
    assert cr["enabled"] is False and cr["token_present"] is False

    monkeypatch.setenv("LIONCLAW_DEPLOYMENT_TOKEN", "olano_dep_x")
    _seed_ledger(1)
    cr = service.diagnostics()["cloud_reporting"]
    assert cr["enabled"] is True and cr["token_present"] is True
    assert cr["backlog_bytes"] > 0  # seeded but unshipped
    assert cr["cloud_api_url"].startswith("https://")


def test_backfill_rows_never_ship_to_the_cloud() -> None:
    """The money test: a backfill ledger row reconstructs spend the gateway
    already billed when it happened — shipping it would charge it twice."""
    record_usage_event(
        agent="merli",
        model="olano:pro",
        input_tokens=1000,
        output_tokens=200,
        usd=0.0,
        credits=5.0,
        origin="cognition",
        backfill=True,
        src="cog::merli__cog__ideation__ab12cd",
    )
    capture = _Capture()
    result = _ship(capture)
    assert result.shipped_events == 0
    assert capture.requests == []
    # And the pass isn't wedged: a later live row still ships.
    _seed_ledger(1)
    assert _ship(_Capture()).shipped_events == 3


def test_origin_and_calls_ride_the_event_metadata() -> None:
    record_usage_event(
        agent="merli",
        model="olano:pro",
        input_tokens=10,
        output_tokens=5,
        usd=0.0,
        credits=0.5,
        calls=4,
        origin="cognition",
    )
    capture = _Capture()
    _ship(capture)
    for event in capture.requests[0]["body"]["events"]:
        assert event["metadata"]["origin"] == "cognition"
        assert event["metadata"]["calls"] == 4
