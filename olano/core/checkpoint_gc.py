"""Checkpoint history garbage collection for the Postgres checkpointer.

LangGraph keeps every checkpoint a thread ever wrote and never deletes one.
Olano never travels back in time, so what a load actually needs is a short
tail of that history: the latest checkpoint of each ``(thread, namespace)``
with the blobs its ``channel_versions`` name and its own pending writes, plus,
for every **delta channel** (deepagents stores ``messages`` and ``files`` as
LangGraph ``DeltaChannel``\\s), the chain of writes from the latest checkpoint
back to the channel's *seed*: the newest ancestor holding a stored value for
it, a snapshot blob or an inline value. Everything older than the oldest seed
is dead weight. On one managed box it was 40 GB of ``checkpoint_writes`` for
3.5 GB of live blobs.

The cut mirrors the saver's own replay walk
(``AsyncPostgresSaver.aget_delta_channel_history``): the walk starts at the
latest checkpoint's parent and follows ``parent_checkpoint_id`` until it finds
a stored value for the channel, or runs out of rows. A delta channel with no
seed in the chain is reconstructed from its writes alone, so for it the cut
stays at its oldest write. Chains are linear here (Olano never forks a
thread), which is why "older than" can be read off ``checkpoint_id`` (a
time-ordered UUID) instead of walking parents row by row.

Rows are removed in three steps per namespace: ``checkpoint_writes`` and
``checkpoints`` older than the cut, then every ``checkpoint_blobs`` row no
surviving checkpoint names. A blob is keyed by ``(channel, version)``, not by
checkpoint, and a channel that has not changed in a thousand steps still
points at the blob written a thousand steps ago, so the blob sweep is by
reference, never by age.

**A channel that never snapshotted pins the whole chain, so the sweep writes
the snapshot itself.** ``files`` snapshots every 50 file updates or 5000
steps; a conversation that wrote files a handful of times early on and then
only talked never reaches either, so its oldest ``files`` write is the cut and
nothing above it can go. The retired generations of agent-to-agent threads
(``talk_to`` rolls a pair over after 200 turns) never take another step, so
they would hold their history forever: one managed box kept 8,257
checkpoints and 4 GB of writes on a thread whose last step was a day old.
When a saver is given, :func:`collect` replays such a channel from its writes
with LangGraph's own ``DeltaChannel.replay_writes`` (the same code a load
runs), appends one checkpoint carrying a ``_DeltaSnapshot`` of the value, and
sweeps behind it. The snapshot sits at the new tip: a load of that checkpoint
restores the value directly (``_delta_channels_to_replay`` only walks for a
channel with nothing stored), and the next step's walk starts at its parent,
which is this checkpoint. Guards: a namespace with writes pending at its tip
(a step in flight or an interrupt) or whose tip is younger than
``quiet_seconds`` is left alone, and a channel whose writes exceed
``compact_max_bytes`` is never materialized, because loading it whole is the
very thing the cost miner refuses to do.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

#: Channels deepagents stores as ``DeltaChannel``: their value is replayed
#: from writes, so their seed bounds what history a load needs.
DEFAULT_DELTA_CHANNELS: tuple[str, ...] = ("messages", "files")
DELTA_CHANNELS_ENV = "LIONCLAW_CHECKPOINT_GC_DELTA_CHANNELS"
#: Checkpoints always kept per namespace, whatever the seeds say: a run in
#: flight is writing new ones, and the margin keeps the sweep clear of them.
DEFAULT_KEEP_MIN = 3
#: Delete in slices of this many checkpoints so one namespace with 20,000 of
#: them does not hold a lock for the whole sweep.
DELETE_SLICE = 500
#: A channel is materialized only when the writes it would stream are under
#: this many bytes. Memory is bounded regardless (one write plus the running
#: value, see ``_materialize``); this bounds the time one namespace can take
#: from a pass, since every byte is read off the database once.
DEFAULT_COMPACT_MAX_BYTES = 4 * 1024 * 1024 * 1024
#: A namespace whose tip is younger than this is left uncompacted: a run may
#: be mid-step on it, and the snapshot must be appended to a settled tip.
DEFAULT_QUIET_SECONDS = 30 * 60
#: A seeded channel whose writes since its seed weigh more than this gets a
#: fresh snapshot too: that replay is what every load of the thread pays, and
#: a thread that never takes another step (a retired peer generation, a
#: stopped agent) would otherwise pay it forever. 8 MB is a few hundred
#: ordinary messages, or one whole-conversation write of the kind the
#: security middleware used to make.
DEFAULT_RESNAPSHOT_BYTES = 8 * 1024 * 1024


def delta_channels() -> tuple[str, ...]:
    raw = (os.environ.get(DELTA_CHANNELS_ENV) or "").strip()
    if not raw:
        return DEFAULT_DELTA_CHANNELS
    names = tuple(part.strip() for part in raw.split(",") if part.strip())
    return names or DEFAULT_DELTA_CHANNELS


@dataclass
class NamespacePlan:
    """What one ``(thread, namespace)`` keeps and what it sheds."""

    thread_id: str
    checkpoint_ns: str
    checkpoints: int
    latest_id: str
    cut_id: str
    removable: int
    seeds: dict[str, str | None] = field(default_factory=dict)
    #: Delta channels with no stored value in the chain, each with the bytes
    #: of writes a snapshot would replace: what pins the cut at their oldest
    #: write, and what a compaction would materialize.
    seedless: dict[str, int] = field(default_factory=dict)
    #: Seeded delta channels whose writes since the seed outweigh
    #: ``resnapshot_bytes``: what every load replays, with the bytes.
    deep: dict[str, int] = field(default_factory=dict)
    #: Checkpoints the cut would remove once every seedless and deep channel
    #: holds a snapshot at the tip; zero when the other channels pin the cut
    #: just as low.
    compactable: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "checkpoint_ns": self.checkpoint_ns,
            "checkpoints": self.checkpoints,
            "removable": self.removable,
            "cut_id": self.cut_id,
            "seeds": dict(self.seeds),
            "seedless": dict(self.seedless),
            "deep": dict(self.deep),
            "compactable": self.compactable,
        }


@dataclass
class GcReport:
    dry_run: bool
    namespaces_scanned: int = 0
    namespaces_swept: int = 0
    checkpoints_removed: int = 0
    writes_removed: int = 0
    blobs_removed: int = 0
    seconds: float = 0.0
    stopped_early: bool = False
    bytes_before: int | None = None
    bytes_after: int | None = None
    plans: list[dict[str, Any]] = field(default_factory=list)
    #: Seedless channels given a snapshot this pass (``thread/ns:channel``).
    compacted: list[str] = field(default_factory=list)
    #: Namespaces a compaction would open up but this pass could not touch
    #: (no saver, too large, a step in flight, a young tip), with the reason.
    compaction_skipped: list[dict[str, Any]] = field(default_factory=list)
    #: Namespaces whose thread belongs to no live agent (``live_agents`` given).
    orphan_namespaces: int = 0
    orphan_checkpoints: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "namespaces_scanned": self.namespaces_scanned,
            "namespaces_swept": self.namespaces_swept,
            "checkpoints_removed": self.checkpoints_removed,
            "writes_removed": self.writes_removed,
            "blobs_removed": self.blobs_removed,
            "seconds": round(self.seconds, 2),
            "stopped_early": self.stopped_early,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "compacted": list(self.compacted),
            "compaction_skipped": self.compaction_skipped[:20],
            "orphan_namespaces": self.orphan_namespaces,
            "orphan_checkpoints": self.orphan_checkpoints,
            "top": self.plans[:20],
        }


# The tip itself counts: a checkpoint holding the channel's value is restored
# from that value (the loader walks only for a channel with nothing stored),
# and the next step's walk starts at its parent, which is the tip. That is
# what lets a compaction's snapshot, appended at the tip, open the chain.
_SEED_SQL = """
SELECT c.checkpoint_id
FROM checkpoints c
WHERE c.thread_id = %(thread)s AND c.checkpoint_ns = %(ns)s AND c.checkpoint_id <= %(latest)s
  AND (
    (c.checkpoint -> 'channel_values') ? %(channel)s
    OR EXISTS (
      SELECT 1 FROM checkpoint_blobs b
      WHERE b.thread_id = c.thread_id AND b.checkpoint_ns = c.checkpoint_ns
        AND b.channel = %(channel)s
        AND b.version = c.checkpoint -> 'channel_versions' ->> %(channel)s
        AND b.type <> 'empty'
    )
  )
ORDER BY c.checkpoint_id DESC
LIMIT 1
"""

_OLDEST_WRITE_SQL = """
SELECT min(checkpoint_id), coalesce(sum(octet_length(blob)), 0) FROM checkpoint_writes
WHERE thread_id = %(thread)s AND checkpoint_ns = %(ns)s AND channel = %(channel)s
"""

_WRITES_SINCE_SQL = """
SELECT coalesce(sum(octet_length(blob)), 0) FROM checkpoint_writes
WHERE thread_id = %(thread)s AND checkpoint_ns = %(ns)s AND channel = %(channel)s
  AND checkpoint_id >= %(seed)s AND checkpoint_id < %(latest)s
"""

_PENDING_AT_TIP_SQL = """
SELECT count(*) FROM checkpoint_writes
WHERE thread_id = %(thread)s AND checkpoint_ns = %(ns)s AND checkpoint_id = %(latest)s
"""

_TIP_ID_SQL = """
SELECT max(checkpoint_id) FROM checkpoints
WHERE thread_id = %(thread)s AND checkpoint_ns = %(ns)s
"""

# The replay a load performs, as keys: every write of the channel keyed on a
# checkpoint from the seed up to (excluding) the tip, in the order the saver
# applies them (chain position, then task and index within a step). Keys
# first, then one blob at a time, so materializing a channel holds one write
# and the running value, never the whole history.
_WRITE_KEYS_SQL = """
SELECT checkpoint_id, task_id, idx FROM checkpoint_writes
WHERE thread_id = %(thread)s AND checkpoint_ns = %(ns)s AND channel = %(channel)s
  AND checkpoint_id >= %(seed)s AND checkpoint_id < %(latest)s
ORDER BY checkpoint_id, task_id, idx
"""

_WRITE_BLOB_SQL = """
SELECT type, blob FROM checkpoint_writes
WHERE thread_id = %(thread)s AND checkpoint_ns = %(ns)s AND channel = %(channel)s
  AND checkpoint_id = %(checkpoint_id)s AND task_id = %(task_id)s AND idx = %(idx)s
"""

_SEED_VALUE_SQL = """
SELECT c.checkpoint -> 'channel_values' -> %(channel)s AS inline, b.type, b.blob
FROM checkpoints c
LEFT JOIN checkpoint_blobs b
  ON b.thread_id = c.thread_id AND b.checkpoint_ns = c.checkpoint_ns
 AND b.channel = %(channel)s
 AND b.version = c.checkpoint -> 'channel_versions' ->> %(channel)s
WHERE c.thread_id = %(thread)s AND c.checkpoint_ns = %(ns)s AND c.checkpoint_id = %(seed)s
"""

_TIP_ROW_SQL = """
SELECT checkpoint, metadata FROM checkpoints
WHERE thread_id = %(thread)s AND checkpoint_ns = %(ns)s AND checkpoint_id = %(latest)s
"""

_ORPHAN_BLOBS_SQL = """
DELETE FROM checkpoint_blobs b
WHERE b.thread_id = %(thread)s AND b.checkpoint_ns = %(ns)s
  AND NOT EXISTS (
    SELECT 1 FROM checkpoints c, jsonb_each_text(c.checkpoint -> 'channel_versions') cv
    WHERE c.thread_id = b.thread_id AND c.checkpoint_ns = b.checkpoint_ns
      AND cv.key = b.channel AND cv.value = b.version
  )
"""

_SIZE_SQL = """
SELECT pg_total_relation_size('checkpoints') + pg_total_relation_size('checkpoint_blobs')
     + pg_total_relation_size('checkpoint_writes')
"""


async def plan_namespace(
    cur: Any,
    thread_id: str,
    checkpoint_ns: str,
    *,
    keep_min: int = DEFAULT_KEEP_MIN,
    channels: tuple[str, ...] | None = None,
) -> NamespacePlan | None:
    """Decide the cut for one namespace; ``None`` when nothing is removable.

    ``cur`` is an open psycopg async cursor on the checkpoint database.
    """
    plan = await _plan(cur, thread_id, checkpoint_ns, keep_min=keep_min, channels=channels)
    return plan if plan is not None and plan.removable > 0 else None


async def _plan(
    cur: Any,
    thread_id: str,
    checkpoint_ns: str,
    *,
    keep_min: int = DEFAULT_KEEP_MIN,
    channels: tuple[str, ...] | None = None,
    resnapshot_bytes: int = DEFAULT_RESNAPSHOT_BYTES,
) -> NamespacePlan | None:
    """The cut for one namespace, with what a compaction would open up.

    ``None`` only when the chain is under ``keep_min``; a plan with
    ``removable == 0`` says what pins it (``seeds``, ``seedless``, ``deep``).
    """
    await cur.execute(
        "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = %s "
        "ORDER BY checkpoint_id DESC",
        (thread_id, checkpoint_ns),
    )
    ids = [
        row[0] if not isinstance(row, dict) else row["checkpoint_id"]
        for row in await cur.fetchall()
    ]
    keep_min = max(1, int(keep_min))
    if len(ids) <= keep_min:
        return None
    latest = ids[0]
    margin = ids[keep_min - 1]
    cut = margin
    # The cut the seeded channels alone would allow: what a compaction of the
    # seedless ones buys.
    seeded_cut = margin
    seeds: dict[str, str | None] = {}
    seedless: dict[str, int] = {}
    deep: dict[str, int] = {}
    for channel in channels or delta_channels():
        params = {"thread": thread_id, "ns": checkpoint_ns, "latest": latest, "channel": channel}
        await cur.execute(_SEED_SQL, params)
        row = await cur.fetchone()
        seed = _scalar(row)
        if seed is None:
            # No stored value in the chain: the channel is rebuilt from its
            # writes alone, so every write stays, and so does every
            # checkpoint between them (the walk follows parent links).
            await cur.execute(_OLDEST_WRITE_SQL, params)
            oldest = await cur.fetchone()
            seed, write_bytes = _pair(oldest)
            if seed is not None:
                seedless[channel] = int(write_bytes or 0)
        else:
            if seed < latest:
                # Seeded, but how much does a load replay on top of it? A
                # channel that keeps getting written snapshots again on its
                # own; one on a thread that never steps again does not.
                await cur.execute(_WRITES_SINCE_SQL, {**params, "seed": seed})
                since = int(_scalar(await cur.fetchone()) or 0)
                if since > resnapshot_bytes:
                    deep[channel] = since
            if seed < seeded_cut and channel not in deep:
                seeded_cut = seed
        seeds[channel] = seed
        if seed is not None and seed < cut:
            cut = seed
    removable = sum(1 for cid in ids if cid < cut)
    compactable = 0
    if seedless or deep:
        compactable = max(0, sum(1 for cid in ids if cid < seeded_cut) - removable)
    return NamespacePlan(
        thread_id=thread_id,
        checkpoint_ns=checkpoint_ns,
        checkpoints=len(ids),
        latest_id=latest,
        cut_id=cut,
        removable=removable,
        seeds=seeds,
        seedless=seedless,
        deep=deep,
        compactable=compactable,
    )


def _scalar(row: Any) -> Any:
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()), None)
    return row[0]


def _pair(row: Any) -> tuple[Any, Any]:
    if row is None:
        return None, None
    values = list(row.values()) if isinstance(row, dict) else list(row)
    return (values[0] if values else None), (values[1] if len(values) > 1 else None)


# ───────────────────────────── compaction ──────────────────────────────────


def default_channel_specs() -> dict[str, Any]:
    """The ``DeltaChannel`` deepagents declares for each delta channel, by name.

    Read off the state classes' own annotations (``DeepAgentState.messages``,
    ``FilesystemState.files``) so the reducer, value type and cadence a
    compaction replays with are exactly what the running graph uses; a channel
    those classes do not declare has no spec and is never compacted.
    """
    specs: dict[str, Any] = {}
    try:
        from deepagents.graph import DeepAgentState
        from deepagents.middleware.filesystem import FilesystemState

        for cls in (DeepAgentState, FilesystemState):
            specs.update(_specs_from_state(cls))
    except Exception:  # pragma: no cover - deepagents is a hard dependency
        logger.debug("checkpoint gc: delta channel specs unavailable", exc_info=True)
    return specs


def _specs_from_state(cls: Any) -> dict[str, Any]:
    import typing

    from langgraph.channels.delta import DeltaChannel

    out: dict[str, Any] = {}
    try:
        hints = typing.get_type_hints(cls, include_extras=True)
    except Exception:
        hints = dict(getattr(cls, "__annotations__", {}) or {})
    for name, hint in hints.items():
        seen: list[Any] = [hint]
        while seen:
            item = seen.pop()
            if isinstance(item, DeltaChannel):
                out[name] = item
                break
            seen.extend(typing.get_args(item))
    return out


async def compact_namespace(
    saver: Any,
    cur: Any,
    plan: NamespacePlan,
    *,
    specs: dict[str, Any],
    max_bytes: int = DEFAULT_COMPACT_MAX_BYTES,
    quiet_seconds: float = DEFAULT_QUIET_SECONDS,
    now: float | None = None,
) -> tuple[list[str], str | None]:
    """Give every seedless or deep channel of ``plan`` a snapshot at the tip.

    Returns ``(channels compacted, reason nothing was)``. The value is rebuilt
    the way a load rebuilds it (the seed's stored value, then every write
    since it through the channel's own ``replay_writes``), but one write at a
    time off the database, so the pass holds the running value and a single
    write rather than the whole history: a 700 MB replay of whole-conversation
    writes materializes into the 15 MB conversation it describes. Then one
    checkpoint is appended through ``saver.aput`` with the value as a
    ``_DeltaSnapshot`` under a bumped version: the same row the pregel loop
    writes at a snapshot step, minus the step. The tip's other channels keep
    their versions, so the new checkpoint loads to exactly the state the old
    tip did.
    """
    from langgraph.checkpoint.base.id import uuid6
    from langgraph.checkpoint.serde.types import _DeltaSnapshot

    from olano.core import thread_locks

    params = {"thread": plan.thread_id, "ns": plan.checkpoint_ns, "latest": plan.latest_id}
    await cur.execute(_PENDING_AT_TIP_SQL, params)
    if int(_scalar(await cur.fetchone()) or 0) > 0:
        return [], "writes pending at the tip (a step in flight or an interrupt)"
    await cur.execute(_TIP_ROW_SQL, params)
    tip = _pair(await cur.fetchone())
    raw_checkpoint, raw_metadata = tip
    if not isinstance(raw_checkpoint, dict):
        return [], "tip checkpoint unreadable"
    age = _tip_age_seconds(raw_checkpoint.get("ts"), now=now)
    if age is None or age < quiet_seconds:
        return [], f"tip is {int(age or 0)}s old, under the {int(quiet_seconds)}s quiet window"

    done: list[str] = []
    skipped: str | None = None
    targets = {**plan.seedless, **plan.deep}
    for channel, write_bytes in targets.items():
        spec = specs.get(channel)
        if spec is None:
            skipped = f"no channel spec for {channel}"
            continue
        if write_bytes > max_bytes:
            skipped = f"{channel} holds {write_bytes // (1024 * 1024)} MB of writes, over the cap"
            continue
        # The rebuild runs unlocked: it can take a while on a deep channel and
        # only reads. Publication is what must be serialized against a turn.
        value = await _materialize(saver, cur, plan, channel, spec, seed_id=plan.seeds.get(channel))
        hold = thread_locks.try_acquire(plan.thread_id)
        if hold is None:
            skipped = "a turn holds the thread"
            break
        try:
            # Under the lock no turn can start on this thread. A turn that ran
            # between the guards above and now moved the tip or left writes on
            # it; either way the value rebuilt against the old tip is stale,
            # and the next pass starts over from the new one.
            await cur.execute(_TIP_ID_SQL, params)
            if _scalar(await cur.fetchone()) != plan.latest_id:
                skipped = "the tip moved during the rebuild"
                break
            await cur.execute(_PENDING_AT_TIP_SQL, params)
            if int(_scalar(await cur.fetchone()) or 0) > 0:
                skipped = "writes landed on the tip during the rebuild"
                break
            checkpoint = dict(raw_checkpoint)
            checkpoint["id"] = str(uuid6())
            checkpoint["ts"] = datetime.now(timezone.utc).isoformat()
            values = dict(checkpoint.get("channel_values") or {})
            values[channel] = _DeltaSnapshot(value)
            checkpoint["channel_values"] = values
            versions = dict(checkpoint.get("channel_versions") or {})
            new_version = saver.get_next_version(versions.get(channel), None)
            versions[channel] = new_version
            checkpoint["channel_versions"] = versions
            if "updated_channels" in checkpoint:
                checkpoint["updated_channels"] = [channel]
            metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
            counters = dict(metadata.get("counters_since_delta_snapshot") or {})
            counters.pop(channel, None)
            if counters:
                metadata["counters_since_delta_snapshot"] = counters
            else:
                metadata.pop("counters_since_delta_snapshot", None)
            config = {
                "configurable": {
                    "thread_id": plan.thread_id,
                    "checkpoint_ns": plan.checkpoint_ns,
                    "checkpoint_id": plan.latest_id,
                }
            }
            next_config = await saver.aput(config, checkpoint, metadata, {channel: new_version})
        finally:
            hold.release()
        # The next channel's snapshot goes on top of this one. The stored row
        # keeps ``True`` where a snapshot blob sits (what ``aput`` writes), so
        # the next copy starts from the shape the saver itself would read back.
        plan.latest_id = next_config["configurable"]["checkpoint_id"]
        params["latest"] = plan.latest_id
        raw_checkpoint = {**checkpoint, "channel_values": {**values, channel: True}}
        raw_metadata = metadata
        done.append(channel)
    return done, skipped


async def _materialize(
    saver: Any, cur: Any, plan: NamespacePlan, channel: str, spec: Any, *, seed_id: str | None
) -> Any:
    """The channel's value at the tip, rebuilt one stored write at a time.

    Mirrors ``_build_delta_channels_writes_history`` + ``replay_writes``: the
    seed's stored value (a ``_DeltaSnapshot`` blob, an inline primitive, or
    nothing), then every write keyed on a checkpoint from the seed up to the
    tip in chain order, then task and index. Reducers on a ``DeltaChannel``
    are batching-invariant by contract, so applying each write in its own
    ``replay_writes`` call equals the one batched call a load makes, and an
    ``Overwrite`` still resets the base at the point it appears.
    """
    from langgraph._internal._typing import MISSING

    base = MISSING
    params = {
        "thread": plan.thread_id,
        "ns": plan.checkpoint_ns,
        "channel": channel,
        "latest": plan.latest_id,
        "seed": seed_id if seed_id is not None else "",
    }
    if seed_id is not None:
        await cur.execute(_SEED_VALUE_SQL, params)
        row = await cur.fetchone()
        values = list(row.values()) if isinstance(row, dict) else list(row or [])
        inline, blob_type, blob = (values + [None, None, None])[:3]
        if blob is not None and blob_type not in (None, "empty"):
            base = saver.serde.loads_typed((blob_type, bytes(blob)))
        elif inline is not None:
            base = inline
    rebuilt = spec.from_checkpoint(base)
    await cur.execute(_WRITE_KEYS_SQL, params)
    keys = [
        (r["checkpoint_id"], r["task_id"], r["idx"]) if isinstance(r, dict) else (r[0], r[1], r[2])
        for r in await cur.fetchall()
    ]
    for checkpoint_id, task_id, idx in keys:
        await cur.execute(
            _WRITE_BLOB_SQL,
            {**params, "checkpoint_id": checkpoint_id, "task_id": task_id, "idx": idx},
        )
        row = await cur.fetchone()
        if row is None:
            continue
        blob_type, blob = _pair(row)
        value = saver.serde.loads_typed((blob_type, bytes(blob)))
        rebuilt.replay_writes([(task_id, channel, value)])
        del value
    return rebuilt.value


def _tip_age_seconds(ts: Any, *, now: float | None = None) -> float | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        stamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    current = now if now is not None else time.time()
    return current - stamp.timestamp()


async def _sweep_namespace(cur: Any, plan: NamespacePlan) -> tuple[int, int, int]:
    """Delete what ``plan`` marks removable. Returns (checkpoints, writes, blobs)."""
    await cur.execute(
        "SELECT checkpoint_id FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = %s "
        "AND checkpoint_id < %s ORDER BY checkpoint_id",
        (plan.thread_id, plan.checkpoint_ns, plan.cut_id),
    )
    doomed = [_scalar(row) if isinstance(row, dict) else row[0] for row in await cur.fetchall()]
    writes = checkpoints = 0
    for start in range(0, len(doomed), DELETE_SLICE):
        chunk = doomed[start : start + DELETE_SLICE]
        await cur.execute(
            "DELETE FROM checkpoint_writes WHERE thread_id = %s AND checkpoint_ns = %s "
            "AND checkpoint_id = ANY(%s)",
            (plan.thread_id, plan.checkpoint_ns, chunk),
        )
        writes += max(0, cur.rowcount or 0)
        await cur.execute(
            "DELETE FROM checkpoints WHERE thread_id = %s AND checkpoint_ns = %s "
            "AND checkpoint_id = ANY(%s)",
            (plan.thread_id, plan.checkpoint_ns, chunk),
        )
        checkpoints += max(0, cur.rowcount or 0)
    await cur.execute(_ORPHAN_BLOBS_SQL, {"thread": plan.thread_id, "ns": plan.checkpoint_ns})
    blobs = max(0, cur.rowcount or 0)
    return checkpoints, writes, blobs


async def collect(
    pool: Any,
    *,
    keep_min: int = DEFAULT_KEEP_MIN,
    dry_run: bool = False,
    max_seconds: float | None = None,
    channels: tuple[str, ...] | None = None,
    measure: bool = True,
    saver: Any | None = None,
    channel_specs: dict[str, Any] | None = None,
    compact_max_bytes: int = DEFAULT_COMPACT_MAX_BYTES,
    quiet_seconds: float = DEFAULT_QUIET_SECONDS,
    resnapshot_bytes: int = DEFAULT_RESNAPSHOT_BYTES,
    live_agents: set[str] | None = None,
) -> GcReport:
    """Sweep every namespace in the checkpoint database.

    ``pool`` is the ``psycopg_pool.AsyncConnectionPool`` the saver runs on
    (``saver.conn``). ``max_seconds`` stops the sweep between namespaces once
    the budget is spent; the next run picks up the rest (namespaces are
    visited largest first, so the budget goes where the bytes are).

    With ``saver`` (the ``AsyncPostgresSaver`` over the same pool), a
    namespace pinned by a delta channel that never snapshotted is compacted
    first (:func:`compact_namespace`) and swept in the same pass;
    ``channel_specs`` defaults to what deepagents declares. ``live_agents``
    only counts the namespaces whose thread belongs to no listed agent; it
    deletes nothing.
    """
    started = time.monotonic()
    report = GcReport(dry_run=dry_run)
    specs = channel_specs if channel_specs is not None else default_channel_specs()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if measure:
                report.bytes_before = await _size(cur)
            await cur.execute(
                "SELECT thread_id, checkpoint_ns, count(*) AS n FROM checkpoints "
                "GROUP BY 1, 2 HAVING count(*) > %s ORDER BY count(*) DESC",
                (max(1, int(keep_min)),),
            )
            rows = await cur.fetchall()
            targets = [
                (r["thread_id"], r["checkpoint_ns"], r["n"])
                if isinstance(r, dict)
                else (r[0], r[1], r[2])
                for r in rows
            ]
            if live_agents is not None:
                from olano.core.checkpoints import thread_owner

                for thread_id, _ns, n in targets:
                    # A background run (``cog::<parent>__cog__…``) is its
                    # parent's: reading ``cog`` as an agent name counted every
                    # one of them as an orphan.
                    owner = thread_owner(thread_id)
                    if owner is not None and owner not in live_agents:
                        report.orphan_namespaces += 1
                        report.orphan_checkpoints += int(n or 0)
            for thread_id, ns, _n in targets:
                if max_seconds is not None and time.monotonic() - started > max_seconds:
                    report.stopped_early = True
                    break
                report.namespaces_scanned += 1
                try:
                    plan = await _plan(
                        cur,
                        thread_id,
                        ns,
                        keep_min=keep_min,
                        channels=channels,
                        resnapshot_bytes=resnapshot_bytes,
                    )
                except Exception:
                    logger.warning(
                        "checkpoint gc: planning %s/%s failed", thread_id, ns, exc_info=True
                    )
                    continue
                if plan is None:
                    continue
                if (plan.seedless or plan.deep) and plan.compactable > 0:
                    label = f"{thread_id}/{ns or 'root'}"
                    wanted = sorted({**plan.seedless, **plan.deep})
                    if dry_run or saver is None:
                        report.compaction_skipped.append(
                            {
                                "namespace": label,
                                "channels": wanted,
                                "compactable": plan.compactable,
                                "reason": "dry run" if dry_run else "no saver",
                            }
                        )
                    else:
                        try:
                            done, reason = await compact_namespace(
                                saver,
                                cur,
                                plan,
                                specs=specs,
                                max_bytes=compact_max_bytes,
                                quiet_seconds=quiet_seconds,
                            )
                        except Exception:
                            logger.warning(
                                "checkpoint gc: compacting %s failed", label, exc_info=True
                            )
                            done, reason = [], "compaction failed (see log)"
                        report.compacted.extend(f"{label}:{ch}" for ch in done)
                        if reason:
                            report.compaction_skipped.append(
                                {
                                    "namespace": label,
                                    "channels": wanted,
                                    "compactable": plan.compactable,
                                    "reason": reason,
                                }
                            )
                        if done:
                            plan = await _plan(
                                cur,
                                thread_id,
                                ns,
                                keep_min=keep_min,
                                channels=channels,
                                resnapshot_bytes=resnapshot_bytes,
                            )
                            if plan is None:
                                continue
                if plan.removable <= 0:
                    continue
                report.plans.append(plan.as_dict())
                if dry_run:
                    report.checkpoints_removed += plan.removable
                    report.namespaces_swept += 1
                    continue
                try:
                    removed, writes, blobs = await _sweep_namespace(cur, plan)
                except Exception:
                    logger.warning(
                        "checkpoint gc: sweeping %s/%s failed", thread_id, ns, exc_info=True
                    )
                    continue
                report.namespaces_swept += 1
                report.checkpoints_removed += removed
                report.writes_removed += writes
                report.blobs_removed += blobs
            if measure and not dry_run:
                report.bytes_after = await _size(cur)
    report.plans.sort(key=lambda p: p["removable"], reverse=True)
    report.seconds = time.monotonic() - started
    return report


async def _size(cur: Any) -> int | None:
    try:
        await cur.execute(_SIZE_SQL)
        return int(_scalar(await cur.fetchone()) or 0)
    except Exception:
        return None
