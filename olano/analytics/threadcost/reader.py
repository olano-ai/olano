"""Mine per-call token/cost records from the LangGraph SQLite checkpoint store.

The deepagents thread checkpointer persists the *full* message list per thread to
``<home>/sessions/threads.db`` (see :mod:`olano.core.checkpoints`). Every stored
``AIMessage`` retains ``usage_metadata`` (input/output/total tokens + cache_read),
``response_metadata.model_name`` (the real per-call model), and ``tool_calls`` — so
the data needed to answer "which tool/thread/call burned the tokens" already exists,
durably and retroactively. This module reads it back **read-only**.

Key nuance: ``checkpoints`` is keyed by ``(thread_id, checkpoint_ns, checkpoint_id)``.
Subagents (the ``task`` tool) run under separate namespaces (``checkpoint_ns`` like
``'tools:<uuid>'``). A thread's ``ns=''`` messages contain only the parent's calls
plus the task *result*; the subagent's internal LLM calls live under
``(thread_id, 'tools:<id>')``. :func:`read_thread_callset` unions all namespaces so
total thread cost (incl. subagents) is captured.

Checkpoint formats: pre-v4 checkpoints store the full message list inline in
``channel_values`` (directly or via blobs), so ``get_tuple`` alone suffices. v4
checkpoints (langgraph-checkpoint >= 4.x with deepagents' ``DeltaChannel`` state)
store only per-step message *deltas* in ``checkpoint_writes``; the latest
checkpoint's ``channel_values`` usually omits ``messages`` entirely (a
``_DeltaSnapshot`` appears only at snapshot cadence). :func:`_messages_from_snapshot`
handles both: inline value when present, otherwise the saver's ancestor walk
(``get_delta_channel_history``) replayed through the same reducer the runtime uses.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite import SqliteSaver

from olano.analytics.threadcost.cost import cost_for_call, pricing_known
from olano.catalog.off_peak import parse_timestamp
from olano.config.home import (
    get_olano_paths,
    load_home_config,
    resolve_checkpoint_backend,
)

logger = logging.getLogger(__name__)

# subagent_type sits near the front of the task args, so a regex survives truncation.
_SUBAGENT_RE = re.compile(r'"subagent_type"\s*:\s*"([^"]+)"')


@dataclass(frozen=True)
class ToolCallRef:
    """A single tool invocation requested by an AI call."""

    id: str
    name: str
    subagent_type: str | None = None


@dataclass(frozen=True)
class CallRecord:
    """One LLM call (an ``AIMessage`` with usage) within a thread namespace."""

    thread_id: str
    ns: str  # '' for the parent agent, 'tools:<uuid>' for a subagent
    seq: int  # index of this message in the namespace's message list (stable id)
    message_id: str | None
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int
    usd: float
    pricing_known: bool
    tool_calls: tuple[ToolCallRef, ...]
    ts: str | None


@dataclass(frozen=True)
class ThreadCallSet:
    """Every LLM call for a thread, across the parent and all subagent namespaces."""

    thread_id: str
    parent_calls: list[CallRecord] = field(default_factory=list)
    subagent_calls: dict[str, list[CallRecord]] = field(default_factory=dict)
    version: str = ""  # invalidation key (count + max checkpoint_id over all ns)
    ts: str = ""  # latest-checkpoint timestamp (thread last-active), for daily bucketing
    read_errors: int = 0  # namespaces whose checkpoint state failed to load
    last_error: str = ""  # most recent load failure, for surfacing upward

    def all_calls(self) -> list[CallRecord]:
        calls = list(self.parent_calls)
        for ns_calls in self.subagent_calls.values():
            calls.extend(ns_calls)
        return calls


# ───────────────────────────────── connection ────────────────────────────────


def thread_db_path(home: str | Path | None = None) -> Path:
    return get_olano_paths(home).thread_db_path


class ThreadDBHandle:
    """Backend-agnostic read handle over the checkpoint store for analytics.

    Normalises the two things the reader needs across SQLite and Postgres:
    parameterised metadata queries (``?`` placeholders, auto-translated to ``%s``
    for Postgres) and a LangGraph ``saver`` exposing ``get_tuple``. Wraps a
    read-only sqlite3 connection or a psycopg connection (tuple rows for the
    positional metadata queries; the Postgres saver uses its own ``dict_row``
    cursors, so the two coexist on one connection). ``close()`` releases it.
    """

    def __init__(self, backend: str, conn: Any, saver: BaseCheckpointSaver) -> None:
        self.backend = backend
        self._conn = conn
        self.saver = saver

    def execute(self, sql: str, params: tuple = ()) -> Any:
        if self.backend == "postgres":
            # psycopg uses %-style placeholders, so any literal '%' (e.g. a
            # LIKE 'tools:%' pattern) must be doubled *before* we translate the
            # sqlite-style '?' placeholders to '%s' — otherwise psycopg reads the
            # bare '%' as a malformed placeholder. Source queries never contain
            # '%s', so escape-then-translate is order-safe.
            sql = sql.replace("%", "%%").replace("?", "%s")
        return self._conn.execute(sql, params)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("threadcost: thread-db handle close failed: %s", exc)


def thread_store_available(home: str | Path | None = None) -> bool:
    """True when the checkpoint store has data worth reading.

    SQLite: the ``threads.db`` file exists. Postgres: assume reachable — the
    metadata queries handle an empty/just-created ``checkpoints`` table gracefully,
    and backend resolution raises earlier if Postgres is misconfigured.
    """
    backend, _ = resolve_checkpoint_backend(load_home_config(home))
    if backend == "postgres":
        return True
    return thread_db_path(home).exists()


def open_thread_db(home: str | Path | None = None) -> ThreadDBHandle:
    """Open a read handle over the checkpoint store (SQLite read-only or Postgres).

    SQLite uses ``mode=ro`` (not ``immutable=1``) so concurrent writes are still
    seen coherently, with a busy timeout to ride out transient write locks.
    Postgres opens a short-lived autocommit connection (tuple rows for the
    positional metadata queries; the saver uses its own ``dict_row`` cursors).
    """
    backend, url = resolve_checkpoint_backend(load_home_config(home))
    # Same at-rest serde as the writers (core/checkpoints.checkpoint_serde):
    # without it, rows written under LANGGRAPH_AES_KEY do not decode here. It
    # follows the box's strict mode (LIONCLAW_CHECKPOINT_REQUIRE_ENCRYPTED) on
    # purpose: an analytics read of a plaintext row is the same leak the runtime
    # refuses, so it is refused here too rather than quietly costed.
    from olano.core.checkpoints import checkpoint_serde_kwargs

    serde_kwargs = checkpoint_serde_kwargs()
    if backend == "postgres":
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver

        assert url is not None  # resolve_checkpoint_backend guarantees it
        pconn = psycopg.connect(url, autocommit=True)
        saver: BaseCheckpointSaver = PostgresSaver(pconn, **serde_kwargs)  # type: ignore[arg-type]
        return ThreadDBHandle("postgres", pconn, saver)
    path = thread_db_path(home)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout = 5000")
    return ThreadDBHandle("sqlite", conn, SqliteSaver(conn, **serde_kwargs))


# ─────────────────────────────── cheap metadata ──────────────────────────────


def list_thread_ids(conn: ThreadDBHandle) -> list[str]:
    rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()
    return [r[0] for r in rows if r[0]]


def thread_namespaces(conn: ThreadDBHandle, thread_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT checkpoint_ns FROM checkpoints WHERE thread_id = ?",
        (thread_id,),
    ).fetchall()
    return [r[0] for r in rows]


def thread_version(conn: ThreadDBHandle, thread_id: str) -> str:
    """Cheap invalidation key — changes whenever any checkpoint is added.

    Deserializes nothing: just counts rows and takes the max checkpoint_id (UUID6,
    time-ordered) across all namespaces for the thread.
    """
    row = conn.execute(
        "SELECT COUNT(*), MAX(checkpoint_id) FROM checkpoints WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    count = int(row[0] or 0)
    max_id = str(row[1] or "")
    return f"{count}:{max_id}"


def all_thread_versions(conn: ThreadDBHandle) -> dict[str, str]:
    """One grouped query → ``{thread_id: version}`` for every thread (no blobs)."""
    rows = conn.execute(
        "SELECT thread_id, COUNT(*), MAX(checkpoint_id) FROM checkpoints GROUP BY thread_id"
    ).fetchall()
    return {r[0]: f"{int(r[1] or 0)}:{r[2] or ''}" for r in rows if r[0]}


def version_key(version: str | None) -> str:
    """The part of a version that says "the thread moved": its newest checkpoint id.

    ``thread_version`` is ``<count>:<max checkpoint_id>``. A new turn always
    mints a newer id, so the id alone is a complete invalidation key. The count
    is NOT: the checkpoint sweep (``core/checkpoint_gc``) deletes old rows and
    moves the count of every thread it touches, and comparing whole versions
    made each sweep mark hundreds of unchanged threads stale, so the next
    Insights or Overview request re-mined them all, the heavy ones included.
    """
    return (version or "").rsplit(":", 1)[-1]


# What the saver replays to load ``messages`` for one namespace: the writes
# from the channel's seed (the newest checkpoint holding a stored value, inline
# or as a blob) onwards plus that seed blob. The same walk ``core/checkpoint_gc``
# cuts on. *From* the seed, not after it: a write row is keyed on the checkpoint
# it was made after, and the saver's chain walk appends the seed's own id before
# it stops there, so the step written on top of the snapshot is replayed too.
# Bytes only: ``octet_length`` reads a TOASTed value's size off its
# pointer, so this costs an index scan over the thread's rows and no I/O on the
# blobs themselves. Positional placeholders and ``jsonb_exists`` rather than
# the ``?`` operator: ``ThreadDBHandle.execute`` rewrites every ``?`` into a
# psycopg placeholder, so the operator spelling cannot travel through it.
_PENDING_HISTORY_SQL = """
WITH seed AS (
  SELECT c.checkpoint_id
  FROM checkpoints c
  WHERE c.thread_id = ? AND c.checkpoint_ns = ?
    AND (
      jsonb_exists(c.checkpoint -> 'channel_values', 'messages')
      OR EXISTS (
        SELECT 1 FROM checkpoint_blobs b
        WHERE b.thread_id = c.thread_id AND b.checkpoint_ns = c.checkpoint_ns
          AND b.channel = 'messages'
          AND b.version = c.checkpoint -> 'channel_versions' ->> 'messages'
          AND b.type <> 'empty'
      )
    )
  ORDER BY c.checkpoint_id DESC
  LIMIT 1
)
SELECT
  coalesce((
    SELECT sum(octet_length(w.blob)) FROM checkpoint_writes w
    WHERE w.thread_id = ? AND w.checkpoint_ns = ? AND w.channel = 'messages'
      AND w.checkpoint_id >= coalesce((SELECT checkpoint_id FROM seed), '')
  ), 0)
  + coalesce((
    SELECT max(octet_length(b.blob))
    FROM seed s
    JOIN checkpoints c ON c.thread_id = ? AND c.checkpoint_ns = ?
                      AND c.checkpoint_id = s.checkpoint_id
    JOIN checkpoint_blobs b ON b.thread_id = c.thread_id AND b.checkpoint_ns = c.checkpoint_ns
                           AND b.channel = 'messages'
                           AND b.version = c.checkpoint -> 'channel_versions' ->> 'messages'
  ), 0)
"""


def pending_history_bytes(conn: ThreadDBHandle, thread_id: str) -> int | None:
    """How many stored bytes a full read of ``thread_id`` would decode, or None.

    Summed over every namespace of the thread, as :func:`read_thread_callset`
    reads them. ``None`` on the sqlite backend (its delta layout is its own and
    a developer box has no 4 GB threads) and on any query error: the caller
    decides what an unknown size means, and here it means "mine as before".

    Why this exists: a thread that has not taken a fresh ``messages`` snapshot
    since the whole-list middleware writes (see ``migrations/0029``) replays
    gigabytes to load, and the miner used to do that inline on an Overview or
    Insights request. One such read is the 12 GB climb that killed the
    runtime; this number is what lets the miner refuse it.
    """
    if conn.backend != "postgres":
        return None
    try:
        total = 0
        for ns in thread_namespaces(conn, thread_id):
            params = (thread_id, ns) * 3
            row = conn.execute(_PENDING_HISTORY_SQL, params).fetchone()
            total += int((row[0] if row else 0) or 0)
        return total
    except Exception as exc:
        logger.debug("threadcost: history size unavailable for %s: %s", thread_id, exc)
        return None


# ───────────────────────────────── extraction ────────────────────────────────


def _msg_kind(msg: Any) -> str:
    return type(msg).__name__


def _is_ai(msg: Any) -> bool:
    return _msg_kind(msg) in ("AIMessage", "AIMessageChunk")


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _tool_call_refs(msg: Any) -> tuple[ToolCallRef, ...]:
    refs: list[ToolCallRef] = []
    for tc in getattr(msg, "tool_calls", None) or []:
        if not isinstance(tc, dict):
            continue
        name = str(tc.get("name") or "")
        if not name:
            continue
        subagent = None
        if name == "task":
            args = tc.get("args")
            if isinstance(args, dict):
                subagent = args.get("subagent_type")
            if not subagent:
                m = _SUBAGENT_RE.search(json.dumps(tc.get("args") or ""))
                subagent = m.group(1) if m else None
        refs.append(ToolCallRef(id=str(tc.get("id") or ""), name=name, subagent_type=subagent))
    return tuple(refs)


def extract_call_records(
    messages: list[Any], thread_id: str, ns: str, *, fallback_model: str = ""
) -> list[CallRecord]:
    """Build one :class:`CallRecord` per AI message that carries usage metadata."""
    records: list[CallRecord] = []
    for idx, msg in enumerate(messages):
        if not _is_ai(msg):
            continue
        usage = _as_dict(getattr(msg, "usage_metadata", None))
        if not usage:
            continue
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
        cache_read = int(_as_dict(usage.get("input_token_details")).get("cache_read") or 0)

        meta = _as_dict(getattr(msg, "response_metadata", None))
        model = str(meta.get("model_name") or meta.get("model") or fallback_model or "unknown")
        ts = meta.get("created_at") or meta.get("created") or None

        records.append(
            CallRecord(
                thread_id=thread_id,
                ns=ns,
                seq=idx,
                message_id=getattr(msg, "id", None),
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                cache_read_tokens=cache_read,
                usd=cost_for_call(
                    model, input_tokens, output_tokens, cache_read, at=parse_timestamp(ts)
                ),
                pricing_known=pricing_known(model),
                tool_calls=_tool_call_refs(msg),
                ts=str(ts) if ts else None,
            )
        )
    return records


# Threads already warned about this process — checkpoint-read failures repeat on
# every warm pass, so warn once per thread (details stay at debug) to keep the
# failure visible in operator logs without flooding them.
_warned_read_failures: set[str] = set()


def _note_read_failure(thread_id: str, ns: str, exc: Exception) -> str:
    """Log a checkpoint-read failure (warning once per thread) and describe it."""
    detail = f"{type(exc).__name__}: {exc}"
    if thread_id not in _warned_read_failures:
        _warned_read_failures.add(thread_id)
        logger.warning(
            "threadcost: failed to load checkpoint state for thread %s (ns=%r): %s "
            "— the thread is excluded from cost mining until it reads back cleanly",
            thread_id,
            ns,
            detail,
        )
    else:
        logger.debug("threadcost: get_tuple failed for %s/%s: %s", thread_id, ns, exc)
    return detail


def _messages_from_snapshot(saver: BaseCheckpointSaver, snap: Any) -> list[Any]:
    """Materialize the message list for a loaded checkpoint tuple.

    Pre-v4 checkpoints carry ``messages`` inline in ``channel_values`` (a plain
    list, or a ``_DeltaSnapshot`` at snapshot cadence) — ``from_checkpoint``
    unwraps both. v4 delta checkpoints omit the key entirely; the saver's
    ancestor walk returns the nearest seed plus the per-step writes between it
    and this checkpoint, which are replayed through ``_messages_delta_reducer``
    — the same reducer deepagents' state schema wires into ``DeltaChannel`` —
    so the reconstruction matches runtime semantics exactly.
    """
    from langgraph.channels.delta import DeltaChannel
    from langgraph.graph.message import _messages_delta_reducer

    if not snap or not snap.checkpoint:
        return []
    values = snap.checkpoint.get("channel_values")
    values = values if isinstance(values, dict) else {}
    # DeltaChannel declares its reducer as (Any, Sequence[Any]) -> Any; the
    # concrete message reducer is narrower, hence the cast.
    reducer = cast("Callable[[Any, Sequence[Any]], Any]", _messages_delta_reducer)
    proto: DeltaChannel = DeltaChannel(reducer, list)
    if "messages" in values:
        channel = proto.from_checkpoint(values["messages"])
        return list(channel.get() or [])
    history = saver.get_delta_channel_history(config=snap.config, channels=["messages"])
    entry: Mapping[str, Any] = history.get("messages") or {}
    # An absent seed means "start empty" — a plain [] routes through the same
    # from_checkpoint branch as a stored empty list, which is equivalent.
    channel = proto.from_checkpoint(entry.get("seed", []))
    channel.replay_writes(list(entry.get("writes") or []))
    return list(channel.get() or [])


def read_namespace_messages(saver: BaseCheckpointSaver, thread_id: str, ns: str) -> list[Any]:
    """The full (latest-checkpoint) message list for one ``(thread, ns)`` partition."""
    messages, _ = read_namespace_messages_ex(saver, thread_id, ns)
    return messages


def read_namespace_messages_ex(
    saver: BaseCheckpointSaver, thread_id: str, ns: str
) -> tuple[list[Any], str | None]:
    """Like :func:`read_namespace_messages` but also returns the load error (if any),
    so API surfaces can distinguish "empty thread" from "unreadable checkpoint"."""
    cfg: Any = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns}}
    try:
        snap = saver.get_tuple(cfg)
        return _messages_from_snapshot(saver, snap), None
    except Exception as exc:  # defensive (corrupt blob, backend mismatch, …)
        return [], _note_read_failure(thread_id, ns, exc)


def read_thread_callset(
    conn: ThreadDBHandle, thread_id: str, *, fallback_model: str = ""
) -> ThreadCallSet:
    """Read every LLM call for a thread, unioning the parent + subagent namespaces."""
    saver = conn.saver  # read-only; tables already exist (no setup())
    namespaces = thread_namespaces(conn, thread_id)
    parent: list[CallRecord] = []
    subagents: dict[str, list[CallRecord]] = {}
    ts = ""
    read_errors = 0
    last_error = ""
    for ns in namespaces:
        cfg: Any = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns}}
        try:
            snap = saver.get_tuple(cfg)
            messages = _messages_from_snapshot(saver, snap)
        except Exception as exc:  # defensive (corrupt blob, backend mismatch, …)
            read_errors += 1
            last_error = _note_read_failure(thread_id, ns, exc)
            continue
        if not snap or not snap.checkpoint:
            continue
        if ns == "":
            ts = str(snap.checkpoint.get("ts") or "")
        if not messages:
            continue
        records = extract_call_records(messages, thread_id, ns, fallback_model=fallback_model)
        if not records:
            continue
        if ns == "":
            parent = records
        else:
            subagents[ns] = records
    return ThreadCallSet(
        thread_id=thread_id,
        parent_calls=parent,
        subagent_calls=subagents,
        version=thread_version(conn, thread_id),
        ts=ts,
        read_errors=read_errors,
        last_error=last_error,
    )
