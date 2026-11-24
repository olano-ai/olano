"""The checkpoint garbage collector keeps exactly what a load needs.

Runs a real LangGraph graph with deepagents' ``messages`` reducer on a
``DeltaChannel`` (snapshot every 5 updates here, 50 in deepagents) through a
real ``AsyncPostgresSaver``, including a node that rewrites the whole message
list the way the security middleware used to, and a subgraph that leaves
``<node>:<task>`` namespaces behind. Then it sweeps and proves the thread's
state is byte-for-byte what it was, that the thread keeps working, and that
the history actually shrank.

Skipped like ``test_checkpoints_postgres.py``: needs the postgres extra plus
``LIONCLAW_TEST_POSTGRES_URL`` or testcontainers + Docker.
"""

from __future__ import annotations

import os
import re
import uuid
from contextlib import closing
from typing import Annotated, Any, TypedDict

import pytest
from deepagents._messages_reducer import _messages_delta_reducer
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.channels.delta import DeltaChannel

from olano.config.home import is_postgres_available

try:
    from testcontainers.postgres import PostgresContainer

    _HAS_TESTCONTAINERS = True
except Exception:  # pragma: no cover - optional dev dep
    _HAS_TESTCONTAINERS = False

_EXTERNAL_URL = os.environ.get("LIONCLAW_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not (is_postgres_available() and (_HAS_TESTCONTAINERS or _EXTERNAL_URL)),
    reason=(
        'needs the postgres extra (uv add "olano[postgres]") plus '
        "LIONCLAW_TEST_POSTGRES_URL or testcontainers[postgres] + Docker"
    ),
)


@pytest.fixture(scope="module")
def pg_url():
    if _EXTERNAL_URL:
        yield re.sub(r"^postgresql\+\w+://", "postgresql://", _EXTERNAL_URL)
        return
    if not _HAS_TESTCONTAINERS:  # pragma: no cover
        pytest.skip("testcontainers not installed")
    try:
        container = PostgresContainer("postgres:16")
        container.start()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"could not start Postgres testcontainer (Docker?): {exc}")
    try:
        yield re.sub(r"^postgresql\+\w+://", "postgresql://", container.get_connection_url())
    finally:
        container.stop()


def _files_reducer(state: dict | None, writes: list[dict]) -> dict:
    out = dict(state or {})
    for w in writes:
        out.update(w)
    return out


class SubState(TypedDict):
    messages: Annotated[list, DeltaChannel(_messages_delta_reducer, snapshot_frequency=5)]


class State(TypedDict):
    messages: Annotated[list, DeltaChannel(_messages_delta_reducer, snapshot_frequency=5)]
    files: Annotated[dict, DeltaChannel(_files_reducer, snapshot_frequency=5)]
    note: str


def _build_graph(saver: Any):
    from langgraph.graph import END, START, StateGraph

    def model(state: State) -> dict:
        n = len(state["messages"])
        return {
            "messages": [AIMessage(content=f"reply {n}", id=f"ai-{n}")],
            "files": {f"f{n % 3}": f"v{n}"},
            "note": f"n{n}",
        }

    def rewrite_whole_list(state: State) -> dict:
        # What the security middleware did until the fix: the entire list back.
        msgs = list(state["messages"])
        last = msgs[-1]
        msgs[-1] = AIMessage(content=last.content + " (scrubbed)", id=last.id)
        return {"messages": msgs}

    def sub_node(state: SubState) -> dict:
        return {"messages": [AIMessage(content="from sub", id=f"sub-{uuid.uuid4().hex[:8]}")]}

    sub = StateGraph(SubState)
    sub.add_node("inner", sub_node)
    sub.add_edge(START, "inner")
    sub.add_edge("inner", END)
    subgraph = sub.compile()

    def call_sub(state: State) -> dict:
        out = subgraph.invoke({"messages": state["messages"]})
        return {"messages": out["messages"][-1:]}

    g = StateGraph(State)
    g.add_node("model", model)
    g.add_node("rewrite", rewrite_whole_list)
    g.add_node("sub", call_sub)
    g.add_edge(START, "model")
    g.add_edge("model", "rewrite")
    g.add_edge("rewrite", "sub")
    g.add_edge("sub", END)
    return g.compile(checkpointer=saver)


async def _counts(pool: Any, thread_id: str) -> dict[str, int]:
    async with pool.connection() as conn, conn.cursor() as cur:
        out = {}
        for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
            await cur.execute(f"SELECT count(*) FROM {table} WHERE thread_id = %s", (thread_id,))
            row = await cur.fetchone()
            out[table] = int(row[0] if not isinstance(row, dict) else next(iter(row.values())))
        await cur.execute(
            "SELECT count(DISTINCT checkpoint_ns) FROM checkpoints WHERE thread_id = %s",
            (thread_id,),
        )
        row = await cur.fetchone()
        out["namespaces"] = int(row[0] if not isinstance(row, dict) else next(iter(row.values())))
        await cur.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = ''",
            (thread_id,),
        )
        row = await cur.fetchone()
        out["root"] = int(row[0] if not isinstance(row, dict) else next(iter(row.values())))
        return out


def _snapshot(values: dict) -> tuple:
    return (
        [(m.id, type(m).__name__, m.content) for m in values["messages"]],
        dict(sorted(values["files"].items())),
        values["note"],
    )


@pytest.fixture
async def saver_and_pool(pg_url):
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        conninfo=pg_url,
        min_size=1,
        max_size=4,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0},
    )
    await pool.open()
    saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
    await saver.setup()
    try:
        yield saver, pool
    finally:
        await pool.close()


async def _drive(graph: Any, thread_id: str, turns: int) -> dict:
    cfg = {"configurable": {"thread_id": thread_id}}
    for i in range(turns):
        await graph.ainvoke({"messages": [HumanMessage(content=f"q{i}", id=f"h-{i}")]}, cfg)
    return cfg


async def test_sweep_keeps_the_thread_state_and_drops_the_history(saver_and_pool) -> None:
    from olano.core.checkpoint_gc import collect

    saver, pool = saver_and_pool
    graph = _build_graph(saver)
    thread = f"gc-long-{uuid.uuid4().hex[:8]}"
    cfg = await _drive(graph, thread, 40)

    before = _snapshot((await graph.aget_state(cfg)).values)
    assert len(before[0]) == 40 * 3  # human + scrubbed reply + sub message per turn
    assert before[0][-2][2].endswith("(scrubbed)")
    rows_before = await _counts(pool, thread)
    assert rows_before["root"] > 150
    assert rows_before["namespaces"] > 40  # one sub namespace per turn

    report = await collect(pool, keep_min=3)
    assert not report.dry_run
    assert report.checkpoints_removed > 100
    assert report.writes_removed > 100
    rows_after = await _counts(pool, thread)
    # The root chain is cut back to the last snapshot of each delta channel
    # (every 5 updates here) plus the keep margin: a couple of turns' worth.
    assert rows_after["root"] <= 16
    assert rows_after["checkpoint_writes"] < rows_before["checkpoint_writes"] // 2
    assert rows_after["checkpoint_blobs"] < rows_before["checkpoint_blobs"]
    # The dead sub-namespaces are tiny and sit under the keep minimum: left alone.
    assert rows_after["namespaces"] == rows_before["namespaces"]

    # The state a load reconstructs is exactly what it was...
    assert _snapshot((await graph.aget_state(cfg)).values) == before
    # ...and the thread keeps working on top of it.
    await graph.ainvoke({"messages": [HumanMessage(content="after", id="h-after")]}, cfg)
    after = _snapshot((await graph.aget_state(cfg)).values)
    assert after[0][: len(before[0])] == before[0]
    # 120 messages + the new human one = 121 when the model node counts.
    assert [m[0] for m in after[0][len(before[0]) :]][:2] == ["h-after", "ai-121"]
    assert after[1]["f1"] == "v121"

    # A second sweep right away finds (almost) nothing: the tail it kept is
    # what the next load needs, and the new turn added a few rows at most.
    again = await collect(pool, keep_min=3)
    assert again.checkpoints_removed <= 12


async def test_orphan_report_reads_a_background_run_as_its_parents(saver_and_pool) -> None:
    """A cognition run writes under ``cog::<parent>__cog__…``; reading the
    prefix as an agent name counted every background run of every live agent
    as an orphan. Only the deleted agent's threads, its runs included, count."""
    from olano.core.checkpoint_gc import collect

    saver, pool = saver_and_pool
    graph = _build_graph(saver)
    tag = uuid.uuid4().hex[:8]
    live, gone = f"live{tag}", f"gone{tag}"
    gone_threads = [f"{gone}::t1", f"cog::{gone}__cog__pulse_agent__bb{tag[:4]}"]
    for thread in [f"{live}::t1", f"cog::{live}__cog__pulse_agent__aa{tag[:4]}", *gone_threads]:
        await _drive(graph, thread, 1)

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
        ids = [r["thread_id"] if isinstance(r, dict) else r[0] for r in await cur.fetchall()]
        # What the sweep should call an orphan: the namespaces of the deleted
        # agent's two threads that the sweep looks at (over keep_min).
        await cur.execute(
            "SELECT count(*) FROM (SELECT 1 FROM checkpoints WHERE thread_id = ANY(%s) "
            "GROUP BY thread_id, checkpoint_ns HAVING count(*) > 1) t",
            (gone_threads,),
        )
        row = await cur.fetchone()
        expected = int(row[0] if not isinstance(row, dict) else next(iter(row.values())))
    # Every other agent this module's threads name is live, and so is `live`;
    # `cog` is not an agent and must not need to be listed.
    others = {
        t.split("::", 1)[0]
        for t in ids
        if "::" in t and not t.startswith(("cog::", "sub::")) and tag not in t
    }
    rows_before = {t: await _counts(pool, t) for t in gone_threads}
    report = await collect(pool, keep_min=1, dry_run=True, live_agents=others | {live})
    assert expected >= 2
    assert report.orphan_namespaces == expected
    # A report, never a deletion.
    assert {t: await _counts(pool, t) for t in gone_threads} == rows_before


async def test_short_threads_and_dry_runs_are_left_alone(saver_and_pool) -> None:
    from olano.core.checkpoint_gc import collect, plan_namespace

    saver, pool = saver_and_pool
    graph = _build_graph(saver)
    short = f"gc-short-{uuid.uuid4().hex[:8]}"
    cfg = await _drive(graph, short, 1)
    before = _snapshot((await graph.aget_state(cfg)).values)
    rows_before = await _counts(pool, short)

    # One turn is four updates, under the snapshot cadence of five: no seed
    # on the root namespace, the cut stays at the oldest write, and nothing
    # in the chain is removable even though the chain is longer than keep_min.
    assert rows_before["root"] > 3
    async with pool.connection() as conn, conn.cursor() as cur:
        assert await plan_namespace(cur, short, "", keep_min=3) is None

    dry = await collect(pool, keep_min=3, dry_run=True)
    assert dry.dry_run
    assert await _counts(pool, short) == rows_before

    await collect(pool, keep_min=3)
    assert _snapshot((await graph.aget_state(cfg)).values) == before
    assert (await _counts(pool, short))["checkpoints"] == rows_before["checkpoints"]


async def test_keep_min_always_survives(saver_and_pool) -> None:
    from olano.core.checkpoint_gc import collect

    saver, pool = saver_and_pool
    graph = _build_graph(saver)
    thread = f"gc-keep-{uuid.uuid4().hex[:8]}"
    cfg = await _drive(graph, thread, 12)
    before = _snapshot((await graph.aget_state(cfg)).values)
    await collect(pool, keep_min=10)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = ''",
            (thread,),
        )
        row = await cur.fetchone()
        kept = int(row[0] if not isinstance(row, dict) else next(iter(row.values())))
    assert kept >= 10
    assert _snapshot((await graph.aget_state(cfg)).values) == before


async def test_pool_helper_hands_the_saver_pool_to_the_collector(pg_url, monkeypatch) -> None:
    from olano.core import checkpoints

    monkeypatch.setenv("LIONCLAW_CHECKPOINT_BACKEND", "postgres")
    monkeypatch.setenv("LIONCLAW_CHECKPOINT_DB_URL", pg_url)
    checkpoints.reset_checkpointers()
    try:
        pool = await checkpoints.aget_postgres_pool()
        assert pool is not None
        saver = await checkpoints.get_async_checkpointer("whoever")
        assert saver.conn is pool
    finally:
        await checkpoints.aclose_checkpointers()
        checkpoints.reset_checkpointers()


# ── migration 0029: a fresh snapshot on the next turn ─────────────────────────


async def _root_snapshots_after(pool: Any, thread_id: str, after_id: str) -> int:
    """Snapshot checkpoints (a stored `messages` value) newer than ``after_id``."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = '' "
            "AND checkpoint_id > %s AND (checkpoint -> 'channel_values') ? 'messages'",
            (thread_id, after_id),
        )
        row = await cur.fetchone()
        return int(row[0] if not isinstance(row, dict) else next(iter(row.values())))


async def _latest_root_id(pool: Any, thread_id: str) -> str:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT max(checkpoint_id) FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = ''",
            (thread_id,),
        )
        row = await cur.fetchone()
        return str(row[0] if not isinstance(row, dict) else next(iter(row.values())))


async def test_migration_arms_heavy_threads_for_a_snapshot_on_the_next_turn(
    saver_and_pool, pg_url
) -> None:
    import importlib

    from olano.core.checkpoint_gc import collect

    mig = importlib.import_module("olano.migrations.0029_checkpoint_history_resnapshot")
    saver, pool = saver_and_pool
    graph = _build_graph(saver)
    thread = f"gc-arm-{uuid.uuid4().hex[:8]}"
    # 4 updates per turn, snapshot every 5: after 3 turns the last snapshot is
    # at update 10 and two more updates are pending; the model/rewrite/sub
    # steps all wrote the whole list the old way, so this is the shape a box
    # arrives in.
    cfg = await _drive(graph, thread, 3)
    before = _snapshot((await graph.aget_state(cfg)).values)
    armed_at = await _latest_root_id(pool, thread)

    report = mig.arm_pending_snapshots(pg_url, min_pending=1)
    assert thread in {tid for tid, _n in report["threads"]}
    # Arming twice changes nothing.
    again = mig.arm_pending_snapshots(pg_url, min_pending=1)
    assert thread in {tid for tid, _n in again["threads"]}

    # The next turn's first update of `messages` writes a snapshot: the human
    # message is the first step, so even the input checkpoint carries one.
    await graph.ainvoke({"messages": [HumanMessage(content="next", id="h-next")]}, cfg)
    after = _snapshot((await graph.aget_state(cfg)).values)
    assert after[0][: len(before[0])] == before[0]
    assert after[0][len(before[0])][0] == "h-next"
    # The counters reset once the snapshot is written, so it is the first
    # checkpoint of the turn that carries it, not the last.
    assert await _root_snapshots_after(pool, thread, armed_at) >= 1

    # And the sweep now cuts straight to that fresh snapshot (scoped to the
    # messages channel: this test graph's own `files` channel, four updates
    # in, has no snapshot yet and rightly keeps its chain).
    await collect(pool, keep_min=1, channels=("messages",))
    rows = await _counts(pool, thread)
    # The snapshot is the turn's first checkpoint; the turn's four other
    # steps sit above it, and that is all that survives.
    assert rows["root"] <= 6
    assert _snapshot((await graph.aget_state(cfg)).values) == after


async def test_migration_leaves_light_threads_alone(saver_and_pool, pg_url) -> None:
    import importlib

    mig = importlib.import_module("olano.migrations.0029_checkpoint_history_resnapshot")
    saver, pool = saver_and_pool
    graph = _build_graph(saver)
    thread = f"gc-light-{uuid.uuid4().hex[:8]}"
    await _drive(graph, thread, 1)
    report = mig.arm_pending_snapshots(pg_url, min_pending=50)
    assert thread not in {tid for tid, _n in report["threads"]}


async def test_pending_history_bytes_follows_the_replay_and_shrinks_at_a_snapshot(
    saver_and_pool, pg_url
) -> None:
    """The miner's size guard measures what a load would replay: the writes
    since the last ``messages`` snapshot plus that snapshot, over every
    namespace. It grows while a thread accumulates writes and drops back once
    the delta channel snapshots (every 5 updates in this graph)."""
    from olano.analytics.threadcost import reader

    saver, pool = saver_and_pool
    graph = _build_graph(saver)
    thread = f"gc-size-{uuid.uuid4().hex[:8]}"
    cfg = {"configurable": {"thread_id": thread}}

    with _thread_db(pg_url) as tc:
        assert reader.pending_history_bytes(tc, thread) == 0

        await graph.ainvoke({"messages": [HumanMessage(content="q0", id="h-0")]}, cfg)
        one_turn = reader.pending_history_bytes(tc, thread)
        assert one_turn is not None and one_turn > 0

        # The second turn crosses the snapshot cadence: the root namespace now
        # holds a seed, so the pending history is the snapshot blob plus the
        # few writes after it, and the raw sum of every write ever stored is
        # strictly larger than what a load replays.
        await graph.ainvoke({"messages": [HumanMessage(content="q1", id="h-1")]}, cfg)
        two_turns = reader.pending_history_bytes(tc, thread)
        assert two_turns is not None and two_turns > 0
        raw_total = tc.execute(
            "SELECT coalesce(sum(octet_length(blob)), 0) FROM checkpoint_writes "
            "WHERE thread_id = ? AND channel = 'messages'",
            (thread,),
        ).fetchone()[0]
        assert two_turns < int(raw_total)

        # The whole-list node writes the entire conversation each step, so a
        # long thread's pending history outgrows a small cap: exactly the
        # thread the warmer refuses.
        await _drive(graph, thread, 20)
        long_run = reader.pending_history_bytes(tc, thread)
        assert long_run is not None and long_run > two_turns


class SeedlessFilesState(TypedDict):
    messages: Annotated[list, DeltaChannel(_messages_delta_reducer, snapshot_frequency=5)]
    # Written once, at the first step, and never again: under the cadence
    # forever, exactly like deepagents' `files` on a conversation that touched
    # a file once.
    files: Annotated[dict, DeltaChannel(_files_reducer, snapshot_frequency=1000)]
    note: str


def _build_seedless_files_graph(saver: Any):
    from langgraph.graph import END, START, StateGraph

    def model(state: SeedlessFilesState) -> dict:
        n = len(state["messages"])
        out: dict = {"messages": [AIMessage(content=f"reply {n}", id=f"ai-{n}")], "note": f"n{n}"}
        if n <= 1:
            out["files"] = {"readme": "v0"}
        return out

    g = StateGraph(SeedlessFilesState)
    g.add_node("model", model)
    g.add_edge(START, "model")
    g.add_edge("model", END)
    return g.compile(checkpointer=saver)


async def test_compaction_gives_a_never_snapshotted_channel_a_seed_and_sweeps_behind_it(
    saver_and_pool,
) -> None:
    """``files`` was written once and never snapshotted, so the cut sat at its
    oldest write and the sweep removed nothing, however long the thread grew.
    With a saver the sweep writes that snapshot itself and the chain opens."""
    from olano.core.checkpoint_gc import _specs_from_state, collect

    saver, pool = saver_and_pool
    graph = _build_seedless_files_graph(saver)
    thread = f"gc-seedless-{uuid.uuid4().hex[:8]}"
    cfg = await _drive(graph, thread, 40)
    before = (await graph.aget_state(cfg)).values
    before_key = ([(m.id, m.content) for m in before["messages"]], dict(before["files"]))
    rows_before = await _counts(pool, thread)
    assert rows_before["root"] > 80

    # Without a saver: pinned at the oldest `files` write (keyed one step
    # above the very first checkpoint, which is all the sweep can take) and
    # reported.
    pinned = await collect(pool, keep_min=3)
    assert rows_before["root"] - (await _counts(pool, thread))["root"] <= 1
    skipped = [s for s in pinned.compaction_skipped if s["namespace"] == f"{thread}/root"]
    assert skipped and skipped[0]["channels"] == ["files"] and skipped[0]["reason"] == "no saver"
    assert skipped[0]["compactable"] > 60

    # With one: a snapshot at the tip, then the sweep.
    report = await collect(
        pool,
        keep_min=3,
        saver=saver,
        channel_specs=_specs_from_state(SeedlessFilesState),
        quiet_seconds=0,
    )
    assert f"{thread}/root:files" in report.compacted
    rows_after = await _counts(pool, thread)
    assert rows_after["root"] <= 16
    assert rows_after["checkpoint_writes"] < rows_before["checkpoint_writes"] // 2

    after = (await graph.aget_state(cfg)).values
    assert ([(m.id, m.content) for m in after["messages"]], dict(after["files"])) == before_key
    await graph.ainvoke({"messages": [HumanMessage(content="after", id="h-after")]}, cfg)
    later = (await graph.aget_state(cfg)).values
    assert later["files"] == {"readme": "v0"}
    assert [m.id for m in later["messages"]][-2:] == ["h-after", "ai-81"]
    # Nothing left to compact: the snapshot is the seed now.
    again = await collect(
        pool, keep_min=3, saver=saver, channel_specs=_specs_from_state(SeedlessFilesState)
    )
    assert not [c for c in again.compacted if c.startswith(thread)]


async def test_compaction_resnapshots_a_seeded_channel_with_a_deep_replay(
    saver_and_pool,
) -> None:
    """A seeded channel whose writes since the seed outweigh ``resnapshot_bytes``
    gets a fresh snapshot at the tip too: that replay is what every load of a
    thread that never steps again would pay forever. The rebuild streams one
    write at a time and lands on the same state a load reconstructs."""
    from olano.core.checkpoint_gc import _specs_from_state, collect

    saver, pool = saver_and_pool
    graph = _build_graph(saver)
    thread = f"gc-deep-{uuid.uuid4().hex[:8]}"
    cfg = await _drive(graph, thread, 12)
    before = _snapshot((await graph.aget_state(cfg)).values)
    rows_before = await _counts(pool, thread)

    report = await collect(
        pool,
        keep_min=3,
        saver=saver,
        channel_specs=_specs_from_state(State),
        quiet_seconds=0,
        resnapshot_bytes=0,
    )
    compacted = sorted(c.split(":")[-1] for c in report.compacted if c.startswith(f"{thread}/root"))
    assert compacted == ["files", "messages"]
    rows_after = await _counts(pool, thread)
    # Both seeds sit at the tip now, so only the keep margin and the two
    # snapshot checkpoints remain on the root namespace.
    assert rows_after["root"] <= 5
    assert rows_after["root"] < rows_before["root"]
    assert _snapshot((await graph.aget_state(cfg)).values) == before
    await graph.ainvoke({"messages": [HumanMessage(content="after", id="h-after")]}, cfg)
    after = _snapshot((await graph.aget_state(cfg)).values)
    assert after[0][: len(before[0])] == before[0]
    assert [m[0] for m in after[0][len(before[0]) :]][:2] == ["h-after", "ai-37"]


async def test_compaction_leaves_a_young_tip_alone(saver_and_pool) -> None:
    """A tip younger than the quiet window may have a step in flight: nothing
    is appended, and the report says why."""
    from olano.core.checkpoint_gc import _specs_from_state, collect

    saver, pool = saver_and_pool
    graph = _build_seedless_files_graph(saver)
    thread = f"gc-young-{uuid.uuid4().hex[:8]}"
    await _drive(graph, thread, 10)
    rows_before = await _counts(pool, thread)
    report = await collect(
        pool,
        keep_min=3,
        saver=saver,
        channel_specs=_specs_from_state(SeedlessFilesState),
        quiet_seconds=3600,
    )
    assert not [c for c in report.compacted if c.startswith(thread)]
    skipped = [s for s in report.compaction_skipped if s["namespace"] == f"{thread}/root"]
    assert skipped and skipped[0]["reason"].startswith("tip is ")
    # The ordinary sweep still runs (the one checkpoint below the oldest
    # `files` write); the chain above it stays whole.
    assert rows_before["root"] - (await _counts(pool, thread))["root"] <= 1


async def test_pending_history_counts_the_writes_made_on_top_of_the_seed(
    saver_and_pool, pg_url, monkeypatch
) -> None:
    """A write row is keyed on the checkpoint it was made after, and the saver's
    chain walk appends the seed's own id before stopping there, so the step
    written on top of the snapshot is replayed with it. The size guard must
    count it: a thread whose one large write sits exactly at the seed used to
    measure as the snapshot alone and slip under the cap."""
    from langgraph.checkpoint.base import empty_checkpoint

    from olano.analytics.threadcost import reader, service

    saver, _pool = saver_and_pool
    thread = f"gc-seed-size-{uuid.uuid4().hex[:8]}"
    with _thread_db(pg_url) as tc:
        cfg: dict[str, Any] = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
        seed = empty_checkpoint()
        seed["channel_values"] = {"messages": []}
        seed["channel_versions"] = {"messages": "1"}
        cfg = tc.saver.put(cfg, seed, {"step": 0}, {"messages": "1"})
        update = [AIMessage(content="x" * (2 * 1024 * 1024), id="answer")]
        tc.saver.put_writes(cfg, [("messages", update)], "model")
        latest = empty_checkpoint()
        latest["channel_values"] = {}
        latest["channel_versions"] = {"messages": "2"}
        tc.saver.put(cfg, latest, {"step": 1}, {"messages": "2"})

        size = reader.pending_history_bytes(tc, thread)
        write_bytes = len(tc.saver.serde.dumps_typed(update)[1])
        assert size is not None and size >= write_bytes

        service._reset_mining_bounds_for_tests()
        monkeypatch.setenv(service._MAX_HISTORY_ENV, "1")
        assert service.history_too_large(tc, thread) == (True, size)


async def _backdate(pool: Any, thread: str, hours: int = 1) -> None:
    from datetime import datetime, timedelta, timezone

    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE checkpoints SET checkpoint = jsonb_set(checkpoint, '{ts}', to_jsonb(%s::text)) "
            "WHERE thread_id = %s",
            (stamp, thread),
        )


async def test_a_turn_resuming_during_the_rebuild_keeps_its_messages(
    saver_and_pool, monkeypatch
) -> None:
    """The review's regression (#682): a turn that resumes the thread between
    the compactor's guards and its publish used to be rolled back, because
    the snapshot carried the pre-turn channel versions under a newer id. The
    publish now re-reads the tip under the thread lock and gives up when it
    moved; the turn's messages stay, and the next pass starts over."""
    from olano.core import checkpoint_gc as GC

    saver, pool = saver_and_pool
    graph = _build_seedless_files_graph(saver)
    thread = f"gc-race-{uuid.uuid4().hex[:8]}"
    cfg = await _drive(graph, thread, 20)
    await _backdate(pool, thread)

    materialize = GC._materialize
    resumed: list[str] = []

    async def resume_during_rebuild(*args: Any, **kwargs: Any) -> Any:
        value = await materialize(*args, **kwargs)
        if not resumed:
            await graph.ainvoke(
                {"messages": [HumanMessage(content="New turn", id="concurrent-human")]}, cfg
            )
            resumed.extend(m.id for m in (await graph.aget_state(cfg)).values["messages"])
            assert "concurrent-human" in resumed
        return value

    monkeypatch.setattr(GC, "_materialize", resume_during_rebuild)
    async with pool.connection() as conn, conn.cursor() as cur:
        plan = await GC._plan(cur, thread, "", keep_min=3, channels=("messages", "files"))
        assert plan is not None and "files" in plan.seedless
        compacted, skipped = await GC.compact_namespace(
            saver, cur, plan, specs=_specs_from_state(SeedlessFilesState)
        )
    assert compacted == []
    assert skipped == "the tip moved during the rebuild"
    visible = [m.id for m in (await graph.aget_state(cfg)).values["messages"]]
    assert visible == resumed

    # With the thread quiet again, the next pass compacts against the new tip.
    monkeypatch.setattr(GC, "_materialize", materialize)
    await _backdate(pool, thread)
    report = await GC.collect(
        pool, keep_min=3, saver=saver, channel_specs=_specs_from_state(SeedlessFilesState)
    )
    assert f"{thread}/root:files" in report.compacted
    assert [m.id for m in (await graph.aget_state(cfg)).values["messages"]] == resumed


async def test_a_turn_holding_the_thread_lock_is_never_compacted_under(
    saver_and_pool,
) -> None:
    """What the runner and the cognition worker hold for the whole turn: while
    it is held the compactor skips the namespace and says so."""
    from olano.core import checkpoint_gc as GC
    from olano.core import thread_locks

    saver, pool = saver_and_pool
    graph = _build_seedless_files_graph(saver)
    thread = f"gc-held-{uuid.uuid4().hex[:8]}"
    await _drive(graph, thread, 10)
    await _backdate(pool, thread)
    rows_before = await _counts(pool, thread)
    hold = await thread_locks.acquire(thread)
    try:
        report = await GC.collect(
            pool, keep_min=3, saver=saver, channel_specs=_specs_from_state(SeedlessFilesState)
        )
    finally:
        hold.release()
    assert not [c for c in report.compacted if c.startswith(thread)]
    skipped = [s for s in report.compaction_skipped if s["namespace"] == f"{thread}/root"]
    assert skipped and skipped[0]["reason"] == "a turn holds the thread"
    assert rows_before["root"] - (await _counts(pool, thread))["root"] <= 1

    # Released: the same pass shape now compacts.
    report = await GC.collect(
        pool, keep_min=3, saver=saver, channel_specs=_specs_from_state(SeedlessFilesState)
    )
    assert f"{thread}/root:files" in report.compacted


def _specs_from_state(cls: Any) -> dict[str, Any]:
    from olano.core.checkpoint_gc import _specs_from_state as specs

    return specs(cls)


def _thread_db(pg_url: str):
    """A ``ThreadDBHandle`` over the test database, as ``open_thread_db`` builds one."""
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver

    from olano.analytics.threadcost.reader import ThreadDBHandle

    pconn = psycopg.connect(pg_url, autocommit=True)
    return closing(ThreadDBHandle("postgres", pconn, PostgresSaver(pconn)))
