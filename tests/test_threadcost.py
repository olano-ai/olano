"""Tests for the token/cost drill-down (olano.analytics.threadcost).

Covers the pure logic (cost + cache discount, tool attribution, origin/agent
resolution, the derived cache) with synthetic data, plus a guarded read-only
end-to-end pass over the real checkpoint store if one exists.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from olano.analytics.threadcost import attribution, cache, cost, reader, resolver
from olano.analytics.threadcost.reader import CallRecord, ThreadCallSet, ToolCallRef
from olano.config.home import get_default_olano_home

_REAL_DB = get_default_olano_home() / "sessions" / "threads.db"

_SONNET = "anthropic:claude-sonnet-4-6"  # 3 / 15 USD per 1M (input / output)
_IN = 3 / 1_000_000
_OUT = 15 / 1_000_000


def _call(seq, inp, out, tools=(), model=_SONNET, ns="", cache_read=0):
    return CallRecord(
        thread_id="t",
        ns=ns,
        seq=seq,
        message_id=f"m{seq}",
        model=model,
        input_tokens=inp,
        output_tokens=out,
        total_tokens=inp + out,
        cache_read_tokens=cache_read,
        usd=cost.cost_for_call(model, inp, out, cache_read),
        pricing_known=cost.pricing_known(model),
        tool_calls=tuple(ToolCallRef(id=f"{t}-{seq}", name=t) for t in tools),
        ts=None,
    )


# ── cost + cache discount ─────────────────────────────────────────────────────


def test_cost_for_call_basic() -> None:
    assert cost.cost_for_call(_SONNET, 1000, 100) == 1000 * _IN + 100 * _OUT


def test_cache_read_is_discounted() -> None:
    full = cost.cost_for_call(_SONNET, 1000, 0)
    cached = cost.cost_for_call(_SONNET, 1000, 0, cache_read_tokens=1000)
    assert cached < full
    # All-cached input bills at the catalog's cache-read rate ($0.30/1M — 10%
    # of the input rate; approx because the two paths round differently).
    assert cached == pytest.approx(1000 * _IN * 0.1)


def test_pricing_known() -> None:
    assert cost.pricing_known(_SONNET) is True
    assert cost.pricing_known("gpt-5.4-mini-2026-03-17") is True
    assert cost.pricing_known("qwen3.5:35b-a3b") is False
    assert cost.pricing_known("") is False


# Every OpenAI model olano can be pointed at, with its list price per 1M
# (input, output). The gpt-5.6 family used to be absent entirely and fell through
# to the generic "gpt-5" prefix, billing terra at 2x and luna at 5x their real
# rate on every dashboard surface.
_OPENAI_RATES = {
    "gpt-6-astra": (10.0, 50.0),
    "gpt-6.1-sol": (2.0, 10.0),
    "gpt-6-sol": (2.0, 10.0),
    "gpt-6-luna": (0.10, 0.50),
    "gpt-5.6": (5.0, 30.0),
    "gpt-5.6-sol": (5.0, 30.0),
    "gpt-5.6-terra": (2.0, 12.0),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.5": (5.0, 30.0),
    "gpt-5.5-pro": (30.0, 180.0),
    "gpt-5.4": (2.50, 15.0),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4-pro": (30.0, 180.0),
    "gpt-5.3-codex": (1.75, 14.0),
    "gpt-5.3-codex-spark": (1.75, 14.0),
    "gpt-5.2": (1.75, 14.0),
    "gpt-5.2-pro": (21.0, 168.0),
    "gpt-5.1": (1.25, 10.0),
    "gpt-5.1-codex-mini": (0.25, 2.0),
    "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-pro": (15.0, 120.0),
}


@pytest.mark.parametrize(("model", "rate"), sorted(_OPENAI_RATES.items()))
@pytest.mark.parametrize("prefix", ["", "openai:", "openai-codex:"])
def test_openai_list_prices(model: str, rate: tuple[float, float], prefix: str) -> None:
    """Each model bills at its own rate — not a more-general sibling's.

    Runs bare and provider-prefixed because the matcher is a substring scan over
    the whole ``provider:model`` string, and threadcost feeds it the raw
    ``response_metadata.model_name`` while the live usage frame feeds it
    ``config.model``.
    """
    want_in, want_out = rate
    assert cost.cost_for_call(prefix + model, 1_000_000, 0) == pytest.approx(want_in)
    assert cost.cost_for_call(prefix + model, 0, 1_000_000) == pytest.approx(want_out)


def test_dated_model_snapshots_price_like_their_base() -> None:
    """Providers echo back dated ids (gpt-5.6-luna-2026-07-09) in response_metadata."""
    for model, (want_in, _) in _OPENAI_RATES.items():
        dated = f"{model}-2026-07-09"
        assert cost.cost_for_call(dated, 1_000_000, 0) == pytest.approx(want_in), dated


# ── extraction from real LangChain messages ──────────────────────────────────


def test_extract_call_records() -> None:
    msgs = [
        HumanMessage(content="hi"),
        AIMessage(
            content="",
            usage_metadata={
                "input_tokens": 1000,
                "output_tokens": 100,
                "total_tokens": 1100,
                "input_token_details": {"cache_read": 200},
            },
            response_metadata={"model_name": _SONNET},
            tool_calls=[{"name": "read_file", "args": {"path": "x"}, "id": "t0"}],
        ),
        ToolMessage(content="file contents", tool_call_id="t0", name="read_file"),
        AIMessage(
            content="done",
            usage_metadata={"input_tokens": 1500, "output_tokens": 50, "total_tokens": 1550},
            response_metadata={"model_name": _SONNET},
        ),
    ]
    recs = reader.extract_call_records(msgs, "tid", "")
    assert len(recs) == 2
    assert recs[0].seq == 1 and recs[1].seq == 3  # index in the message list
    assert recs[0].input_tokens == 1000
    assert recs[0].cache_read_tokens == 200
    assert recs[0].model == _SONNET
    assert [tc.name for tc in recs[0].tool_calls] == ["read_file"]
    assert recs[1].tool_calls == ()


# ── tool attribution (the next-input-delta rule) ──────────────────────────────


_BASE = "(system + context)"
_GEN = "(model output)"


def _by_tool(callset):
    return {a.tool: a for a in attribution.attribute_tools(callset)}


def _thread_total(callset) -> float:
    return sum(c.usd for c in callset.all_calls())


def test_attribution_sums_to_thread_total() -> None:
    # The breakdown must partition the thread's net (cache-discounted) cost exactly.
    cs = ThreadCallSet(
        "t",
        parent_calls=[
            _call(0, 1000, 100, tools=("read_file",)),
            _call(1, 1600, 50),  # final answer, no tools
        ],
    )
    by = _by_tool(cs)
    rf = by["read_file"]
    # read_file's result (delta 600 minus the prior call's 100 output tokens = 500)
    # is billed once (only re-appears in c1's input).
    assert rf.input_tokens == 500
    assert rf.output_tokens == 0  # output is the model's, not the tool's
    assert rf.usd == 500 * _IN
    assert _BASE in by and _GEN in by
    assert by[_BASE].input_tokens == 2000  # 1000 base re-billed across both calls
    assert by[_GEN].output_tokens == 150  # 100 + 50 generated
    # Exact partition of the thread total.
    assert abs(sum(a.usd for a in by.values()) - _thread_total(cs)) < 1e-9


def test_attribution_persisted_result_is_rebilled() -> None:
    # A tool result that arrives early and lingers is re-billed on every later call,
    # so it costs more than the same result arriving last.
    early = ThreadCallSet(
        "t",
        parent_calls=[
            _call(0, 1000, 100, tools=("read_file",)),  # result enters at c1
            _call(1, 2000, 100, tools=("noop",)),  # +900 ≈ 100 out + 800 result
            _call(2, 2900, 0),  # read_file's 800 still in context → re-billed
        ],
    )
    rf = _by_tool(early)["read_file"]
    # 800 tokens of result, billed in c1 AND c2 → ~1600 billed input tokens.
    assert rf.input_tokens > 800
    assert abs(sum(a.usd for a in attribution.attribute_tools(early)) - _thread_total(early)) < 1e-9


def test_attribution_parallel_tools_split_evenly() -> None:
    cs = ThreadCallSet(
        "t",
        parent_calls=[
            _call(0, 1000, 100, tools=("ls", "ls")),
            _call(1, 1400, 0),  # +400 ≈ 100 out + 300 result, split 150/150
        ],
    )
    by = _by_tool(cs)
    assert by["ls"].calls == 2
    assert by["ls"].input_tokens == 300


def test_attribution_last_call_result_not_rebilled() -> None:
    # A tool whose result is never carried into a later call costs ~0 (only the
    # model's emitting output, which is generation).
    cs = ThreadCallSet("t", parent_calls=[_call(0, 1000, 100, tools=("web_search",))])
    by = _by_tool(cs)
    assert "web_search" not in by  # its result never re-billed
    assert by[_GEN].output_tokens == 100
    assert abs(sum(a.usd for a in by.values()) - _thread_total(cs)) < 1e-9


def test_attribution_compaction_drop_keeps_partition() -> None:
    # Context shrank between calls (compaction) → composition scales down, no crash,
    # and the partition still sums to the thread total.
    cs = ThreadCallSet(
        "t",
        parent_calls=[
            _call(0, 2000, 50, tools=("read_file",)),
            _call(1, 1500, 0),  # drop
        ],
    )
    by = _by_tool(cs)
    assert _BASE in by
    assert abs(sum(a.usd for a in by.values()) - _thread_total(cs)) < 1e-9


def test_attribution_task_subagent_fold() -> None:
    cs = ThreadCallSet(
        "t",
        parent_calls=[_call(0, 1000, 50, tools=("task",)), _call(1, 1200, 0)],
        subagent_calls={"tools:abc": [_call(0, 5000, 200, ns="tools:abc")]},
    )
    by = _by_tool(cs)
    assert "task" in by
    # Downstream subagent cost surfaces as a secondary figure.
    assert by["task"].subagent_internal_usd == cost.cost_for_call(_SONNET, 5000, 200)


# ── execution tree ────────────────────────────────────────────────────────────


def test_turn_tree_nests_tools_and_subagents() -> None:
    cs = ThreadCallSet(
        "t",
        parent_calls=[
            CallRecord(
                "t",
                "",
                0,
                "m0",
                _SONNET,
                1000,
                50,
                1050,
                0,
                cost.cost_for_call(_SONNET, 1000, 50),
                True,
                (ToolCallRef(id="abc", name="task", subagent_type="researcher"),),
                None,
            ),
        ],
        subagent_calls={"tools:abc": [_call(0, 4000, 100, ns="tools:abc")]},
    )
    tree = attribution.build_turn_tree(cs)
    assert tree and tree[0].kind == "turn"
    ai = tree[0].children[0]
    assert ai.kind == "ai_call"
    tool_node = ai.children[0]
    assert tool_node.kind == "tool"
    # The subagent namespace nested under its originating task tool node.
    assert tool_node.children and tool_node.children[0].kind == "subagent"


# ── resolver + origin ─────────────────────────────────────────────────────────


def test_resolve_agent() -> None:
    idx = {"abc": "merli"}
    assert resolver.resolve_agent("abc", idx) == "merli"
    assert resolver.resolve_agent("cog::scout", idx) == "scout"
    assert resolver.resolve_agent("nope", idx) == "unknown"


def test_owning_agent_of_collapses_worker_names() -> None:
    assert resolver.owning_agent_of("merli") == "merli"
    assert resolver.owning_agent_of("merli__cog__pulse_agent__ab12cd") == "merli"
    assert resolver.owning_agent_of("merli__cog__sub__deploy__ab12cd") == "merli"


def test_resolve_agent_collapses_real_cog_worker_ids() -> None:
    """`cog::` ids embed the EPHEMERAL child name (`{parent}__cog__{label}__{hex}`);
    the resolver must hand back the parent or every cog row fragments into a
    one-run agent no member grant matches."""
    idx: dict[str, str] = {}
    assert resolver.resolve_agent("cog::merli__cog__pulse_agent__ab12cd", idx) == "merli"
    assert resolver.resolve_agent("cog::merli__cog__sub__deploy__ab12cd", idx) == "merli"


def test_mining_candidates_include_cog_threads_but_not_orphans() -> None:
    from olano.analytics.threadcost import service

    agent_index = {"merli::t1": "merli"}
    versions = {
        "merli::t1": "v1",  # session-file thread with checkpoints
        "merli::deleted": "v1",  # orphan checkpoint (JSONL deleted) — stays excluded
        "cog::merli__cog__pulse__ab": "v2",  # cognition thread — now mineable
    }
    cands = service._mining_candidates(agent_index, versions)
    assert "merli::t1" in cands
    assert "cog::merli__cog__pulse__ab" in cands
    assert "merli::deleted" not in cands
    # JSONL threads come first so a cog backlog can't starve them of the cap.
    assert cands.index("merli::t1") < cands.index("cog::merli__cog__pulse__ab")


def test_ledger_credits_overlay_carries_by_origin_buckets() -> None:
    from olano.analytics import usage_ledger
    from olano.analytics.threadcost import service

    usage_ledger.record_usage_event(
        "merli", "olano:pro", 1000, 100, 0.0, credits=5.0, origin="cognition"
    )
    usage_ledger.record_usage_event("merli", "anthropic:claude-sonnet-4-6", 300, 30, 0.01)
    overlay = service._ledger_credits_overlay(None)
    ob = overlay["by_origin"]
    assert ob["cognition"]["credits"] == 5.0 and ob["cognition"]["calls"] == 1
    # Unlabeled + unpriced rows still count toward their bucket (usd/tokens/calls
    # accumulate before the credits skip, exactly like by_source).
    assert ob["turns"]["usd"] == 0.01 and ob["turns"]["credits"] == 0.0
    assert ob["turns"]["calls"] == 1


def test_classify_origin() -> None:
    assert resolver.classify_origin("cog::merli", None) == resolver.ORIGIN_AUTONOMOUS
    assert resolver.classify_origin("x", {"role": "peer_inbound"}) == resolver.ORIGIN_PEER
    assert (
        resolver.classify_origin("x", {"role": "user", "content": "[Autonomous scheduled task] go"})
        == resolver.ORIGIN_SCHEDULED
    )
    assert (
        resolver.classify_origin("x", {"role": "user", "content": "hello"})
        == resolver.ORIGIN_INTERACTIVE
    )


# ── derived cache ─────────────────────────────────────────────────────────────


def _row(tid, agent, usd, origin="interactive", calls=3, version="v1", day="2026-06-08", ts=None):
    return cache.CachedThread(
        thread_id=tid,
        agent=agent,
        origin=origin,
        day=day,
        # Mirrors refresh_thread: `day` is the first 10 chars of `last_ts`.
        last_ts=ts if ts is not None else (f"{day}T12:00:00+00:00" if day else ""),
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=0,
        total_tokens=120,
        usd=usd,
        call_count=calls,
        subagent_call_count=0,
        namespaces=1,
        pricing_complete=1,
        model_json='{"anthropic:claude-sonnet-4-6": {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 0, "usd": '
        + str(usd)
        + "}}",
        version=version,
    )


def test_cache_upsert_rank_and_invalidation() -> None:
    conn = cache.connect()
    try:
        cache.upsert(conn, _row("a", "merli", 1.0))
        cache.upsert(conn, _row("b", "merli", 3.0))
        cache.upsert(conn, _row("c", "scout", 2.0))
        agents = cache.rank_agents(conn)
        assert [a["agent"] for a in agents] == ["merli", "scout"]
        assert agents[0]["usd"] == 4.0 and agents[0]["thread_count"] == 2
        threads, total = cache.rank_threads(conn, agent="merli")
        assert total == 2 and [t["thread_id"] for t in threads] == ["b", "a"]
        # Invalidation: version changes when checkpoints are added.
        assert cache.cached_versions(conn)["a"] == "v1"
        cache.upsert(conn, _row("a", "merli", 1.0, version="v2"))
        assert cache.cached_versions(conn)["a"] == "v2"
    finally:
        conn.close()


def test_cache_prune_and_zero_call_filter() -> None:
    conn = cache.connect()
    try:
        cache.upsert(conn, _row("keep", "merli", 1.0))
        cache.upsert(conn, _row("drop", "merli", 1.0))
        cache.upsert(conn, _row("empty", "merli", 0.0, calls=0))
        removed = cache.prune(conn, {"keep", "empty"})
        assert removed == 1  # "drop" removed
        # Zero-call threads are excluded from ranking.
        threads, total = cache.rank_threads(conn, agent="merli")
        assert [t["thread_id"] for t in threads] == ["keep"]
        assert total == 1
    finally:
        conn.close()


def test_cache_since_window_filters_by_last_active_time() -> None:
    """Backs the drill-down's 1/7/30/90-day selector: rows are kept by their
    last-active timestamp, and a blank one (thread with no mineable timestamp)
    counts as now so it never falls out of every window."""
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=29)).isoformat()  # the "30 days" button
    conn = cache.connect()
    try:
        cache.upsert(conn, _row("w-recent", "winagent", 1.0, day=today.isoformat()))
        cache.upsert(
            conn, _row("w-old", "winagent", 5.0, day=(today - timedelta(days=45)).isoformat())
        )
        cache.upsert(conn, _row("w-undated", "winagent", 2.0, day=""))

        # No window ⇒ all history (the pre-existing behaviour).
        rows, total = cache.rank_threads(conn, agent="winagent")
        assert total == 3 and {t["thread_id"] for t in rows} == {"w-recent", "w-old", "w-undated"}

        rows, total = cache.rank_threads(conn, agent="winagent", since=since)
        assert total == 2 and {t["thread_id"] for t in rows} == {"w-recent", "w-undated"}

        agent_row = next(
            a for a in cache.rank_agents(conn, since=since) if a["agent"] == "winagent"
        )
        assert agent_row["usd"] == 3.0 and agent_row["thread_count"] == 2

        assert cache.agent_origin_split(conn, since=since)["winagent"]["interactive"]["usd"] == 3.0

        # A day-shaped bound keeps only the two rows at/after that day.
        rows, total = cache.rank_threads(conn, agent="winagent", since=today.isoformat())
        assert total == 2

        # origin_split is agent-scoped here so leftover rows from other tests
        # (same shared cache DB) can't move the numbers.
        split = cache.origin_split(conn, agents={"winagent"}, since=since)
        assert [(s["origin"], s["usd"]) for s in split] == [("interactive", 3.0)]
    finally:
        conn.execute("DELETE FROM thread_cost WHERE agent = 'winagent'")
        conn.commit()
        conn.close()


def test_cache_migrates_last_ts_in_place_instead_of_dropping(tmp_path) -> None:
    """A schema-3 cache keeps its rows across the bump.

    Dropping would be correct (the table is derived) but it refills at 150
    threads per request, so a big fleet's drill-down would read near-zero until
    someone browsed it back. `last_ts` seeds from `day` and each thread gets its
    real timestamp on the next re-mine.
    """
    import sqlite3

    from olano.config.home import get_olano_paths

    db = get_olano_paths(tmp_path).threadcost_db_path
    db.parent.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(str(db))
    old.executescript(
        "CREATE TABLE thread_cost (thread_id TEXT PRIMARY KEY, agent TEXT, origin TEXT, "
        "day TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, "
        "total_tokens INTEGER, usd REAL, call_count INTEGER, subagent_call_count INTEGER, "
        "namespaces INTEGER, pricing_complete INTEGER, model_json TEXT, version TEXT, "
        "updated_at TEXT); PRAGMA user_version = 3;"
    )
    old.execute(
        "INSERT INTO thread_cost (thread_id, agent, day, usd, call_count) VALUES (?,?,?,?,?)",
        ("legacy", "merli", "2026-06-08", 1.25, 4),
    )
    old.commit()
    old.close()

    conn = cache.connect(tmp_path)
    try:
        row = conn.execute("SELECT * FROM thread_cost WHERE thread_id = 'legacy'").fetchone()
        assert row is not None and row["usd"] == 1.25  # survived the bump
        assert row["last_ts"] == "2026-06-08T12:00:00+00:00"  # seeded from `day`
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        conn.close()


def test_since_bound_maps_window_buttons_to_a_lower_bound() -> None:
    from datetime import datetime, timedelta, timezone

    from olano.analytics.threadcost import service

    today = datetime.now(timezone.utc).date()
    assert service._since_bound(None) is None
    assert service._since_bound(0) is None  # "all history"
    assert service._since_bound(30) == (today - timedelta(days=29)).isoformat()
    assert service._since_bound(90) == (today - timedelta(days=89)).isoformat()
    # "1 day" is the rolling last 24 hours — the same window the Overview's
    # `hours=24` selects, not "threads active today". `hours` wins over `days`.
    bound = service._since_bound(1, 24)
    assert bound is not None and "T" in bound
    cutoff = datetime.fromisoformat(bound)
    assert timedelta(hours=23) <= datetime.now(timezone.utc) - cutoff <= timedelta(hours=24)


def test_drilldown_one_day_window_is_a_rolling_24_hours() -> None:
    """The reported mismatch: the same "1 day" button selected a rolling 24 h on
    the Overview and "threads active today" here, so a thread from 02:00 was in
    one and out of the other depending on the wall clock."""
    from datetime import datetime, timedelta, timezone

    from olano.analytics.threadcost import service

    now = datetime.now(timezone.utc)
    conn = cache.connect()
    try:
        cache.upsert(
            conn, _row("r-2h", "rollagent", 1.0, ts=(now - timedelta(hours=2)).isoformat())
        )
        cache.upsert(
            conn, _row("r-30h", "rollagent", 5.0, ts=(now - timedelta(hours=30)).isoformat())
        )
        since = service._since_bound(1, 24)
        rows, total = cache.rank_threads(conn, agent="rollagent", since=since)
        assert total == 1 and rows[0]["thread_id"] == "r-2h"
        # The 7-day button still keeps both — a shorter window stays a subset.
        rows, total = cache.rank_threads(conn, agent="rollagent", since=service._since_bound(7))
        assert total == 2
    finally:
        conn.execute("DELETE FROM thread_cost WHERE agent = 'rollagent'")
        conn.commit()
        conn.close()


def test_aggregate_day_usage() -> None:
    # Pure aggregation feeding the work journal (by_agent / by_model / totals).
    from olano.analytics.threadcost import service

    rows = [
        {
            "agent": "merli",
            "input_tokens": 100,
            "output_tokens": 20,
            "usd": 0.5,
            "call_count": 3,
            "model_json": '{"gpt-5.5": {"input_tokens": 100, "output_tokens": 20, "usd": 0.5, "calls": 3}}',
        },
        {
            "agent": "scout",
            "input_tokens": 50,
            "output_tokens": 10,
            "usd": 0.2,
            "call_count": 1,
            "model_json": '{"gpt-5.5": {"input_tokens": 50, "output_tokens": 10, "usd": 0.2, "calls": 1}}',
        },
    ]
    out = service._aggregate_day_usage(rows)
    assert round(out["totals"]["usd"], 6) == 0.7
    assert out["totals"]["calls"] == 4
    assert out["by_agent"]["merli"]["usd"] == 0.5
    assert out["by_model"]["gpt-5.5"]["calls"] == 4
    assert out["by_model"]["gpt-5.5"]["input_tokens"] == 150


def test_origins_split_empty_when_no_db() -> None:
    # No threads.db in the test home → empty, well-formed payload (no crash).
    from olano.analytics.threadcost import service

    out = service.origins()
    assert out["split"] == []
    assert out["interactive"]["usd"] == 0.0
    assert out["non_interactive"]["usd"] == 0.0


def test_agents_ranked_empty_when_no_db() -> None:
    from olano.analytics.threadcost import service

    out = service.agents_ranked()
    assert out["agents"] == []
    assert out["totals"]["usd"] == 0.0
    assert out["totals"]["credits"] == 0.0


def test_attach_credits_book_grafts_managed_charge_onto_mined_rows() -> None:
    """The drill-down mines dollars only — an Olano-managed call prices to $0
    there by design — so the credits book is grafted from the usage ledger.

    Without it a credits-first fleet reads as a near-$0 drill-down with no hint
    that the spend is recorded elsewhere, and the page's totals silently
    disagree with the Insights Overview's.
    """
    from olano.analytics.threadcost import service

    agents = [
        {"agent": "merli", "usd": 2.5, "thread_count": 3, "call_count": 40},
        {"agent": "scout", "usd": 0.0, "thread_count": 1, "call_count": 5},
    ]
    service._attach_credits_book(agents, {"merli": 12.0, "ghost": 7.5})

    by_name = {a["agent"]: a for a in agents}
    assert by_name["merli"]["credits"] == 12.0
    # A mined agent with no managed calls reads 0, never the fleet's number.
    assert by_name["scout"]["credits"] == 0.0
    # An agent the ledger knows but the cache doesn't still appears, honestly
    # empty, so this page's credits total matches the Overview's.
    assert by_name["ghost"]["credits"] == 7.5
    assert by_name["ghost"]["usd"] == 0.0
    assert by_name["ghost"]["thread_count"] == 0 and by_name["ghost"]["call_count"] == 0


def test_attach_credits_book_ignores_zero_credit_ledger_agents() -> None:
    # A BYOK-only agent is in the ledger with credits 0; inventing a phantom
    # drill-down row for it would just be noise.
    from olano.analytics.threadcost import service

    agents: list[dict] = []
    service._attach_credits_book(agents, {"byok-only": 0.0})
    assert agents == []


# ── dual-dialect handle: SQL translation (portable, no real DB needed) ─────────


class _CapturingConn:
    """Records the SQL/params last passed to ``execute`` (no real DB)."""

    def __init__(self) -> None:
        self.sql: str | None = None
        self.params: tuple = ()

    def execute(self, sql: str, params: tuple = ()):  # test stub
        self.sql = sql
        self.params = params
        return []


def test_threaddbhandle_postgres_escapes_literal_percent() -> None:
    """Postgres must double literal '%' (e.g. LIKE 'tools:%') *before* '?'→'%s'.

    psycopg uses %-style placeholders, so a bare '%' followed by a non-placeholder
    char raises ProgrammingError. Regression for the LIKE 'tools:%' subagent query.
    """
    conn = _CapturingConn()
    handle = reader.ThreadDBHandle("postgres", conn, saver=object())  # type: ignore[arg-type]
    handle.execute(
        "SELECT thread_id FROM checkpoints WHERE checkpoint_ns LIKE 'tools:%' AND thread_id = ?",
        ("t",),
    )
    assert conn.sql == (
        "SELECT thread_id FROM checkpoints WHERE checkpoint_ns LIKE 'tools:%%' AND thread_id = %s"
    )
    assert conn.params == ("t",)


def test_threaddbhandle_sqlite_passes_sql_through() -> None:
    """SQLite leaves '?' placeholders and literal '%' untouched."""
    conn = _CapturingConn()
    handle = reader.ThreadDBHandle("sqlite", conn, saver=object())  # type: ignore[arg-type]
    sql = "SELECT thread_id FROM checkpoints WHERE checkpoint_ns LIKE 'tools:%' AND thread_id = ?"
    handle.execute(sql, ("t",))
    assert conn.sql == sql


# ── guarded read-only end-to-end against the real checkpoint store ─────────────


@pytest.mark.skipif(not _REAL_DB.exists(), reason="no real threads.db on this machine")
def test_real_checkpoint_store_invariants() -> None:
    """Read (never write) a few real threads and assert structural invariants.

    Validates the reader/attribution against real serialized messages without
    asserting any specific values (data varies per machine).
    """
    home = get_default_olano_home()
    conn = reader.open_thread_db(home)
    try:
        # Prefer threads with subagent namespaces so the union path is exercised.
        with_sub = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints WHERE checkpoint_ns LIKE 'tools:%' LIMIT 3"
            ).fetchall()
        ]
        sample = with_sub or reader.list_thread_ids(conn)[:3]
        assert sample, "expected at least one thread in the real store"
        for tid in sample:
            cs = reader.read_thread_callset(conn, tid)
            calls = cs.all_calls()
            # Per-call totals are non-negative and total == input + output.
            for c in calls:
                assert c.input_tokens >= 0 and c.output_tokens >= 0
                assert c.usd >= 0.0
            # Tool attribution partitions the thread's net (cache-discounted) cost.
            tools = attribution.attribute_tools(cs)
            thread_usd = sum(c.usd for c in calls)
            assert abs(sum(t.usd for t in tools) - thread_usd) < 1e-6
            assert all(t.usd >= -1e-9 for t in tools)
            # Tree builds without error and is JSON-serializable.
            tree = attribution.build_turn_tree(cs)
            assert all("kind" in n.as_dict() for n in tree)
    finally:
        conn.close()


def test_build_thread_agent_index_emits_scoped_keys(tmp_path) -> None:
    """The index must key by the agent-scoped checkpoint id so it joins directly
    against threads.db (whose rows the runner now writes as ``<agent>::<stem>``),
    and two agents sharing a human's thread stem stay distinct (no last-writer-wins).
    """
    sessions = tmp_path / "sessions"
    for agent, stem in [("marcus", "5550001234"), ("cindy", "5550001234"), ("cindy", "solo")]:
        d = sessions / agent
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{stem}.jsonl").write_text("{}\n", encoding="utf-8")

    # get_olano_paths(home).sessions_dir resolves to <home>/sessions.
    idx = resolver.build_thread_agent_index(tmp_path)

    assert idx["marcus::5550001234"] == "marcus"
    assert idx["cindy::5550001234"] == "cindy"  # NOT collapsed with marcus's
    assert idx["cindy::solo"] == "cindy"
    # Bare (unscoped) stems must not appear.
    assert "5550001234" not in idx
    assert "solo" not in idx
    # resolve_agent round-trips the scoped key; cog:: fallback still works.
    assert resolver.resolve_agent("cindy::5550001234", idx) == "cindy"
    assert resolver.resolve_agent("cog::scout", idx) == "scout"


# ── read-error surfacing (silent-zero poisoning regression) ────────────────────
#
# On a managed deployment, checkpoint reads failed silently: every
# namespace's get_tuple error was swallowed at debug, the thread mined to zero
# calls, and the zero row was cached under the CURRENT version — so it was never
# re-mined and Insights stayed blank forever. These pin the fixed behavior:
# errors are counted on the callset, error-poisoned zeros are not cached, and
# the warmer reports them.


class _FetchResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _RaisingSaver:
    def get_tuple(self, cfg):
        raise RuntimeError("blob deserialization failed")


class _FakeThreadsConn:
    """Serves thread_namespaces / thread_version; saver.get_tuple always raises."""

    backend = "sqlite"
    saver = _RaisingSaver()

    def execute(self, sql, params=()):
        if "DISTINCT checkpoint_ns" in sql:
            return _FetchResult([("",), ("tools:abc",)])
        if "GROUP BY thread_id" in sql:  # all_thread_versions (also has COUNT(*))
            return _FetchResult([("a::t1", 3, "ckpt-3")])
        if "COUNT(*)" in sql:  # thread_version
            return _FetchResult([(3, "ckpt-3")])
        return _FetchResult([])

    def close(self):
        pass


def test_read_thread_callset_counts_read_errors() -> None:
    cs = reader.read_thread_callset(_FakeThreadsConn(), "a::t1")
    assert cs.all_calls() == []
    assert cs.read_errors == 2  # both namespaces failed
    assert "blob deserialization failed" in cs.last_error
    assert cs.version == "3:ckpt-3"  # version still computed (metadata path works)


def test_refresh_thread_does_not_cache_error_poisoned_zero(tmp_path) -> None:
    from olano.analytics.threadcost import service

    cc = cache.connect(tmp_path)
    try:
        row = service.refresh_thread(cc, _FakeThreadsConn(), "a::t1", {"a::t1": "a"})
        assert row.get("skipped") is True
        assert "blob deserialization failed" in row.get("error", "")
        # Nothing cached: the next warm pass must retry this thread.
        assert cache.cached_versions(cc) == {}
    finally:
        cc.close()


def test_warm_cache_reports_errors_and_recovers(tmp_path, monkeypatch) -> None:
    from olano.analytics.threadcost import service

    cc = cache.connect(tmp_path)
    try:
        status = service.warm_cache(cc, _FakeThreadsConn(), {"a::t1": "a"})
        assert status["total"] == 1 and status["refreshed"] == 0
        assert status["errors"] == 1
        assert "blob deserialization failed" in status["last_error"]
        assert cache.cached_versions(cc) == {}

        # Reads recover → the same thread mines and caches on the next pass.
        good = ThreadCallSet(
            "a::t1",
            parent_calls=[_call(0, 100, 10)],
            version="3:ckpt-3",
            ts="2026-07-18T00:00:00+00:00",
        )
        monkeypatch.setattr(service.reader, "read_thread_callset", lambda *a, **k: good)
        status = service.warm_cache(cc, _FakeThreadsConn(), {"a::t1": "a"})
        assert status["refreshed"] == 1 and status["errors"] == 0
        assert cache.cached_versions(cc) == {"a::t1": "3:ckpt-3"}
    finally:
        cc.close()


def _good_callset(version: str = "3:ckpt-3") -> ThreadCallSet:
    return ThreadCallSet(
        "a::t1",
        parent_calls=[_call(0, 100, 10)],
        version=version,
        ts="2026-07-18T00:00:00+00:00",
    )


def test_version_key_is_the_newest_checkpoint_id_not_the_row_count() -> None:
    # The sweep deletes rows (count moves) without a new turn (max id stays).
    assert reader.version_key("3:ckpt-3") == reader.version_key("1:ckpt-3")
    assert reader.version_key("3:ckpt-3") != reader.version_key("4:ckpt-4")
    assert reader.version_key(None) == "" and reader.version_key("") == ""


def test_warm_cache_does_not_re_mine_a_thread_the_sweep_trimmed(tmp_path, monkeypatch) -> None:
    """A version that differs only in its row count is not stale: the sweep
    trims hundreds of threads at once, and re-mining them all on the next
    Overview request is the 12 GB climb this guards against."""
    from olano.analytics.threadcost import service

    service._reset_mining_bounds_for_tests()
    reads: list[str] = []

    def read(_conn, tid):
        reads.append(tid)
        return _good_callset()

    monkeypatch.setattr(service.reader, "read_thread_callset", read)
    cc = cache.connect(tmp_path)
    try:
        status = service.warm_cache(cc, _FakeThreadsConn(), {"a::t1": "a"})
        assert status["refreshed"] == 1 and reads == ["a::t1"]
        # Same newest checkpoint, fewer rows (as after a sweep): nothing to do.
        monkeypatch.setattr(
            service.reader, "all_thread_versions", lambda _conn: {"a::t1": "1:ckpt-3"}
        )
        status = service.warm_cache(cc, _FakeThreadsConn(), {"a::t1": "a"})
        assert status["stale"] == 0 and status["refreshed"] == 0 and reads == ["a::t1"]
        # A new turn (newer id) is stale again.
        monkeypatch.setattr(
            service.reader, "all_thread_versions", lambda _conn: {"a::t1": "2:ckpt-4"}
        )
        status = service.warm_cache(cc, _FakeThreadsConn(), {"a::t1": "a"})
        assert status["stale"] == 1 and status["refreshed"] == 1 and reads == ["a::t1", "a::t1"]
    finally:
        cc.close()


def test_warm_cache_refuses_a_thread_whose_history_is_over_the_cap(tmp_path, monkeypatch) -> None:
    """The miner never reads a thread it cannot afford to decode, and does not
    ask again until the thread's version moves."""
    from olano.analytics.threadcost import service

    service._reset_mining_bounds_for_tests()
    monkeypatch.setenv(service._MAX_HISTORY_ENV, "1")  # 1 MB
    sizes: list[str] = []

    def size(_conn, tid):
        sizes.append(tid)
        return 5 * 1024 * 1024

    reads: list[str] = []

    def read(_conn, tid):
        reads.append(tid)
        return _good_callset()

    monkeypatch.setattr(service.reader, "pending_history_bytes", size)
    monkeypatch.setattr(service.reader, "read_thread_callset", read)
    cc = cache.connect(tmp_path)
    try:
        status = service.warm_cache(cc, _FakeThreadsConn(), {"a::t1": "a"})
        assert status["too_large"] == 1 and status["refreshed"] == 0 and status["errors"] == 0
        assert reads == [] and sizes == ["a::t1"]
        # Nothing cached, nothing poisoned: an earlier row would have survived.
        assert cache.cached_versions(cc) == {}
        # The next pass does not even measure it again at the same version...
        status = service.warm_cache(cc, _FakeThreadsConn(), {"a::t1": "a"})
        assert status["too_large"] == 1 and sizes == ["a::t1"] and reads == []
        # ...but a new turn (new version) is measured afresh, and once the
        # snapshot brought it under the cap it mines like any other thread.
        monkeypatch.setattr(
            service.reader, "all_thread_versions", lambda _conn: {"a::t1": "9:ckpt-9"}
        )
        monkeypatch.setattr(service.reader, "pending_history_bytes", lambda _c, _t: 1024)
        status = service.warm_cache(cc, _FakeThreadsConn(), {"a::t1": "a"})
        assert status["too_large"] == 0 and status["refreshed"] == 1 and reads == ["a::t1"]
        assert cache.cached_versions(cc) == {"a::t1": "9:ckpt-9"}
    finally:
        cc.close()


def test_an_unknown_history_size_mines_as_before(tmp_path, monkeypatch) -> None:
    from olano.analytics.threadcost import service

    service._reset_mining_bounds_for_tests()
    monkeypatch.setattr(service.reader, "pending_history_bytes", lambda _c, _t: None)
    monkeypatch.setattr(service.reader, "read_thread_callset", lambda *_a: _good_callset())
    cc = cache.connect(tmp_path)
    try:
        status = service.warm_cache(cc, _FakeThreadsConn(), {"a::t1": "a"})
        assert status["refreshed"] == 1 and status["too_large"] == 0
    finally:
        cc.close()


def test_pending_history_bytes_is_unknown_on_sqlite() -> None:
    assert reader.pending_history_bytes(_FakeThreadsConn(), "a::t1") is None


def test_thread_calls_refuses_a_thread_over_the_cap(tmp_path, monkeypatch) -> None:
    from olano.analytics.threadcost import service

    service._reset_mining_bounds_for_tests()
    monkeypatch.setenv(service._MAX_HISTORY_ENV, "1")
    monkeypatch.setattr(service, "_has_thread_db", lambda _home: True)
    monkeypatch.setattr(service.reader, "open_thread_db", lambda _home: _FakeThreadsConn())
    monkeypatch.setattr(service.reader, "pending_history_bytes", lambda _c, _t: 3 * 1024 * 1024)

    def boom(*_a):
        raise AssertionError("read_thread_callset must not run on a too-large thread")

    monkeypatch.setattr(service.reader, "read_thread_callset", boom)
    out = service.thread_calls("a::t1", home=tmp_path)
    assert out["calls"] == [] and out["totals"]["call_count"] == 0
    assert out["skipped"]["reason"] == "history_too_large"
    assert out["skipped"]["bytes"] == 3 * 1024 * 1024
    assert out["skipped"]["cap_bytes"] == 1024 * 1024


@pytest.mark.parametrize("endpoint", ["raw", "tools", "diagnostics"])
def test_every_whole_thread_reader_refuses_a_thread_over_the_cap(
    endpoint, tmp_path, monkeypatch
) -> None:
    """The warmer and ``thread_calls`` were gated first; the raw-call
    inspector, the tool rollup and the diagnostics probe read whole threads
    through the same two reader functions and must take the same cap, or a
    drill-down click, the Insights tools card or the health page is the one
    request that loads the thread everything else refuses."""
    from olano.analytics.threadcost import service

    service._reset_mining_bounds_for_tests()
    monkeypatch.setenv(service._MAX_HISTORY_ENV, "1")
    monkeypatch.setattr(service, "_has_thread_db", lambda _home: True)
    monkeypatch.setattr(service.reader, "thread_store_available", lambda _home: True)
    monkeypatch.setattr(service.reader, "open_thread_db", lambda _home: _FakeThreadsConn())
    monkeypatch.setattr(service.reader, "pending_history_bytes", lambda _c, _t: 3 * 1024 * 1024)
    monkeypatch.setattr(service.resolver, "build_thread_agent_index", lambda _home: {"a::t1": "a"})
    reads: list[str] = []

    def read_callset(_conn, tid):
        reads.append(tid)
        return _good_callset()

    def read_messages(_saver, tid, _ns):
        reads.append(tid)
        return [], None

    monkeypatch.setattr(service.reader, "read_thread_callset", read_callset)
    monkeypatch.setattr(service.reader, "read_namespace_messages_ex", read_messages)

    if endpoint == "raw":
        out = service.call_raw("a::t1", "", 0, home=tmp_path)
        assert out["found"] is False
        skipped = out["skipped"]
    elif endpoint == "tools":
        out = service.tools_rollup(thread_id="a::t1", home=tmp_path)
        assert out["tools"] == [] and out["totals"]["threads_scanned"] == 0
        (row,) = out["skipped"]
        assert row["thread_id"] == "a::t1"
        skipped = row
    else:
        probe = service.diagnostics(home=tmp_path)["probe"]
        assert probe["thread_id"] == "a::t1" and probe["ok"] is False
        assert probe["error"] == "history_too_large"
        skipped = probe["skipped"]
    assert reads == []
    assert skipped["reason"] == "history_too_large"
    assert skipped["bytes"] == 3 * 1024 * 1024
    assert skipped["cap_bytes"] == 1024 * 1024


def test_only_one_warm_pass_runs_at_a_time(tmp_path, monkeypatch) -> None:
    """A second request arriving mid-pass serves the cache instead of mining
    the same threads alongside the first."""
    from olano.analytics.threadcost import service

    service._reset_mining_bounds_for_tests()
    monkeypatch.setattr(service.reader, "read_thread_callset", lambda *_a: _good_callset())
    cc = cache.connect(tmp_path)
    try:
        assert service._WARM_LOCK.acquire(blocking=False)
        try:
            status = service.warm_cache(cc, _FakeThreadsConn(), {"a::t1": "a"})
        finally:
            service._WARM_LOCK.release()
        assert status["busy"] is True and status["refreshed"] == 0
        assert status["remaining"] == 1 and cache.cached_versions(cc) == {}
        status = service.warm_cache(cc, _FakeThreadsConn(), {"a::t1": "a"})
        assert "busy" not in status and status["refreshed"] == 1
    finally:
        cc.close()


def test_read_namespace_messages_ex_returns_error() -> None:
    messages, error = reader.read_namespace_messages_ex(_RaisingSaver(), "a::t1", "")
    assert messages == []
    assert error is not None and "blob deserialization failed" in error
    # The plain wrapper stays list-only for existing callers.
    assert reader.read_namespace_messages(_RaisingSaver(), "a::t1", "") == []


# ── the usage ledger is the Insights source of truth ──────────────────────────


def _write_ledger_events(n: int = 2) -> None:
    from olano.analytics.usage_ledger import record_usage_event

    for _ in range(n):
        record_usage_event(
            agent="merli",
            model=_SONNET,
            input_tokens=100,
            output_tokens=50,
            usd=0.001,
        )


def test_usage_rollup_reads_the_ledger() -> None:
    # conftest pins a tmp LIONCLAW_HOME with the sqlite backend and no threads.db,
    # so the checkpoint side has nothing; the ledger must feed the Overview.
    from olano.analytics.threadcost import service

    _write_ledger_events(2)
    data = service.usage_rollup(30)
    assert data["source"] == "usage_ledger"
    assert data["totals"]["calls"] == 2
    assert data["totals"]["input_tokens"] == 200
    assert data["by_agent"][0]["agent"] == "merli"


def test_usage_rollup_empty_everywhere_reports_the_ledger_source() -> None:
    """Nothing recorded anywhere still names the store that was read.

    The label used to be ``checkpoint_cache`` because mining was the primary
    source and the ledger only a fallback; that priority is now inverted (a
    checkpoint is a snapshot, not a log), so an empty window is an empty ledger.
    """
    from olano.analytics.threadcost import service

    data = service.usage_rollup(30)
    assert data["totals"]["calls"] == 0
    assert data["source"] == "usage_ledger"


def test_day_usage_reads_the_ledger() -> None:
    from datetime import datetime, timezone

    from olano.analytics.threadcost import service

    _write_ledger_events(3)
    today = datetime.now(timezone.utc).date().isoformat()
    data = service.day_usage(today)
    assert data["totals"]["calls"] == 3
    assert data["by_agent"]["merli"]["input_tokens"] == 300
    # A day with no activity anywhere stays empty.
    assert service.day_usage("2001-01-01")["totals"]["calls"] == 0


def test_day_usage_counts_folded_sub_calls_not_ledger_rows() -> None:
    """One ledger row can fold a tool-heavy turn's model sub-calls; the journal
    must count those, like every window rollup does."""
    from datetime import datetime, timezone

    from olano.analytics.threadcost import service
    from olano.analytics.usage_ledger import record_usage_event

    record_usage_event("merli", _SONNET, 100, 50, 0.001, calls=5)
    today = datetime.now(timezone.utc).date().isoformat()
    assert service.day_usage(today)["totals"]["calls"] == 5


# ── one source per day: mined checkpoints only fill pre-ledger history ────────


def _seed_cached_thread(day: str, *, agent: str = "merli", usd: float, calls: int) -> None:
    """Write one mined thread row bucketed to ``day`` straight into the cache."""
    from contextlib import closing as _closing

    from olano.analytics.threadcost import cache

    with _closing(cache.connect()) as cc:
        cache.upsert(
            cc,
            cache.CachedThread(
                thread_id=f"{agent}::{day}-{calls}",
                agent=agent,
                origin="interactive",
                day=day,
                last_ts=f"{day}T12:00:00+00:00",
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=0,
                total_tokens=15,
                usd=usd,
                call_count=calls,
                subagent_call_count=0,
                namespaces=1,
                pricing_complete=1,
                model_json=json.dumps({_SONNET: {"usd": usd, "calls": calls}}),
                version="v1",
            ),
        )


def test_mined_rows_never_double_count_a_day_the_ledger_covers() -> None:
    """The bug this guards: the Overview mined threads.db for usd/calls while the
    ledger fed by_source and the 1-day view, so one page quoted two books and
    "1 day" could out-total "7 days"."""
    from datetime import datetime, timezone

    from olano.analytics.threadcost import service

    today = datetime.now(timezone.utc).date().isoformat()
    _write_ledger_events(2)  # 2 calls, $0.002, today
    _seed_cached_thread(today, usd=99.0, calls=500)

    data = service.usage_rollup(30)
    assert data["source"] == "usage_ledger"
    assert data["totals"]["calls"] == 2
    assert data["totals"]["usd"] == pytest.approx(0.002)
    # The day the ledger owns reads exactly what the ledger says.
    today_row = next(d for d in data["daily"] if d["date"] == today)
    assert today_row["calls"] == 2


def test_mined_rows_fill_days_the_ledger_never_recorded() -> None:
    """A window reaching past the ledger's first row keeps its history rather
    than showing a cliff of zeros."""
    from datetime import datetime, timedelta, timezone

    from olano.analytics.threadcost import service

    today = datetime.now(timezone.utc).date()
    old = (today - timedelta(days=5)).isoformat()
    _write_ledger_events(2)
    _seed_cached_thread(old, usd=1.5, calls=7)

    data = service.usage_rollup(30)
    assert data["source"] == "usage_ledger+checkpoint_prehistory"
    assert data["totals"]["calls"] == 9
    assert data["totals"]["usd"] == pytest.approx(1.502)
    old_row = next(d for d in data["daily"] if d["date"] == old)
    assert old_row["calls"] == 7 and old_row["usd"] == pytest.approx(1.5)
    # Cumulatives are re-walked over the grafted series.
    assert data["daily"][-1]["cum_usd"] == pytest.approx(1.502)


def test_mined_only_window_still_reports_the_checkpoint_source() -> None:
    from datetime import datetime, timedelta, timezone

    from olano.analytics.threadcost import service

    old = (datetime.now(timezone.utc).date() - timedelta(days=3)).isoformat()
    _seed_cached_thread(old, usd=2.0, calls=4)
    data = service.usage_rollup(30)
    assert data["source"] == "checkpoint_cache"
    assert data["totals"]["calls"] == 4


def test_an_idle_day_inside_the_ledgers_era_is_never_mined() -> None:
    """The seam is the ledger's FIRST day, not "any day with no ledger rows".

    A thread's last checkpoint can land a day after its last model call, so a
    quiet day inside the ledger's era can still carry a mined row — grafting it
    would re-count calls the ledger already has on the neighbouring day, and (as
    the seam would then move with the window) let a 7-day view out-total a
    30-day one.
    """
    from datetime import datetime, timedelta, timezone

    from olano.analytics.threadcost import service
    from olano.analytics.usage_ledger import record_usage_event

    now = datetime.now(timezone.utc)
    record_usage_event("merli", _SONNET, 100, 50, 0.001, ts=(now - timedelta(days=20)).isoformat())
    record_usage_event("merli", _SONNET, 100, 50, 0.001)
    quiet = (now.date() - timedelta(days=10)).isoformat()
    _seed_cached_thread(quiet, usd=99.0, calls=500)

    data = service.usage_rollup(30)
    assert data["source"] == "usage_ledger"
    assert data["totals"]["calls"] == 2
    assert next(d for d in data["daily"] if d["date"] == quiet)["calls"] == 0
    # The journal reads the same seam, so it agrees with the window.
    assert service.day_usage(quiet)["totals"]["calls"] == 0


def test_shorter_windows_never_out_total_longer_ones() -> None:
    """The reported bug, as an invariant: every window reads one source per day,
    so 1 day ⊆ 7 days ⊆ 30 days for spend, calls and tokens alike."""
    from datetime import datetime, timedelta, timezone

    from olano.analytics.threadcost import service
    from olano.analytics.usage_ledger import record_usage_event

    now = datetime.now(timezone.utc)
    for hours_back in (0, 2, 30, 26 * 24):
        record_usage_event(
            "merli",
            _SONNET,
            100,
            50,
            0.001,
            calls=3,
            ts=(now - timedelta(hours=hours_back)).isoformat(),
        )
    # A mined thread from before the ledger's first row (26 days back), inside
    # the 30-day window — the graft the longer window picks up and the shorter
    # ones must not.
    _seed_cached_thread((now.date() - timedelta(days=28)).isoformat(), usd=5.0, calls=11)

    day1 = service.overview(1, hours=24)["usage"]["totals"]
    day7 = service.overview(7)["usage"]["totals"]
    day30 = service.overview(30)["usage"]["totals"]
    for metric in ("calls", "usd", "total_tokens", "credits"):
        assert day1[metric] <= day7[metric] <= day30[metric], metric
    assert day30["calls"] > day7["calls"]  # the older events really are in there


def test_agents_ranked_money_matches_the_overview() -> None:
    """The reported mismatch: the drill-down ranked agents by mined spend while
    the Overview read the ledger, so a fleet's top spender showed $7.81 against
    $2,800 for the same window. Both now read the ledger; only `thread_count`
    and `inspectable_usd` stay mined."""
    from olano.analytics.threadcost import service
    from olano.analytics.usage_ledger import record_usage_event

    # 40 scheduled turns; the checkpoint store kept one small conversation.
    for _ in range(40):
        record_usage_event("notion", _SONNET, 1000, 200, 2.0, calls=50, origin="scheduled")
    record_usage_event("notion", _SONNET, 100, 50, 1.0, calls=2, origin="interactive")
    _seed_cached_thread(
        datetime.now(timezone.utc).date().isoformat(), agent="notion", usd=0.5, calls=3
    )

    ranked = service.agents_ranked()
    row = next(a for a in ranked["agents"] if a["agent"] == "notion")
    overview = service.overview(30)["usage"]
    by_agent = next(a for a in overview["by_agent"] if a["agent"] == "notion")

    assert row["usd"] == pytest.approx(by_agent["usd"]) == pytest.approx(81.0)
    assert row["call_count"] == by_agent["calls"] == 2002
    # The origin split rides the ledger too, so scheduled work lands in it.
    assert row["autonomous_usd"] == pytest.approx(80.0)
    assert row["interactive_usd"] == pytest.approx(1.0)
    # …and the mined half says how much of that is actually openable.
    assert row["inspectable_usd"] == pytest.approx(0.5)
    assert row["thread_count"] == 1


def test_origin_cards_split_the_ledger_not_the_checkpoints() -> None:
    """The two cards above the agent list must agree with it — an autonomous
    bucket mined from checkpoints under-reports the unattended side hardest,
    which is the side an operator opens this page to check."""
    from olano.analytics.threadcost import service
    from olano.analytics.usage_ledger import record_usage_event

    record_usage_event("notion", _SONNET, 100, 50, 9.0, calls=30, origin="scheduled")
    record_usage_event("notion", _SONNET, 100, 50, 1.0, calls=2, origin="interactive")
    # A legacy row with no origin is a plain agent turn, per the ledger's docs.
    record_usage_event("notion", _SONNET, 100, 50, 0.5, calls=1)

    out = service.origins()
    assert out["non_interactive"]["usd"] == pytest.approx(9.0)
    assert out["non_interactive"]["call_count"] == 30
    assert out["interactive"]["usd"] == pytest.approx(1.5)
    assert out["interactive"]["call_count"] == 3
    ranked = service.agents_ranked()["totals"]
    assert out["interactive"]["usd"] + out["non_interactive"]["usd"] == pytest.approx(ranked["usd"])


def test_usage_ledger_carries_the_thread_it_ran_on() -> None:
    """Ranking threads from the ledger needs the thread id on the row; it is
    optional so pre-upgrade rows still parse."""
    from olano.analytics import usage_ledger

    usage_ledger.record_usage_event("merli", _SONNET, 1, 1, 0.0, thread_id="merli::abc")
    usage_ledger.record_usage_event("merli", _SONNET, 1, 1, 0.0)
    events = usage_ledger.read_usage_events()
    assert [e.thread_id for e in events] == ["merli::abc", ""]
    # Absent rather than empty on the wire, so old readers are unaffected.
    assert "thread_id" not in events[1].to_json()


def test_overview_totals_agree_with_their_own_billing_split() -> None:
    """`totals` and `by_source` used to come from different stores, so the BYOK
    card and the spend chart beside it disagreed by orders of magnitude."""
    from olano.analytics.threadcost import service
    from olano.analytics.usage_ledger import record_usage_event

    record_usage_event("merli", _SONNET, 100, 50, 0.01, calls=2)
    record_usage_event("scout", "olano:pro", 200, 60, 0.0, credits=12.0, calls=3)

    for payload in (service.overview(7)["usage"], service.overview(1, hours=24)["usage"]):
        assert payload["totals"]["calls"] == sum(r["calls"] for r in payload["by_source"])
        assert payload["totals"]["usd"] == pytest.approx(
            sum(r["usd"] for r in payload["by_source"])
        )
        assert payload["totals"]["credits"] == pytest.approx(
            sum(r["credits"] for r in payload["by_source"])
        )
        # …and with the series the charts are drawn from.
        series = payload.get("hourly") or payload["daily"]
        assert payload["totals"]["calls"] == sum(p["calls"] for p in series)
        assert payload["totals"]["usd"] == pytest.approx(sum(p["usd"] for p in series))


# ── diagnostics payload ────────────────────────────────────────────────────────


def test_diagnostics_reports_backend_store_and_ledger() -> None:
    from olano.analytics.threadcost import service

    _write_ledger_events(1)
    diag = service.diagnostics()
    assert diag["backend"] == "sqlite"
    assert diag["store_available"] is False  # tmp home has no threads.db
    assert diag["ledger"]["events"] == 1
    for key in ("session_threads", "checkpoint_threads", "cached_threads", "probe"):
        assert key in diag


def test_diagnostics_reports_ledger_vs_mined_drift() -> None:
    """The two stores can only be compared if something prints both. A mined
    count far under the ledger's is the signature of compaction eating the
    checkpoint history — and of any page that mixes the two."""
    from datetime import datetime, timedelta, timezone

    from olano.analytics.threadcost import service

    _write_ledger_events(4)
    _seed_cached_thread(
        (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat(), usd=0.5, calls=1
    )
    win = service.diagnostics()["window_30d"]
    assert win["ledger"]["calls"] == 4
    assert win["mined"]["calls"] == 1
    assert win["mined_call_ratio"] == 0.25
