"""The Olano credits pipeline, end to end on the instance side.

The property under test: a STREAMED managed turn — which is every normal turn —
must capture the gateway's per-call charge and carry it, in the right unit, to
every consumer: the SSE usage frame, the per-thread state, the usage ledger,
the analytics rollups, the journal, and the cloud usage reporter. Before these
fixes a streamed olano turn recorded nothing at all (langchain attaches the
cost header to each sub-call's FIRST chunk and usage to its LAST, and the
stream loop only merged usage).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessageChunk

from olano.config.agent_config import AgentConfig
from olano.core.runner import AgentRunner


def _first_chunk(credits: str) -> AIMessageChunk:
    """A sub-call's first streamed chunk: headers, no usage (langchain shape)."""
    return AIMessageChunk(
        content="",
        response_metadata={"headers": {"x-olano-credits-charged": credits}},
    )


def _usage_chunk(inp: int, out: int) -> AIMessageChunk:
    """A sub-call's final streamed chunk: usage, no headers."""
    return AIMessageChunk(
        content="",
        usage_metadata={"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out},
    )


def _streaming_runner(base_config: AgentConfig, chunks: list[AIMessageChunk]) -> AgentRunner:
    agent = MagicMock()

    async def fake_stream(*args, **kwargs):
        for c in chunks:
            yield (c, {})

    agent.astream = fake_stream
    agent.aget_state = AsyncMock(return_value=MagicMock(interrupts=[]))
    agent.ainvoke = AsyncMock()
    return AgentRunner(agent, base_config)


async def test_streamed_turn_accumulates_credits_across_sub_calls(
    base_config: AgentConfig,
) -> None:
    """Headers ride each sub-call's first chunk; usage its last. A tool-loop
    turn has several sub-calls — the frame must carry their SUM, in credits
    (not the internal micro counter), and the per-thread state must persist it
    so a reopened thread can still show what it cost."""
    base_config.model = "olano:pro"
    chunks = [
        _first_chunk("1.25"),
        AIMessageChunk(content="thinking… "),
        _usage_chunk(1000, 50),
        _first_chunk("0.75"),
        AIMessageChunk(content="done."),
        _usage_chunk(500, 20),
    ]
    runner = _streaming_runner(base_config, chunks)

    thread_id = str(uuid.uuid4())
    frames = [f async for f in runner.astream_events("Hello", thread_id=thread_id)]

    usage_frames = [payload for kind, payload in frames if kind == "usage"]
    assert len(usage_frames) == 1
    frame = usage_frames[0]
    assert frame["credits"] == pytest.approx(2.0)
    # The fixed-point accumulator is internal — leaking it would hand the
    # dashboard a mystery integer 1e6 times too large.
    assert "credits_micro" not in frame
    # Tokens merged from the usage chunks as before.
    assert frame["input_tokens"] == 1500
    assert frame["output_tokens"] == 70
    # olano:* has no USD price on this box — the frame must not carry a
    # fabricated near-zero dollar figure.
    assert "usd" not in frame

    # Per-thread persistence: credits are minable from no other store.
    state = runner.load_thread_state(thread_id)
    assert state["credits_total"] == pytest.approx(2.0)


async def test_streamed_byok_turn_carries_no_credits(base_config: AgentConfig) -> None:
    chunks = [AIMessageChunk(content="hi"), _usage_chunk(100, 10)]
    runner = _streaming_runner(base_config, chunks)
    thread_id = str(uuid.uuid4())
    frames = [f async for f in runner.astream_events("Hello", thread_id=thread_id)]
    usage_frames = [payload for kind, payload in frames if kind == "usage"]
    assert len(usage_frames) == 1
    assert "credits" not in usage_frames[0]
    # And no state write for a turn that charged nothing.
    assert "credits_total" not in runner.load_thread_state(thread_id)


async def test_header_casing_is_normalized(base_config: AgentConfig) -> None:
    """`_merge_olano_credits` must accept the raw header regardless of case —
    HTTP header casing is not guaranteed across client stacks."""
    base_config.model = "olano:turbo"
    chunks = [
        AIMessageChunk(
            content="x",
            response_metadata={"headers": {"X-Olano-Credits-Charged": "0.5"}},
        ),
        _usage_chunk(10, 5),
    ]
    runner = _streaming_runner(base_config, chunks)
    frames = [f async for f in runner.astream_events("Hi", thread_id=str(uuid.uuid4()))]
    usage = next(p for k, p in frames if k == "usage")
    assert usage["credits"] == pytest.approx(0.5)


async def test_gateway_cost_header_converts_credit_usd(
    base_config: AgentConfig, monkeypatch
) -> None:
    """The gateway reports `x-litellm-response-cost` in credit-USD (its
    model entries are priced in credit-USD by construction); credits = value ×
    the published rate. Header wins over the token-count estimate."""
    from olano.catalog import credits_registry

    monkeypatch.setattr(credits_registry, "credits_per_usd", lambda: 100.0)
    base_config.model = "olano:pro"
    chunks = [
        AIMessageChunk(
            content="x",
            response_metadata={"headers": {"x-litellm-response-cost": "0.021"}},
        ),
        _usage_chunk(10, 5),
    ]
    runner = _streaming_runner(base_config, chunks)
    frames = [f async for f in runner.astream_events("Hi", thread_id=str(uuid.uuid4()))]
    usage = next(p for k, p in frames if k == "usage")
    assert usage["credits"] == pytest.approx(2.1)


async def test_gateway_cost_header_is_ignored_for_byok(
    base_config: AgentConfig, monkeypatch
) -> None:
    """A customer pointing a BYOK provider at their own LiteLLM proxy must not
    have its cost telemetry converted into a fabricated olano-credits charge —
    the litellm header only counts on managed (olano:*) turns."""
    from olano.catalog import credits_registry

    monkeypatch.setattr(credits_registry, "credits_per_usd", lambda: 100.0)
    chunks = [
        AIMessageChunk(
            content="x",
            response_metadata={"headers": {"x-litellm-response-cost": "0.021"}},
        ),
        _usage_chunk(10, 5),
    ]
    runner = _streaming_runner(base_config, chunks)
    frames = [f async for f in runner.astream_events("Hi", thread_id=str(uuid.uuid4()))]
    usage = next(p for k, p in frames if k == "usage")
    assert "credits" not in usage


async def test_streamed_turn_without_headers_estimates_from_tokens(
    base_config: AgentConfig,
) -> None:
    """The gateway cannot put a stream's cost in its headers (they leave before
    the first token exists), so a streamed managed turn with no header at all
    must still charge, from token counts × the tier's credit rate. olano:pro
    bundled launch rate: 761.25 cr/1M input, 1522.5 cr/1M output."""
    base_config.model = "olano:pro"
    chunks = [
        AIMessageChunk(content="hello "),
        _usage_chunk(1000, 50),
        AIMessageChunk(content="world."),
        _usage_chunk(500, 20),
    ]
    runner = _streaming_runner(base_config, chunks)
    thread_id = str(uuid.uuid4())
    frames = [f async for f in runner.astream_events("Hi", thread_id=thread_id)]
    usage = next(p for k, p in frames if k == "usage")
    # 1500 × 761.25/1M + 70 × 1522.5/1M = 1.141875 + 0.106575
    assert usage["credits"] == pytest.approx(1.248450)
    assert runner.load_thread_state(thread_id)["credits_total"] == pytest.approx(1.248450)


def test_tier_rates_prefer_the_synced_catalog(monkeypatch) -> None:
    """The 0037 catalog rows carry metadata.credit_pricing; a synced reprice
    must win over the bundled launch table without a runtime update."""
    from olano.catalog import models_registry
    from olano.models.olano_gateway import estimate_credits_micro, tier_credit_rates

    monkeypatch.setattr(
        models_registry,
        "model_entry",
        lambda model_id: (
            {"metadata": {"credit_pricing": {"input": 600, "cached_input": 60, "output": 3000}}}
            if model_id == "olano:pro"
            else None
        ),
    )
    assert tier_credit_rates("pro") == {"input": 600.0, "cached_input": 60.0, "output": 3000.0}
    # turbo has no catalog row → bundled fallback still answers.
    assert tier_credit_rates("turbo") == {"input": 245.0, "cached_input": 4.9, "output": 490.0}
    # And the estimate uses the repriced table: 1000×600 + 100×3000.
    est = estimate_credits_micro("olano:pro", {"input_tokens": 1000, "output_tokens": 100})
    assert est == 900_000


def test_estimate_credits_micro_math() -> None:
    """Micro-credits = tokens × (credits per 1M): exact integers, cache reads
    billed at the cached rate, non-olano models always 0."""
    from olano.models.olano_gateway import estimate_credits_micro

    # pro (bundled launch rate): in 761.25 / cached 6.34375 / out
    # 1522.5 credits per 1M.
    usage = {"input_tokens": 1000, "output_tokens": 100, "cache_read": 400}
    est = estimate_credits_micro("olano:pro", usage)
    #   600×761.25 + 400×6.34375 + 100×1522.5 = 456750 + 2537.5 + 152250
    assert est == 611_538  # round() of 611537.5
    # cache_read can never exceed input_tokens (defensive clamp): all 10 input
    # tokens bill at the cached rate (10 × 6.34375 = 63.4375 → 63 micro).
    est = estimate_credits_micro("olano:pro", {"input_tokens": 10, "cache_read": 50})
    assert est == 63
    assert estimate_credits_micro("anthropic:claude-sonnet-4-6", usage) == 0
    assert estimate_credits_micro(None, usage) == 0
    assert estimate_credits_micro("olano:pro", {}) == 0


# ── analytics: the ledger feeds rollups with both books ──────────────────────


def _write_ledger_events() -> None:
    from olano.analytics.usage_ledger import record_usage_event

    record_usage_event(
        agent="merli",
        model="olano:pro",
        input_tokens=1000,
        output_tokens=100,
        usd=0.0,
        credits=2.5,
    )
    record_usage_event(
        agent="merli",
        model="anthropic:claude-sonnet-4-6",
        input_tokens=2000,
        output_tokens=200,
        usd=0.0123,
    )


def test_usage_rollup_splits_billing_sources() -> None:
    from olano.analytics.aggregate import usage_rollup

    _write_ledger_events()
    data = usage_rollup(days=7)

    assert data["totals"]["credits"] == pytest.approx(2.5)
    assert data["totals"]["usd"] == pytest.approx(0.0123)

    by_source = {r["source"]: r for r in data["by_source"]}
    assert by_source["olano"]["credits"] == pytest.approx(2.5)
    assert by_source["olano"]["usd"] == pytest.approx(0.0)
    assert by_source["customer"]["usd"] == pytest.approx(0.0123)
    assert by_source["customer"]["credits"] == pytest.approx(0.0)

    # Managed agents must not sink to the bottom as $0 rows.
    merli = next(r for r in data["by_agent"] if r["agent"] == "merli")
    assert merli["credits"] == pytest.approx(2.5)

    daily_with_spend = [d for d in data["daily"] if d["calls"]]
    assert daily_with_spend and daily_with_spend[-1]["credits"] == pytest.approx(2.5)
    assert data["daily"][-1]["cum_credits"] == pytest.approx(2.5)


def test_hourly_rollup_carries_credits_too() -> None:
    from olano.analytics.aggregate import usage_rollup_hourly

    _write_ledger_events()
    data = usage_rollup_hourly(hours=24)
    assert data["totals"]["credits"] == pytest.approx(2.5)
    assert {r["source"] for r in data["by_source"]} == {"olano", "customer"}


def test_threadcost_daily_view_grafts_ledger_credits() -> None:
    """The 30-day Insights view mines threads.db, which can never know the
    gateway charge — its credits (and the whole by_source dimension) must come
    from the ledger overlay, not silently render as zero."""
    from olano.analytics.threadcost import service

    _write_ledger_events()
    data = service.usage_rollup(days=7)
    assert data["totals"]["credits"] == pytest.approx(2.5)
    by_source = {r["source"]: r for r in data["by_source"]}
    assert by_source["olano"]["credits"] == pytest.approx(2.5)


def test_journal_day_carries_credits() -> None:
    from datetime import datetime, timezone

    from olano.analytics.journal import build_journal_day

    _write_ledger_events()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = build_journal_day(day)
    assert entry["metrics"]["credits"] == pytest.approx(2.5)
    merli = next(r for r in entry["by_agent"] if r["agent"] == "merli")
    assert merli["credits"] == pytest.approx(2.5)


# ── cloud usage reporter ─────────────────────────────────────────────────────


def test_reporter_ships_credits_in_metadata_and_as_ai_credit_event() -> None:
    from olano.deployment.usage_reporter import _events_for_line

    events = _events_for_line(
        {
            "ts": "2026-08-10T00:00:00+00:00",
            "agent": "merli",
            "model": "olano:pro",
            "input_tokens": 1000,
            "output_tokens": 100,
            "usd": 0.0,
            "credits": 2.5,
        }
    )
    kinds = [e["kind"] for e in events]
    assert kinds == ["tokens_input", "tokens_output", "ai_credit", "request"]
    credit_event = next(e for e in events if e["kind"] == "ai_credit")
    assert credit_event["quantity"] == 2.5
    assert credit_event["provider"] == "olano"
    for e in events:
        assert e["metadata"]["billing_source"] == "olano"
        assert e["metadata"]["credits"] == 2.5


def test_reporter_byok_line_has_no_credit_event() -> None:
    from olano.deployment.usage_reporter import _events_for_line

    events = _events_for_line(
        {
            "ts": "2026-08-10T00:00:00+00:00",
            "agent": "merli",
            "model": "anthropic:claude-sonnet-4-6",
            "input_tokens": 1000,
            "output_tokens": 100,
            "usd": 0.01,
        }
    )
    kinds = [e["kind"] for e in events]
    assert "ai_credit" not in kinds
    for e in events:
        assert e["metadata"]["billing_source"] == "customer"
        # Absent, not 0.0: "Olano sold nothing" ≠ "a free managed call".
        assert "credits" not in e["metadata"]


# ── tier metadata (static — the gateway never reveals the backing model) ─────


def test_tier_context_windows_are_conservative_floors() -> None:
    from olano.models.olano_gateway import tier_context_window

    assert tier_context_window("olano:pro") == 1_000_000
    assert tier_context_window("olano:max") == 1_000_000
    # The tier's retired id is not a tier at all any more.
    assert tier_context_window("olano:reasoning") is None
    # Since the 2026-08 re-route every tier's backends carry a 1M window, so
    # the floor is 1M throughout (it was 200K when turbo fell back to
    # claude-haiku). The value is a FLOOR, not the routed model's real window.
    assert tier_context_window("olano:turbo") == 1_000_000
    assert tier_context_window("olano:auto") == 1_000_000
    assert tier_context_window("olano:flash") == 1_000_000  # legacy alias
    assert tier_context_window("anthropic:claude-sonnet-4-6") is None


def test_context_report_resolves_olano_tiers() -> None:
    from olano.runtime.context_report import model_context_window

    assert model_context_window("olano:pro") == 1_000_000
    assert model_context_window("olano:auto") == 1_000_000


def test_reasoning_surface_knows_the_reasoning_tier() -> None:
    """All three consumers (info, resolution, kwarg gating) flow through the
    same probe. `reasoning` and `pro` expose the effort picker (both backends
    accept it — verified against the live gateway); `turbo`/`auto` stay
    knob-free."""
    from olano.models.reasoning import (
        gate_reasoning_kwargs,
        reasoning_effort_info,
        resolve_reasoning_effort,
    )

    info = reasoning_effort_info("olano:max")
    assert info is not None and info["levels"] == ["low", "medium", "high"]
    assert reasoning_effort_info("olano:reasoning") is None  # retired id
    pro = reasoning_effort_info("olano:pro")
    assert pro is not None and pro["levels"] == ["low", "medium", "high"]
    assert reasoning_effort_info("olano:turbo") is None

    # Unset effort ⇒ None (leave the model's thinking alone), valid ⇒ passes.
    assert resolve_reasoning_effort("olano:max", None) is None
    assert resolve_reasoning_effort("olano:max", "high") == "high"

    kwargs = gate_reasoning_kwargs("olano", "max", {"reasoning_effort": "high"})
    assert kwargs.get("reasoning_effort") == "high"
    kwargs = gate_reasoning_kwargs("olano", "pro", {"reasoning_effort": "high"})
    assert kwargs.get("reasoning_effort") == "high"
    # turbo takes no effort knob, so the kwarg is dropped rather than sent to a
    # backend that would reject or silently ignore it.
    kwargs = gate_reasoning_kwargs("olano", "turbo", {"reasoning_effort": "high"})
    assert "reasoning_effort" not in kwargs


# ── honest connected state ───────────────────────────────────────────────────


def test_olano_provider_connection_tracks_gateway_credentials(monkeypatch) -> None:
    from olano.admin.models import provider_connection
    from olano.deployment import gateway_key as gk
    from olano.deployment import sources

    monkeypatch.setattr(gk, "gateway_key", lambda: None)
    monkeypatch.setattr(sources, "deployment_token", lambda: None)
    conn = provider_connection("olano")
    assert conn["connected"] is False
    assert conn["state"] == "unattached"
    assert conn["auth"] == "deployment_token"

    # A deployment token alone counts as CONNECTED: the box can enroll a key on
    # first use, and `connected` gates the model pickers + `connected_only` +
    # model_provider_connected. Narrowing it to "holds a key" would make every
    # olano tier unselectable during a transient enrollment failure — including
    # for agents already configured on one.
    monkeypatch.setattr(sources, "deployment_token", lambda: "olano_dep_x")
    conn = provider_connection("olano")
    assert conn["connected"] is True
    # …but it is NOT healthy, and the surface has to say so. This is the state a
    # box sits in while it cannot reach the control plane, where every olano
    # call 401s; reporting a flat "connected" here is what hid a total
    # managed-inference outage behind a green provider card (2026-08-14).
    assert conn["state"] == "unenrolled"
    assert "will fail" in conn["detail"]

    # A gateway key alone is the fully healthy state (env-provisioned box, no
    # vaulted token).
    monkeypatch.setattr(sources, "deployment_token", lambda: None)
    monkeypatch.setattr(gk, "gateway_key", lambda: "sk-olano-dep-key")
    conn = provider_connection("olano")
    assert conn["connected"] is True
    assert conn["state"] == "enrolled"
