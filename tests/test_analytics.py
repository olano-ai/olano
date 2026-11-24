"""Tests for the analytics layer: usage ledger, rollups, journal, md script."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from olano.analytics import aggregate, journal, service_ledger, usage_ledger
from olano.config.home import get_olano_paths

TODAY = datetime.now(timezone.utc).date().isoformat()


def _ts(hour: int) -> str:
    return f"{TODAY}T{hour:02d}:00:00+00:00"


def _hours_ago(hours: float) -> str:
    """Full ISO UTC timestamp ``hours`` before now (for rolling-window tests)."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _seed_usage() -> None:
    usage_ledger.record_usage_event(
        "merli", "anthropic:claude-sonnet-4-6", 1000, 300, 0.0075, ts=_ts(9)
    )
    usage_ledger.record_usage_event(
        "merli", "anthropic:claude-opus-4-8", 500, 200, 0.0225, ts=_ts(10)
    )
    usage_ledger.record_usage_event("scout", "openai:gpt-4o", 2000, 100, 0.0115, ts=_ts(11))


def _seed_services() -> None:
    """Two managed calls (one priced by the cloud, one gateway-metered) + a BYOK one."""
    service_ledger.record_service_event(
        "web_search", provider="olano", managed=True, credits=2.5, agent="merli", ts=_ts(9)
    )
    service_ledger.record_service_event(
        "image", provider="olano", managed=True, units=2, agent="merli", ts=_ts(10)
    )
    service_ledger.record_service_event(
        "tts", provider="openai", managed=False, agent="scout", ts=_ts(11)
    )


def _seed_session_tools() -> None:
    sd = get_olano_paths().sessions_dir / "merli"
    sd.mkdir(parents=True, exist_ok=True)
    with (sd / "t1.jsonl").open("w", encoding="utf-8") as f:
        for name in ("web_search", "web_search", "read_file"):
            f.write(json.dumps({"role": "tool_call", "name": name, "ts": _ts(9)}) + "\n")
        f.write(json.dumps({"role": "assistant", "content": "hi", "ts": _ts(9)}) + "\n")


def _seed_a2a() -> None:
    ld = get_olano_paths().logs_dir
    ld.mkdir(parents=True, exist_ok=True)
    with (ld / "a2a_activity.jsonl").open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "agent_message",
                    "ts": _ts(10),
                    "data": {"phase": "send", "from": "merli", "to": "scout", "message": "help"},
                }
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "agent_message",
                    "ts": _ts(10),
                    "data": {
                        "phase": "reply",
                        "from": "merli",
                        "to": "scout",
                        "message": "help",
                        "reply": "ok",
                    },
                }
            )
            + "\n"
        )


# ── usage ledger ──────────────────────────────────────────────────────────────


def test_usage_ledger_record_and_read() -> None:
    usage_ledger.record_usage_event(
        "merli", "anthropic:claude-sonnet-4-6", 100, 50, 0.001, ts=_ts(8)
    )
    events = usage_ledger.read_usage_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.agent == "merli"
    assert ev.input_tokens == 100
    assert ev.output_tokens == 50
    assert ev.total_tokens == 150
    assert ev.date == TODAY


def test_usage_ledger_since_filter() -> None:
    usage_ledger.record_usage_event("a", "m", 1, 1, 0.0, ts="2020-01-01T00:00:00+00:00")
    usage_ledger.record_usage_event("b", "m", 1, 1, 0.0, ts=_ts(12))
    assert len(usage_ledger.read_usage_events()) == 2
    recent = usage_ledger.read_usage_events(since="2021-01-01")
    assert len(recent) == 1
    assert recent[0].agent == "b"


def test_ledger_start_day_is_the_minimum_not_the_first_line() -> None:
    """`analytics/backfill.py` appends reconstructed history stamped at its
    original run day, so the earliest row can arrive last — and this value is the
    seam the analytics layer uses to decide which days predate the ledger."""
    assert usage_ledger.ledger_start_day() is None  # empty ledger
    usage_ledger.record_usage_event("a", "m", 1, 1, 0.0, ts=_ts(12))
    assert usage_ledger.ledger_start_day() == TODAY
    # Appended later, dated earlier — the incremental tail read must catch it.
    usage_ledger.record_usage_event("b", "m", 1, 1, 0.0, ts="2020-01-01T00:00:00+00:00")
    assert usage_ledger.ledger_start_day() == "2020-01-01"
    # A repeat call on an unchanged file returns the same answer from the memo.
    assert usage_ledger.ledger_start_day() == "2020-01-01"


def test_ledger_start_day_rescans_a_rewritten_file() -> None:
    """A shrunk or replaced ledger can't be tail-read — it needs a full pass."""
    usage_ledger.record_usage_event("a", "m", 1, 1, 0.0, ts="2020-01-01T00:00:00+00:00")
    usage_ledger.record_usage_event("b", "m", 1, 1, 0.0, ts=_ts(12))
    assert usage_ledger.ledger_start_day() == "2020-01-01"
    path = usage_ledger.usage_ledger_path()
    path.write_text(
        json.dumps({"ts": f"{TODAY}T09:00:00+00:00", "agent": "c", "model": "m"}) + "\n",
        encoding="utf-8",
    )
    assert usage_ledger.ledger_start_day() == TODAY


def test_usage_ledger_folds_model_sub_calls_into_one_row() -> None:
    """One row per turn, but `calls` carries the turn's model sub-calls.

    A tool-heavy turn is several model calls; counting rows instead would
    under-report the "Model calls" figure the dashboard and chat header show.
    """
    usage_ledger.record_usage_event("merli", "openai:gpt-4o", 100, 50, 0.001, calls=4, ts=_ts(8))
    assert usage_ledger.read_usage_events()[0].calls == 4
    assert aggregate.usage_rollup(30)["totals"]["calls"] == 4


def test_usage_ledger_pre_calls_row_reads_as_one_call() -> None:
    """A row written before `calls` existed is the single call it was assumed to be."""
    path = usage_ledger.usage_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ts": _ts(8),
                "agent": "merli",
                "model": "openai:gpt-4o",
                "input_tokens": 10,
                "output_tokens": 5,
                "usd": 0.001,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert usage_ledger.read_usage_events()[0].calls == 1
    assert aggregate.usage_rollup(30)["totals"]["calls"] == 1


def test_usage_ledger_record_never_raises(monkeypatch) -> None:
    # A broken path must not propagate — accounting can't break a turn.
    monkeypatch.setattr(
        usage_ledger, "usage_ledger_path", lambda: Path("/proc/nonexistent/x.jsonl")
    )
    usage_ledger.record_usage_event("x", "m", 1, 1, 0.0)  # should not raise


# ── aggregation ───────────────────────────────────────────────────────────────


def test_usage_rollup_totals_and_breakdowns() -> None:
    _seed_usage()
    roll = aggregate.usage_rollup(30)
    assert roll["totals"]["calls"] == 3
    assert roll["totals"]["total_tokens"] == 4100
    assert roll["totals"]["agents"] == 2
    assert roll["totals"]["models"] == 3
    # by_agent sorted by usd desc; merli (0.03) > scout (0.0115)
    assert [r["agent"] for r in roll["by_agent"]] == ["merli", "scout"]
    assert roll["by_agent"][0]["usd"] == 0.03
    # by_model: opus most expensive
    assert roll["by_model"][0]["model"] == "anthropic:claude-opus-4-8"


def test_usage_rollup_daily_cumulative() -> None:
    _seed_usage()
    daily = aggregate.usage_rollup(30)["daily"]
    assert len(daily) == 30
    nonzero = [d for d in daily if d["usd"] > 0]
    assert len(nonzero) == 1
    assert nonzero[0]["date"] == TODAY
    # cumulative is monotonic non-decreasing and ends at the total
    cum = [d["cum_usd"] for d in daily]
    assert cum == sorted(cum)
    assert daily[-1]["cum_tokens"] == 4100


def test_tool_rollup() -> None:
    _seed_session_tools()
    roll = aggregate.tool_rollup(30)
    assert roll["totals"]["tool_calls"] == 3
    assert roll["totals"]["distinct_tools"] == 2
    assert roll["by_agent"][0] == {"agent": "merli", "tool_calls": 3}
    top = {r["tool"]: r["count"] for r in roll["by_tool"]}
    assert top == {"web_search": 2, "read_file": 1}
    assert roll["by_subagent"] == []  # no task calls seeded


def test_tool_rollup_splits_subagents_and_recovers_names() -> None:
    sd = get_olano_paths().sessions_dir / "merli"
    sd.mkdir(parents=True, exist_ok=True)
    with (sd / "t1.jsonl").open("w", encoding="utf-8") as f:
        # legacy entries: no structured name, content="<name>: <args>"
        f.write(json.dumps({"role": "tool_call", "content": "web_search: {}", "ts": _ts(9)}) + "\n")
        f.write(json.dumps({"role": "tool_call", "content": "read_file: a", "ts": _ts(9)}) + "\n")
        # structured subagent dispatch via the middleware path
        f.write(
            json.dumps(
                {
                    "role": "tool_call",
                    "name": "task",
                    "args": json.dumps({"subagent_type": "TradingAgent", "description": "buy"}),
                    "message_id": "task::c1",
                    "ts": _ts(10),
                }
            )
            + "\n"
        )
        # legacy task with TRUNCATED args in content — subagent still recovered by regex
        f.write(
            json.dumps(
                {
                    "role": "tool_call",
                    "content": 'task: {"subagent_type": "MarketAgent", "des',
                    "ts": _ts(11),
                }
            )
            + "\n"
        )
    roll = aggregate.tool_rollup(30)
    # task excluded from tools; real names recovered from legacy content
    assert {r["tool"]: r["count"] for r in roll["by_tool"]} == {"web_search": 1, "read_file": 1}
    assert "task" not in {r["tool"] for r in roll["by_tool"]}
    # subagents split out, keyed by subagent_type (incl. truncated-arg recovery)
    assert {r["subagent"]: r["count"] for r in roll["by_subagent"]} == {
        "TradingAgent": 1,
        "MarketAgent": 1,
    }
    assert roll["totals"]["tool_calls"] == 2
    assert roll["totals"]["subagent_calls"] == 2
    assert roll["totals"]["distinct_subagents"] == 2


def test_tool_name_helpers() -> None:
    assert aggregate.tool_name_of({"name": "web_search"}) == "web_search"
    assert aggregate.tool_name_of({"content": "read_file: a.txt"}) == "read_file"
    assert aggregate.tool_name_of({"content": "no delimiter here"}) == "(unnamed)"
    assert aggregate.is_subagent_call({"name": "task"})
    assert aggregate.is_subagent_call({"message_id": "task::abc", "content": "x"})
    assert not aggregate.is_subagent_call({"name": "web_search"})
    assert aggregate.subagent_name_of({"args": '{"subagent_type": "Scout"}'}) == "Scout"
    assert (
        aggregate.subagent_name_of({"content": 'task: {"subagent_type": "Scout", "de'}) == "Scout"
    )
    assert aggregate.subagent_name_of({"args": "{}"}) == "unknown"


def test_a2a_rollup_counts_sends_only() -> None:
    _seed_a2a()
    roll = aggregate.a2a_rollup(30)
    assert roll["totals"]["messages"] == 1  # reply not double-counted
    assert roll["edges"] == [{"from": "merli", "to": "scout", "count": 1}]
    by_agent = {r["agent"]: r for r in roll["by_agent"]}
    assert by_agent["merli"]["sent"] == 1
    assert by_agent["scout"]["received"] == 1


def test_overview_combines_all() -> None:
    _seed_usage()
    _seed_session_tools()
    _seed_a2a()
    _seed_services()
    ov = aggregate.overview(30)
    assert ov["window_days"] == 30
    assert ov["usage"]["totals"]["calls"] == 3
    assert ov["tools"]["totals"]["tool_calls"] == 3
    assert ov["a2a"]["totals"]["messages"] == 1
    assert ov["services"]["totals"]["calls"] == 4


def test_empty_overview_is_well_formed() -> None:
    ov = aggregate.overview(7)
    assert ov["usage"]["totals"]["calls"] == 0
    assert ov["usage"]["by_agent"] == []
    assert len(ov["usage"]["daily"]) == 7
    assert ov["tools"]["by_tool"] == []
    assert ov["a2a"]["edges"] == []
    assert ov["services"]["totals"]["calls"] == 0
    assert len(ov["services"]["daily"]) == 7


# ── service ledger (non-model spend: image / speech / managed web) ────────────


def test_service_ledger_record_and_read() -> None:
    service_ledger.record_service_event(
        "web_fetch", provider="olano", managed=True, credits=1.25, agent="merli", ts=_ts(8)
    )
    events = service_ledger.read_service_events()
    assert len(events) == 1
    ev = events[0]
    assert ev.agent == "merli"
    assert ev.service == "web_fetch"
    assert ev.billing_source == "olano"
    assert ev.credits == 1.25
    assert ev.units == 1
    assert ev.date == TODAY


def test_service_ledger_byok_call_carries_no_charge() -> None:
    """A BYOK service call is counted, never priced — Olano sold nothing."""
    service_ledger.record_service_event(
        "tts", provider="openai", managed=False, agent="scout", ts=_ts(8)
    )
    ev = service_ledger.read_service_events()[0]
    assert ev.billing_source == "customer"
    assert ev.credits is None
    # …and the serialized row omits the key entirely rather than writing 0.0.
    assert "credits" not in ev.to_json()


def test_service_ledger_record_never_raises(monkeypatch) -> None:
    # A broken path must not propagate — accounting can't break a tool call.
    monkeypatch.setattr(
        service_ledger, "service_ledger_path", lambda: Path("/proc/nonexistent/x.jsonl")
    )
    service_ledger.record_service_event("image", provider="olano", managed=True)


def test_service_rollup_totals_and_breakdowns() -> None:
    _seed_services()
    roll = aggregate.service_rollup(30)
    # `units` counts, so the 2-image call contributes 2.
    assert roll["totals"]["calls"] == 4
    assert roll["totals"]["credits"] == 2.5
    # Only the cloud-priced web call carries a charge; image/tts are metered
    # cloud-side and reported without one.
    assert roll["totals"]["priced_calls"] == 1
    assert roll["totals"]["services"] == 3
    by_service = {r["service"]: r for r in roll["by_service"]}
    assert by_service["image"]["calls"] == 2
    assert by_service["image"]["credits"] == 0.0
    assert by_service["web_search"]["credits"] == 2.5
    by_source = {r["source"]: r for r in roll["by_source"]}
    assert by_source["olano"]["calls"] == 3
    assert by_source["customer"]["calls"] == 1
    assert by_source["customer"]["credits"] == 0.0


def test_service_rollup_scopes_to_granted_agents() -> None:
    _seed_services()
    roll = aggregate.service_rollup(30, agents={"scout"})
    assert roll["totals"]["calls"] == 1
    assert [r["service"] for r in roll["by_service"]] == ["tts"]


def test_service_rollup_daily_series_is_zero_filled() -> None:
    _seed_services()
    daily = aggregate.service_rollup(7)["daily"]
    assert len(daily) == 7
    assert [d["date"] for d in daily] == sorted(d["date"] for d in daily)
    today = next(d for d in daily if d["date"] == TODAY)
    assert today["calls"] == 4
    assert today["credits"] == 2.5


# ── hourly (rolling last-24h) rollups ─────────────────────────────────────────


def test_hour_window_bounds_shape() -> None:
    before = datetime.now(timezone.utc).isoformat()[:13]
    start, since, all_hours = aggregate._hour_window_bounds(24)
    after = datetime.now(timezone.utc).isoformat()[:13]
    assert len(all_hours) == 24
    assert since == start.isoformat()
    assert all_hours[0] == start.isoformat()[:13]
    # Newest bucket is the current (possibly partial) UTC hour; tolerate the
    # test itself straddling an hour boundary.
    assert all_hours[-1] in (before, after)
    assert all_hours == sorted(all_hours)
    # Clamped to sane bounds.
    assert len(aggregate._hour_window_bounds(0)[2]) == 24
    assert len(aggregate._hour_window_bounds(9999)[2]) == 168


def test_usage_rollup_hourly_buckets_and_window() -> None:
    # Two calls safely inside the last 24 h, one safely outside.
    usage_ledger.record_usage_event(
        "merli", "anthropic:claude-sonnet-4-6", 1000, 300, 0.0075, ts=_hours_ago(2)
    )
    usage_ledger.record_usage_event("scout", "openai:gpt-4o", 2000, 100, 0.0115, ts=_hours_ago(3))
    usage_ledger.record_usage_event(
        "merli", "anthropic:claude-opus-4-8", 500, 200, 0.0225, ts=_hours_ago(30)
    )
    roll = aggregate.usage_rollup_hourly(24)
    assert roll["source"] == "usage_ledger"
    assert roll["totals"]["calls"] == 2
    assert roll["totals"]["total_tokens"] == 3400
    assert roll["totals"]["agents"] == 2
    assert roll["totals"]["models"] == 2  # the out-of-window opus call is excluded
    assert len(roll["hourly"]) == 24
    assert roll["daily"] == []  # kept (empty) so the common shape never breaks
    # Bucket keys are "YYYY-MM-DDTHH" and the in-window calls all landed.
    assert all(len(h["hour"]) == 13 and h["hour"][10] == "T" for h in roll["hourly"])
    assert sum(h["calls"] for h in roll["hourly"]) == 2
    # Cumulative series is monotonic and ends at the window totals.
    cum = [h["cum_usd"] for h in roll["hourly"]]
    assert cum == sorted(cum)
    assert roll["hourly"][-1]["cum_tokens"] == 3400


def test_tool_rollup_hours_window() -> None:
    sd = get_olano_paths().sessions_dir / "merli"
    sd.mkdir(parents=True, exist_ok=True)
    with (sd / "t1.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps({"role": "tool_call", "name": "web_search", "ts": _hours_ago(1)}) + "\n")
        f.write(json.dumps({"role": "tool_call", "name": "read_file", "ts": _hours_ago(30)}) + "\n")
    roll = aggregate.tool_rollup(hours=24)
    assert {r["tool"]: r["count"] for r in roll["by_tool"]} == {"web_search": 1}
    assert roll["totals"]["tool_calls"] == 1
    # The same transcript over a day window still sees both calls.
    assert aggregate.tool_rollup(30)["totals"]["tool_calls"] == 2


def test_service_rollup_hours_window() -> None:
    service_ledger.record_service_event("image", provider="olano", managed=True, ts=_hours_ago(1))
    service_ledger.record_service_event("stt", provider="olano", managed=True, ts=_hours_ago(30))
    roll = aggregate.service_rollup(hours=24)
    assert roll["totals"]["calls"] == 1
    assert [r["service"] for r in roll["by_service"]] == ["image"]
    # Rolling windows carry `hourly` and leave `daily` empty (and vice versa),
    # the same naming contract the usage rollups follow.
    assert len(roll["hourly"]) == 24
    assert roll["daily"] == []
    assert aggregate.service_rollup(30)["totals"]["calls"] == 2


def test_a2a_rollup_hours_window() -> None:
    ld = get_olano_paths().logs_dir
    ld.mkdir(parents=True, exist_ok=True)
    with (ld / "a2a_activity.jsonl").open("w", encoding="utf-8") as f:
        for ts in (_hours_ago(3), _hours_ago(30)):
            f.write(
                json.dumps(
                    {
                        "type": "agent_message",
                        "ts": ts,
                        "data": {"phase": "send", "from": "merli", "to": "scout", "message": "hi"},
                    }
                )
                + "\n"
            )
    roll = aggregate.a2a_rollup(hours=24)
    assert roll["totals"]["messages"] == 1
    assert aggregate.a2a_rollup(30)["totals"]["messages"] == 2


def test_overview_hourly_mode() -> None:
    usage_ledger.record_usage_event(
        "merli", "anthropic:claude-sonnet-4-6", 100, 50, 0.001, ts=_hours_ago(1)
    )
    ov = aggregate.overview(hours=24)
    assert ov["window_hours"] == 24
    assert ov["window_days"] == 1
    assert "T" in ov["since"]  # full ISO timestamp, not a bare day
    assert ov["usage"]["totals"]["calls"] == 1
    assert len(ov["usage"]["hourly"]) == 24


def test_threadcost_overview_hourly_uses_ledger() -> None:
    """The API-serving overview must switch to the ledger-sourced hourly rollup
    (the checkpoint cache buckets whole threads by day, so it cannot split hours)."""
    from olano.analytics import threadcost

    usage_ledger.record_usage_event(
        "merli", "anthropic:claude-sonnet-4-6", 200, 80, 0.002, ts=_hours_ago(2)
    )
    ov = threadcost.overview(hours=24)
    assert ov["window_hours"] == 24
    assert ov["usage"]["source"] == "usage_ledger"
    assert ov["usage"]["totals"]["calls"] == 1
    assert len(ov["usage"]["hourly"]) == 24
    assert ov["usage"]["daily"] == []


# ── journal ───────────────────────────────────────────────────────────────────


def test_journal_build_and_read() -> None:
    # LLM usage/spend now comes from the thread checkpoint store (threads.db),
    # which isn't present in unit tests → usage metrics are 0 here (the cost math is
    # covered by tests/test_threadcost.py). Tool-call + A2A metrics still come from
    # the session transcripts / A2A log seeded below.
    _seed_session_tools()
    _seed_a2a()
    entry = journal.build_journal_day(TODAY)
    assert entry["date"] == TODAY
    assert entry["metrics"]["calls"] == 0  # usage sourced from threads.db (none here)
    assert entry["metrics"]["tool_calls"] == 3
    assert entry["metrics"]["a2a_messages"] == 1
    assert set(entry["metrics"]["active_agents"]) == {"merli", "scout"}
    # merli row has tools + a2a
    merli = next(a for a in entry["by_agent"] if a["agent"] == "merli")
    assert merli["tool_calls"] == 3
    assert merli["a2a_sent"] == 1
    # round-trips from disk
    again = journal.read_journal(TODAY)
    assert again is not None and again["date"] == TODAY
    assert TODAY in journal.list_journal_days()


def test_journal_rebuild_preserves_narrative() -> None:
    _seed_usage()
    entry = journal.build_journal_day(TODAY)
    entry["narrative"] = "A productive day."
    journal.journal_path(TODAY).write_text(json.dumps(entry), encoding="utf-8")
    # Rebuild metrics — narrative must survive.
    rebuilt = journal.build_journal_day(TODAY)
    assert rebuilt["narrative"] == "A productive day."


def test_journal_missing_day_returns_none() -> None:
    assert journal.read_journal("1999-01-01") is None


def test_journal_day_keeps_the_two_billing_books_apart() -> None:
    """A journal day must carry the managed charge AND the BYOK estimate, never
    one standing in for the other.

    This is the bug the dashboard's "Spend: 0.046 credits" was: on a deployment
    whose agents run on Olano models, ``cost_for_call`` prices those calls at
    $0 (the box is never told the gateway's rate), so ``metrics.usd`` is only
    the BYOK sliver — and rendering *that* through the USD→credits display
    conversion produced a credits-labelled number from the other book, with the
    day's real credit charge nowhere on the panel.
    """
    usage_ledger.record_usage_event("merli", "olano:pro", 1000, 300, 0.0, credits=45.0, ts=_ts(9))
    usage_ledger.record_usage_event(
        "scout", "anthropic:claude-sonnet-4-6", 800, 200, 0.0075, ts=_ts(10)
    )
    entry = journal.build_journal_day(TODAY)

    m = entry["metrics"]
    # Managed spend lands in credits, verbatim — not converted, not summed in.
    assert m["credits"] == 45.0
    # BYOK spend stays a dollar figure covering only the customer's own calls.
    assert m["usd"] == 0.0075
    by_agent = {r["agent"]: r for r in entry["by_agent"]}
    assert by_agent["merli"]["credits"] == 45.0 and by_agent["merli"]["usd"] == 0.0
    assert by_agent["scout"]["credits"] == 0.0 and by_agent["scout"]["usd"] == 0.0075


# ── markdown script ───────────────────────────────────────────────────────────


def _load_md_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "journal_to_markdown.py"
    spec = importlib.util.spec_from_file_location("journal_to_markdown", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_journal_to_markdown_render() -> None:
    md_script = _load_md_script()
    _seed_session_tools()
    entry = journal.build_journal_day(TODAY)
    entry["narrative"] = "Merli did research while scout helped."
    # by_model usage now comes from threads.db (absent in tests); inject a row so
    # the renderer's "## By model" section is exercised.
    entry["by_model"] = [
        {"model": "anthropic:claude-sonnet-4-6", "usd": 0.01, "total_tokens": 1000, "calls": 1}
    ]
    out = md_script.render_journal(entry)
    assert f"# Work Journal — {TODAY}" in out
    assert "Merli did research" in out
    assert "## By agent" in out
    assert "merli" in out
    assert "## By model" in out
    # No credits recorded ⇒ no credits column, and the dollar figure is labelled
    # for the book it belongs to rather than as a bare "Spend".
    assert "API cost" in out
    assert "Credits" not in out
    # token compaction helper
    assert md_script._fmt_tokens(1_500_000) == "1.5M"
    assert md_script._fmt_tokens(3400) == "3.4K"
    assert md_script._fmt_tokens(12) == "12"


def test_journal_to_markdown_renders_both_books() -> None:
    """A day with Olano-managed spend gets a credits column beside the dollar
    one — never a single merged 'spend' figure, which would add two currencies."""
    md_script = _load_md_script()
    entry = {
        "date": TODAY,
        "metrics": {
            "usd": 0.0075,
            "credits": 45.0,
            "calls": 2,
            "input_tokens": 1800,
            "output_tokens": 500,
            "total_tokens": 2300,
            "tool_calls": 0,
            "a2a_messages": 0,
            "active_agents": ["merli", "scout"],
        },
        "by_agent": [
            {"agent": "merli", "usd": 0.0, "credits": 45.0, "calls": 1, "total_tokens": 1300},
            {"agent": "scout", "usd": 0.0075, "credits": 0.0, "calls": 1, "total_tokens": 1000},
        ],
        "by_model": [
            {"model": "olano:pro", "usd": 0.0, "credits": 45.0, "total_tokens": 1300, "calls": 1},
        ],
    }
    out = md_script.render_journal(entry)
    assert "- **Olano credits:** 45 credits" in out
    assert "- **API cost (your keys):** $0.0075" in out
    assert "| Agent | Credits | API cost | Tokens | Calls | Tools | A2A (↑/↓) | Top tools |" in out
    assert "| Model | Credits | API cost | Tokens | Calls |" in out
    assert "| merli | 45 credits | $0.0000 " in out
    # Sub-credit amounts keep enough precision to not read as zero.
    assert md_script._fmt_credits(0.046) == "0.046 credits"


# ── narrative extraction (reasoning-model robustness) ─────────────────────────


def test_extract_text_skips_reasoning_blocks() -> None:
    class Result:
        content = [
            {"id": "rs_x", "type": "reasoning", "encrypted_content": "BASE64", "index": 0},
            {"type": "text", "text": "Merli researched pricing and delegated to scout."},
        ]

    out = journal._extract_text(Result())
    assert out == "Merli researched pricing and delegated to scout."
    assert "reasoning" not in out and "encrypted" not in out and "{" not in out


def test_extract_text_plain_string_and_text_accessor() -> None:
    class Plain:
        content = "hello world"

    class WithAccessor:
        content = [{"type": "text", "text": "ignored"}]

        def text(self) -> str:
            return "from text()"

    assert journal._extract_text(Plain()) == "hello world"
    assert journal._extract_text(WithAccessor()) == "from text()"


# ── latest active day ─────────────────────────────────────────────────────────


def test_latest_active_day() -> None:
    # Now sourced from the thread checkpoint cache (max day), falling back to the
    # newest journal file when the cache is empty (no threads.db in unit tests).
    assert journal.latest_active_day() is None
    journal.build_journal_day("2026-05-01")
    journal.build_journal_day("2026-05-03")
    assert journal.latest_active_day() == "2026-05-03"


# ── pricing table currency ────────────────────────────────────────────────────


def test_pricing_covers_modern_models() -> None:
    from olano.pulse.cost_tracker import _model_cost_per_token

    # gpt-5.4 must now match the gpt-5 prefix → non-trivial cost (was ~0 before).
    inp, out = _model_cost_per_token("openai:gpt-5.4")
    assert 45_000 * inp + 342 * out > 0.05
    # More-specific prefix must win over the broader parent.
    assert _model_cost_per_token("openai:gpt-4o-mini") == (0.15 / 1e6, 0.60 / 1e6)
    assert _model_cost_per_token("openai:gpt-4o") == (2.5 / 1e6, 10.0 / 1e6)
    # Claude 4.x naming (the "-N-N" infix) matches the bare-tier prefixes.
    assert _model_cost_per_token("anthropic:claude-sonnet-4-6") == (3.0 / 1e6, 15.0 / 1e6)
    # Claude 3.x naming has a "-3-"/"-3-5-" infix so it needs explicit entries;
    # claude-3-opus is priced differently from claude-opus-4.x.
    assert _model_cost_per_token("anthropic:claude-3-opus-20240229") == (15.0 / 1e6, 75.0 / 1e6)
    assert _model_cost_per_token("anthropic:claude-3-5-sonnet-20241022") == (3.0 / 1e6, 15.0 / 1e6)
    assert _model_cost_per_token("anthropic:claude-3-haiku-20240307") == (0.25 / 1e6, 1.25 / 1e6)


def test_extract_text_dict_content_no_raw_repr() -> None:
    class DictContent:
        content = {"type": "text", "text": "clean text"}

    class DictNoText:
        content = {"id": "rs_x", "type": "reasoning", "encrypted_content": "BASE64"}

    assert journal._extract_text(DictContent()) == "clean text"
    out = journal._extract_text(DictNoText())
    assert out == "" and "{" not in out


# ── LLM highlights (generate_highlights) ─────────────────────────────────────


class _FakeLLM:
    def __init__(self, text: str) -> None:
        self._text = text

    async def ainvoke(self, prompt: str):
        class R:
            content = self._text

        return R()


async def test_generate_highlights_falls_back_to_fast_model(monkeypatch) -> None:
    """With no preferred_model and a missing default agent, highlights must fall
    back to the cognition fast_model instead of silently doing nothing."""
    import olano.models as models_mod
    from olano.config.system_settings import SystemSettings

    _seed_session_tools()
    built: list[str] = []

    def fake_build_model(model_str: str, **kwargs):
        built.append(model_str)
        return _FakeLLM("The fleet had a busy day.")

    monkeypatch.setattr(models_mod, "build_model", fake_build_model)
    entry = await journal.generate_highlights(TODAY)
    assert entry["narrative"] == "The fleet had a busy day."
    assert built == [SystemSettings().fast_model]
    assert entry["narrative_model"] == SystemSettings().fast_model
    assert "narrative_error" not in entry
    # persisted
    on_disk = journal.read_journal(TODAY)
    assert on_disk is not None and on_disk["narrative"] == "The fleet had a busy day."


async def test_generate_highlights_explicit_model_wins(monkeypatch) -> None:
    import olano.models as models_mod

    built: list[str] = []

    def fake_build_model(model_str: str, **kwargs):
        built.append(model_str)
        return _FakeLLM("Summary.")

    monkeypatch.setattr(models_mod, "build_model", fake_build_model)
    entry = await journal.generate_highlights(TODAY, model="anthropic:claude-sonnet-4-6")
    assert built == ["anthropic:claude-sonnet-4-6"]
    assert entry["narrative_model"] == "anthropic:claude-sonnet-4-6"


async def test_generate_highlights_surfaces_error_transiently(monkeypatch) -> None:
    """A provider failure sets narrative_error on the response but is never
    written to the journal file."""
    import olano.models as models_mod

    def broken_build_model(model_str: str, **kwargs):
        raise RuntimeError("invalid api key")

    monkeypatch.setattr(models_mod, "build_model", broken_build_model)
    entry = await journal.generate_highlights(TODAY)
    assert entry["narrative"] is None
    assert "invalid api key" in entry.get("narrative_error", "")
    on_disk = journal.read_journal(TODAY)
    assert on_disk is not None and "narrative_error" not in on_disk


async def test_generate_highlights_empty_text_sets_error(monkeypatch) -> None:
    import olano.models as models_mod

    class ReasoningOnly:
        async def ainvoke(self, prompt: str):
            class R:
                content = [{"type": "reasoning", "encrypted_content": "B64"}]

            return R()

    monkeypatch.setattr(models_mod, "build_model", lambda m, **kw: ReasoningOnly())
    entry = await journal.generate_highlights(TODAY)
    assert entry["narrative"] is None
    assert "no usable text" in entry.get("narrative_error", "")


async def test_generate_highlights_ledgers_its_own_spend(monkeypatch) -> None:
    """The narrative call runs outside any AgentRunner turn; it must ledger its
    own usage (agent="system", origin="highlights") or it stays invisible while
    the gateway bills it."""
    import olano.models as models_mod

    class UsageLLM:
        async def ainvoke(self, prompt: str):
            class R:
                content = "Busy day."
                usage_metadata = {"input_tokens": 900, "output_tokens": 90, "total_tokens": 990}
                response_metadata: dict = {}

            return R()

    monkeypatch.setattr(models_mod, "build_model", lambda m, **kw: UsageLLM())
    await journal.generate_highlights(TODAY, model="anthropic:claude-sonnet-4-6")
    events = usage_ledger.read_usage_events()
    assert len(events) == 1
    ev = events[0]
    assert (ev.agent, ev.origin) == ("system", "highlights")
    assert ev.input_tokens == 900 and ev.calls == 1


# ── origin dimension ──────────────────────────────────────────────────────────


def test_usage_ledger_origin_round_trip_and_legacy_rows() -> None:
    usage_ledger.record_usage_event(
        "merli", "olano:pro", 100, 10, 0.0, credits=2.0, origin="cognition", ts=_ts(9)
    )
    usage_ledger.record_usage_event(
        "merli", "anthropic:claude-sonnet-4-6", 100, 10, 0.01, ts=_ts(10)
    )
    events = usage_ledger.read_usage_events()
    assert events[0].origin == "cognition"
    assert events[0].to_json()["origin"] == "cognition"
    # Unlabeled rows keep parsing and serialize without the key (old readers
    # and pre-migration rows are unaffected).
    assert events[1].origin == ""
    assert "origin" not in events[1].to_json()


def test_usage_rollup_by_origin_buckets() -> None:
    usage_ledger.record_usage_event(
        "merli", "olano:pro", 1000, 100, 0.0, credits=5.0, origin="cognition", ts=_ts(9)
    )
    usage_ledger.record_usage_event(
        "merli", "olano:turbo", 500, 50, 0.0, credits=1.0, origin="interactive", ts=_ts(10)
    )
    usage_ledger.record_usage_event("scout", "openai:gpt-4o", 200, 20, 0.02, ts=_ts(11))
    data = aggregate.usage_rollup(7)
    origins = {r["origin"]: r for r in data["by_origin"]}
    assert origins["cognition"]["credits"] == 5.0 and origins["cognition"]["calls"] == 1
    assert origins["interactive"]["credits"] == 1.0
    # Pre-origin rows land in the "turns" bucket, both books intact.
    assert origins["turns"]["usd"] == 0.02 and origins["turns"]["credits"] == 0.0


def test_usage_rollup_hourly_by_origin() -> None:
    usage_ledger.record_usage_event(
        "merli", "olano:pro", 100, 10, 0.0, credits=2.0, origin="pulse", ts=_hours_ago(1)
    )
    data = aggregate.usage_rollup_hourly(24)
    origins = {r["origin"]: r for r in data["by_origin"]}
    assert origins["pulse"]["credits"] == 2.0


# ── work-based highlights (summarise WORK, never usage stats) ─────────────────


def test_scheduler_history_for_day() -> None:
    """history_for_day joins runs to task name + agent and filters by date."""
    from olano.scheduler.db import SchedulerDB

    db = SchedulerDB()
    tid = db.create_task("Morning digest", "summarise news", agent_name="merli")
    db.add_history(tid, result="Posted digest to #general")
    rows = db.history_for_day(TODAY)
    assert len(rows) == 1
    assert rows[0]["name"] == "Morning digest"
    assert rows[0]["agent_name"] == "merli"
    assert rows[0]["result"] == "Posted digest to #general"
    # A day with no runs is empty.
    assert db.history_for_day("1999-01-01") == []


def test_work_is_empty() -> None:
    assert journal.work_is_empty({"conversations": {}, "scheduled_tasks": [], "cortex_groups": []})
    assert not journal.work_is_empty(
        {"conversations": {"merli": [{}]}, "scheduled_tasks": [], "cortex_groups": []}
    )
    assert not journal.work_is_empty(
        {
            "conversations": {},
            "scheduled_tasks": [],
            "cortex_groups": [
                {"agents": [], "cortex": [{"kind": "idea"}], "cognition_actions": []}
            ],
        }
    )


def test_work_highlights_prompt_excludes_usage_stats() -> None:
    """The narrative prompt carries the real WORK and NO usage statistics."""
    work = {
        "conversations": {
            "merli": [
                {
                    "thread": "t1",
                    "user_turns": 1,
                    "assistant_turns": 1,
                    "excerpt": "asked: book me a flight to Tokyo · replied: booked LH714",
                }
            ]
        },
        "scheduled_tasks": [
            {
                "name": "Nightly backup",
                "agent": "scout",
                "result": "12 files archived",
                "error": "",
                "ran_at": _ts(3),
            }
        ],
        "cortex_groups": [
            {
                "agents": ["merli"],
                "cortex": [
                    {
                        "kind": "idea",
                        "title": "Trip planner",
                        "status": "active",
                        "summary": "auto-plan trips",
                        "created": True,
                        "ts": _ts(4),
                    }
                ],
                "cognition_actions": [],
            }
        ],
    }
    prompt = journal._work_highlights_prompt(TODAY, work)
    # The substance of the work is present…
    assert "book me a flight to Tokyo" in prompt
    assert "Nightly backup" in prompt
    assert "Trip planner" in prompt
    assert "[merli]" in prompt and "[scout]" in prompt
    # …and NOT a single usage statistic.
    low = prompt.lower()
    assert "$" not in prompt
    assert "token" not in low
    assert "tool-call" not in low and "tool call" not in low
    assert "spend" not in low
    assert "a2a" not in low


def test_gather_day_work_reads_conversations_and_tasks() -> None:
    from olano.scheduler.db import SchedulerDB

    sd = get_olano_paths().sessions_dir / "merli"
    sd.mkdir(parents=True, exist_ok=True)
    with (sd / "conv.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "what's the weather?", "ts": _ts(9)}) + "\n")
        f.write(json.dumps({"role": "assistant", "content": "Sunny, 25C.", "ts": _ts(9)}) + "\n")

    db = SchedulerDB()
    tid = db.create_task("Weather ping", "check weather", agent_name="merli")
    db.add_history(tid, result="done")

    work = journal.gather_day_work(TODAY)
    assert "merli" in work["conversations"]
    thread = work["conversations"]["merli"][0]
    assert thread["user_turns"] == 1 and thread["assistant_turns"] == 1
    assert "what's the weather?" in thread["excerpt"]
    assert any(t["name"] == "Weather ping" for t in work["scheduled_tasks"])
    assert not journal.work_is_empty(work)


def test_latest_highlights_returns_most_recent_narrative() -> None:
    # Nothing generated yet → no narrative, but still reports today.
    res = journal.latest_highlights()
    assert res["narrative"] is None
    assert res["today"] == TODAY

    journal.build_journal_day("2026-05-01")
    entry = journal.build_journal_day("2026-05-03")
    entry["narrative"] = "Scout triaged incoming tickets while Merli booked travel."
    journal.journal_path("2026-05-03").write_text(json.dumps(entry), encoding="utf-8")

    res2 = journal.latest_highlights()
    assert res2["date"] == "2026-05-03"
    assert res2["narrative"] == "Scout triaged incoming tickets while Merli booked travel."


def _seed_heartbeat_thread() -> None:
    """A heartbeat_<agent> thread with one eventful check and one quiet one."""
    sd = get_olano_paths().sessions_dir / "scout"
    sd.mkdir(parents=True, exist_ok=True)
    with (sd / "heartbeat_scout.jsonl").open("w", encoding="utf-8") as f:
        f.write(
            json.dumps({"role": "user", "content": "HEARTBEAT: check inbox", "ts": _ts(6)}) + "\n"
        )
        f.write(
            json.dumps({"role": "assistant", "content": "Found 2 urgent tickets", "ts": _ts(6)})
            + "\n"
        )
        f.write(
            json.dumps({"role": "user", "content": "HEARTBEAT: check disk", "ts": _ts(7)}) + "\n"
        )
        f.write(json.dumps({"role": "assistant", "content": "HEARTBEAT_OK", "ts": _ts(7)}) + "\n")


def test_day_heartbeat_pairs_task_and_finding() -> None:
    _seed_heartbeat_thread()
    hb = journal._day_heartbeat(TODAY)
    assert "scout" in hb
    runs = hb["scout"]
    assert {r["task"] for r in runs} == {"check inbox", "check disk"}
    eventful = next(r for r in runs if r["task"] == "check inbox")
    assert eventful["finding"] == "Found 2 urgent tickets"
    quiet = next(r for r in runs if r["task"] == "check disk")
    # HEARTBEAT_OK is the "nothing to report" sentinel → no finding.
    assert quiet["finding"] == ""


def test_day_conversations_excludes_system_threads() -> None:
    """Heartbeat / scheduled / peer threads must not leak into user conversations —
    they're summarised in their own sections."""
    sd = get_olano_paths().sessions_dir / "merli"
    sd.mkdir(parents=True, exist_ok=True)
    # A genuine user conversation…
    with (sd / "user-thread.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps({"role": "user", "content": "plan my week", "ts": _ts(9)}) + "\n")
        f.write(json.dumps({"role": "assistant", "content": "sure", "ts": _ts(9)}) + "\n")
    # …plus assorted machinery threads that must be ignored here.
    for stem, msg in (
        ("heartbeat_merli", "HEARTBEAT: poll"),
        ("scheduled_merli_abcd1234", "AUTONOMOUS: nightly job"),
        ("acp::scout", "peer question"),
    ):
        with (sd / f"{stem}.jsonl").open("w", encoding="utf-8") as f:
            f.write(json.dumps({"role": "user", "content": msg, "ts": _ts(9)}) + "\n")
            f.write(json.dumps({"role": "assistant", "content": "done", "ts": _ts(9)}) + "\n")

    convs = journal._day_conversations(TODAY)
    assert "merli" in convs
    threads = convs["merli"]
    assert len(threads) == 1
    assert threads[0]["thread"] == "user-thread"
    assert "plan my week" in threads[0]["excerpt"]


def test_day_agent_messages_reads_content_and_dedups() -> None:
    _seed_a2a()  # one send + one reply for the SAME merli→scout "help" exchange
    msgs = journal._day_agent_messages(TODAY)
    # The send and its reply collapse into a single exchange with the answer.
    assert len(msgs) == 1
    m = msgs[0]
    assert m["from"] == "merli" and m["to"] == "scout"
    assert m["message"] == "help"
    assert m["reply"] == "ok"


def test_gather_day_work_includes_heartbeat_and_agent_messages() -> None:
    _seed_heartbeat_thread()
    _seed_a2a()
    work = journal.gather_day_work(TODAY)
    assert "scout" in work["heartbeat"]
    assert work["agent_messages"] and work["agent_messages"][0]["from"] == "merli"
    assert not journal.work_is_empty(work)


def test_work_is_empty_counts_heartbeat_and_messages() -> None:
    base = {"conversations": {}, "scheduled_tasks": [], "cortex_groups": []}
    assert journal.work_is_empty({**base, "heartbeat": {}, "agent_messages": []})
    assert not journal.work_is_empty(
        {**base, "heartbeat": {"scout": [{"task": "x", "finding": ""}]}, "agent_messages": []}
    )
    assert not journal.work_is_empty(
        {**base, "heartbeat": {}, "agent_messages": [{"from": "a", "to": "b", "message": "hi"}]}
    )


def test_work_highlights_prompt_renders_heartbeat_and_messages() -> None:
    work = {
        "conversations": {},
        "scheduled_tasks": [],
        "cortex_groups": [],
        "heartbeat": {"scout": [{"task": "check inbox", "finding": "2 urgent tickets"}]},
        "agent_messages": [
            {"from": "merli", "to": "scout", "message": "can you cover triage?", "reply": "on it"}
        ],
    }
    prompt = journal._work_highlights_prompt(TODAY, work)
    assert "check inbox" in prompt and "2 urgent tickets" in prompt
    assert "merli → scout" in prompt and "can you cover triage?" in prompt and "on it" in prompt
    # Still zero usage statistics, even with the new sections.
    low = prompt.lower()
    assert "$" not in prompt
    assert "token" not in low
    assert "spend" not in low
    assert "a2a" not in low


def test_legacy_stats_narrative_is_discarded_and_regenerated() -> None:
    """A stored cost/usage blurb self-heals: a rebuild drops it and the Overview
    panel skips it so a fresh work summary is generated."""
    stats_blurb = (
        "Cindy was the sole active agent, executing 145 model calls with no tool "
        "use. The fleet's total spend of $1.03 on 8.3M tokens is routine."
    )
    assert journal._looks_stats_narrative(stats_blurb)
    # A real work summary must NOT be flagged.
    assert not journal._looks_stats_narrative(
        "Merli booked a flight to Tokyo and Scout triaged three support tickets."
    )
    # A refund amount alone (no token/usage vocabulary) must NOT be flagged.
    assert not journal._looks_stats_narrative("Scout processed a $500 refund for a customer.")

    entry = journal.build_journal_day(TODAY)
    entry["narrative"] = stats_blurb
    journal.journal_path(TODAY).write_text(json.dumps(entry), encoding="utf-8")

    # Rebuild drops the legacy stats narrative instead of preserving it.
    rebuilt = journal.build_journal_day(TODAY)
    assert rebuilt["narrative"] is None
    # And the Overview panel skips it rather than surfacing stale stats.
    latest = journal.latest_highlights()
    assert latest["narrative"] is None


# ── structured tool-name persistence (runner) ─────────────────────────────────


def test_append_tool_session_persists_structured_name(base_config, mock_agent) -> None:
    from olano.core.runner import AgentRunner

    runner = AgentRunner(mock_agent, base_config)
    runner._append_tool_session("t1", "tool_call", name="web_search", content='{"q":"x"}')
    path = runner._session_path("t1")
    line = path.read_text(encoding="utf-8").strip().splitlines()[-1]
    entry = json.loads(line)
    assert entry["role"] == "tool_call"
    assert entry["name"] == "web_search"  # structured field for analytics
    assert entry["content"].startswith("web_search:")  # human-readable preserved


# ── per-tier breakdown + the raw-line since prefilter ────────────────────────


def test_tier_of_maps_tiers_aliases_and_utility_groups() -> None:
    assert aggregate.tier_of("olano:pro") == "pro"
    assert aggregate.tier_of("olano:flash") == "turbo"  # legacy alias
    assert aggregate.tier_of("olano:max") == "max"
    # Utility groups bill through the service ledger, never the tier chart.
    assert aggregate.tier_of("olano:vision") is None
    assert aggregate.tier_of("anthropic:claude-sonnet-4-6") is None
    assert aggregate.tier_of("") is None


def test_usage_rollup_carries_by_tier_for_managed_models_only() -> None:
    usage_ledger.record_usage_event(
        "merli", "olano:pro", 1000, 300, 0.0, credits=45.0, calls=2, ts=_ts(9)
    )
    usage_ledger.record_usage_event("merli", "olano:turbo", 200, 50, 0.0, credits=1.5, ts=_ts(10))
    usage_ledger.record_usage_event("scout", "openai:gpt-4o", 500, 100, 0.02, ts=_ts(11))

    for rollup in (aggregate.usage_rollup(days=7), aggregate.usage_rollup_hourly(hours=24)):
        tiers = {r["tier"]: r for r in rollup["by_tier"]}
        assert set(tiers) == {"pro", "turbo"}  # BYOK rows never invent a tier
        assert tiers["pro"]["credits"] == 45.0 and tiers["pro"]["calls"] == 2
        assert tiers["turbo"]["credits"] == 1.5


def test_line_ts_prefilter_never_drops_what_the_parse_would_keep() -> None:
    """iter_usage_events(since=...) must return exactly what the full parse
    returns — the raw-line prefilter is an optimization, never the authority."""
    usage_ledger.record_usage_event("a", "m", 1, 1, 0.0, ts="2026-01-01T08:00:00+00:00")
    usage_ledger.record_usage_event("b", "m", 1, 1, 0.0, ts="2026-06-15T08:00:00+00:00")
    # A foreign line without the writer's ts spelling falls through to the
    # parse (and is then dropped there for being old — not by the prefilter).
    with usage_ledger.usage_ledger_path().open("a", encoding="utf-8") as fh:
        fh.write('{"schema": 1, "ts":"2026-01-02T00:00:00+00:00", "agent": "c", ')
        fh.write('"model": "m", "input_tokens": 1, "output_tokens": 1, "usd": 0.0}\n')

    since = "2026-06-01"
    fast = [e.agent for e in usage_ledger.iter_usage_events(since=since)]
    slow = [e.agent for e in usage_ledger.read_usage_events() if e.ts and e.ts >= since]
    assert fast == slow == ["b"]
    # And the helper itself: absent needle → False (never claims "before").
    assert usage_ledger._line_ts_before("not json at all", since) is False
    assert usage_ledger._line_ts_before('{"ts": "2026-01-01T00:00:00+00:00"}', since) is True
    assert usage_ledger._line_ts_before('{"ts": "2026-07-01T00:00:00+00:00"}', since) is False


def test_config_schedule_runs_are_attributed_to_their_agent() -> None:
    """A ``cfg:<agent>:<n>`` run has no ``tasks`` row, so the history JOIN
    yields no name and no agent — it used to reach the journal as an anonymous
    "(unnamed task)" that ``filter_work`` then dropped from the agent's view."""
    from olano.scheduler.db import SchedulerDB

    SchedulerDB().add_history("cfg:maria:0", result="3 prospects drafted")
    tasks = journal._day_scheduled_tasks(TODAY)
    (run,) = [t for t in tasks if t["result"] == "3 prospects drafted"]
    assert run["agent"] == "maria"
    assert run["name"] == "maria schedule 1"

    work = journal.gather_day_work(TODAY, agents={"maria"})
    assert any(t["result"] == "3 prospects drafted" for t in work["scheduled_tasks"])
    assert journal.filter_work(work, {"aurelius"})["scheduled_tasks"] == []


def test_malformed_cfg_id_stays_anonymous() -> None:
    from olano.scheduler.db import SchedulerDB

    SchedulerDB().add_history("cfg:broken", result="x")
    (run,) = [t for t in journal._day_scheduled_tasks(TODAY) if t["result"] == "x"]
    assert run["agent"] == ""
    assert run["name"] == "(unnamed task)"
