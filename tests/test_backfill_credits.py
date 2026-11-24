"""Historical cognition-credit backfill (analytics/backfill.py).

The mining seams (threads.db, the threadcost cache warmer) are monkeypatched so
each test controls exactly which ``cog::`` rollup rows exist; the LEDGER side is
real — rows land in the pinned test home's usage.jsonl and are read back with
the production reader, so dedup/cutoff/blank-day behavior is exercised end to
end, not against fakes.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from olano.analytics import backfill
from olano.analytics.threadcost import cache, reader, resolver, service
from olano.analytics.usage_ledger import iter_usage_events, record_usage_event

TID = "cog::merli__cog__ideation__ab12cd"


class _DummyConn:
    # Shaped like a `ThreadDBHandle`: the replay's size guard reads `backend`
    # before anything else, and a handle that is not postgres measures as
    # unknown, which never refuses a read.
    backend = "sqlite"
    saver = None

    def close(self) -> None:
        pass


def _cog_row(**overrides) -> dict:
    row = {
        "thread_id": TID,
        "agent": "merli",
        "day": "2026-06-01",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_read_tokens": 40,
        "call_count": 3,
        "subagent_call_count": 0,
    }
    row.update(overrides)
    return row


def _pin_mining(monkeypatch, rows: list[dict]) -> None:
    # The engine's deep model comes from the box's own config.yaml; pin it so
    # the dev machine's real config can't flip rows between olano and BYOK.
    monkeypatch.setattr(backfill, "_deep_model", lambda: "olano:pro")
    monkeypatch.setattr(reader, "thread_store_available", lambda home=None: True)
    monkeypatch.setattr(reader, "open_thread_db", lambda home=None: _DummyConn())
    monkeypatch.setattr(cache, "connect", lambda home=None: _DummyConn())
    monkeypatch.setattr(cache, "all_rows", lambda conn: list(rows))
    monkeypatch.setattr(resolver, "build_thread_agent_index", lambda home=None: {})
    monkeypatch.setattr(
        service,
        "warm_cache",
        lambda cc, tc, index, cap=500, home=None: {"remaining": 0, "refreshed": 0},
    )


def test_worker_label_parses_run_ids_and_compound_labels() -> None:
    assert backfill._worker_label("cog::merli__cog__pulse_agent__ab12cd") == "pulse_agent"
    # The label itself may contain __ — only the trailing hex run-id strips.
    assert backfill._worker_label("cog::merli__cog__sub__deploy__ab12cd") == "sub__deploy"
    assert backfill._worker_label("cog::legacy-no-marker") == ""


def test_estimated_rows_carry_origin_backfill_src_and_noon_ts(monkeypatch) -> None:
    _pin_mining(
        monkeypatch, [_cog_row(), _cog_row(thread_id="cog::merli__cog__sub__deploy__ff00aa")]
    )
    stats = backfill.backfill_cognition_credits()
    assert stats["written"] == 2 and stats["estimated"] == 2 and stats["replayed"] == 0
    assert stats["complete"] is True and backfill.is_complete() is True

    events = {ev.src: ev for ev in iter_usage_events()}
    ev = events[TID]
    assert ev.agent == "merli"
    assert ev.model == "olano:pro"  # deep-model default on an unconfigured box
    assert ev.origin == "cognition"
    assert ev.backfill is True
    # History lands at noon UTC of the run day, never "now".
    assert ev.ts == "2026-06-01T12:00:00+00:00"
    assert ev.credits and ev.credits > 0
    assert ev.calls == 3 and ev.input_tokens == 1000 and ev.cache_read_tokens == 40
    # A delegate_task worker (sub__* label) files under the delegate origin.
    assert events["cog::merli__cog__sub__deploy__ff00aa"].origin == "delegate"


def test_rerun_is_idempotent_via_src_dedup(monkeypatch) -> None:
    _pin_mining(monkeypatch, [_cog_row()])
    assert backfill.backfill_cognition_credits()["written"] == 1
    again = backfill.backfill_cognition_credits(force=True)  # force skips cutoff, never dedup
    assert again["written"] == 0 and again["skipped_dedup"] == 1
    assert sum(1 for _ in iter_usage_events()) == 1


def test_blank_day_rows_stay_unwritten(monkeypatch) -> None:
    # A blank day reads as "today" downstream — history must never land there.
    _pin_mining(monkeypatch, [_cog_row(day="")])
    stats = backfill.backfill_cognition_credits()
    assert stats["written"] == 0 and stats["skipped_blank_day"] == 1
    assert list(iter_usage_events()) == []


def test_cutoff_stops_at_first_live_cognition_day(monkeypatch) -> None:
    # Live recording began 2026-06-02: that day and later already have real
    # rows, so backfilling them would double count the boundary.
    record_usage_event(
        "merli",
        "olano:pro",
        10,
        5,
        0.0,
        credits=0.5,
        ts="2026-06-02T08:00:00+00:00",
        origin="cognition",
    )
    _pin_mining(
        monkeypatch,
        [
            _cog_row(),  # 2026-06-01 < cutoff → written
            _cog_row(thread_id="cog::merli__cog__ideation__ee11ff", day="2026-06-02"),
        ],
    )
    stats = backfill.backfill_cognition_credits()
    assert stats["written"] == 1 and stats["skipped_cutoff"] == 1
    assert {ev.src for ev in iter_usage_events() if ev.backfill} == {TID}
    # --force ignores the cutoff (dedup still holds), picking up the rest.
    forced = backfill.backfill_cognition_credits(force=True)
    assert forced["written"] == 1 and forced["skipped_dedup"] == 1


def test_byok_deep_model_is_skipped(monkeypatch) -> None:
    # No credits to reconstruct on BYOK — that history is the mined book's.
    _pin_mining(monkeypatch, [_cog_row()])
    monkeypatch.setattr(backfill, "_deep_model", lambda: "anthropic:claude-sonnet-4-6")
    stats = backfill.backfill_cognition_credits()
    assert stats["written"] == 0 and stats["skipped_byok"] == 1


def test_inherit_labels_price_at_the_parent_model(monkeypatch) -> None:
    _pin_mining(monkeypatch, [_cog_row(thread_id="cog::merli__cog__pulse_agent__aa11bb")])
    monkeypatch.setattr(backfill, "_parent_model", lambda agent: "olano:max")
    backfill.backfill_cognition_credits()
    (ev,) = list(iter_usage_events())
    assert ev.model == "olano:max" and ev.origin == "cognition"


def test_replay_prices_from_checkpointed_gateway_headers(monkeypatch) -> None:
    _pin_mining(
        monkeypatch,
        [
            _cog_row(),
            # A thread with subagent calls never replays (root namespace only
            # covers part of it) — it falls through to the estimator.
            _cog_row(thread_id="cog::merli__cog__ideation__ee11ff", subagent_call_count=2),
        ],
    )
    msgs = [
        AIMessage(content="a", response_metadata={"headers": {"x-olano-credits-charged": "2.5"}}),
        AIMessage(content="b", response_metadata={"headers": {"x-litellm-response-cost": "0.001"}}),
    ]
    monkeypatch.setattr(reader, "read_namespace_messages_ex", lambda saver, tid, ns: (msgs, None))
    stats = backfill.backfill_cognition_credits()
    assert stats["replayed"] == 1 and stats["estimated"] == 1
    replayed = next(ev for ev in iter_usage_events() if ev.src == TID)
    # 2.5 charged directly + 0.001 credit-USD at the published rate.
    from olano.catalog.credits_registry import credits_per_usd

    assert replayed.credits == round(2.5 + 0.001 * credits_per_usd(), 6)


def test_oversized_replay_prices_off_the_cached_row_without_loading_history(monkeypatch) -> None:
    """The credit replay reads a thread's whole root namespace, from a thread
    that starts at boot; a thread over the miner's cap is estimated from its
    cached token counts instead, and the checkpoint store is never read."""
    _pin_mining(monkeypatch, [_cog_row()])
    monkeypatch.setenv(service._MAX_HISTORY_ENV, "1")
    service._reset_mining_bounds_for_tests()
    monkeypatch.setattr(reader, "pending_history_bytes", lambda _tc, _tid: 3 * 1024 * 1024)
    reads: list[str] = []

    def read_messages(_saver, tid, _ns):
        reads.append(tid)
        return [], None

    monkeypatch.setattr(reader, "read_namespace_messages_ex", read_messages)
    stats = backfill.backfill_cognition_credits()
    assert reads == []
    assert stats["estimated"] == 1 and stats["replayed"] == 0
    (event,) = list(iter_usage_events())
    assert event.src == TID and event.input_tokens == 1000 and event.credits > 0


def test_dry_run_writes_no_rows_and_no_state(monkeypatch) -> None:
    _pin_mining(monkeypatch, [_cog_row()])
    stats = backfill.backfill_cognition_credits(dry_run=True)
    assert stats["written"] == 1 and stats["dry_run"] is True
    assert list(iter_usage_events()) == []
    assert backfill.is_complete() is False


def test_partial_mine_leaves_state_pending(monkeypatch) -> None:
    _pin_mining(monkeypatch, [_cog_row()])
    # Warmer reports stale threads but no progress (errors) → partial pass.
    monkeypatch.setattr(
        service,
        "warm_cache",
        lambda cc, tc, index, cap=500, home=None: {"remaining": 3, "refreshed": 0},
    )
    stats = backfill.backfill_cognition_credits()
    # Whatever IS cached still backfills; completion waits for a full mine.
    assert stats["written"] == 1 and stats["complete"] is False
    assert backfill.is_complete() is False


def test_no_thread_store_completes_immediately(monkeypatch) -> None:
    monkeypatch.setattr(reader, "thread_store_available", lambda home=None: False)
    stats = backfill.backfill_cognition_credits()
    assert stats["complete"] is True and stats["written"] == 0
    assert backfill.is_complete() is True
    # And the boot hook becomes a no-op once done.
    assert backfill.maybe_spawn_backfill() is None
