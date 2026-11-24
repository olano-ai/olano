"""Make every heavy thread take a fresh snapshot on its next turn.

Until this release the security middleware wrote the whole message list back
on every step it rewrote one message, and deepagents' ``messages`` channel
replays every write since its last snapshot on each load (a ``DeltaChannel``,
snapshot every 50 updates). A box upgraded from such a release still carries
up to 49 of those whole-conversation writes per thread, and keeps replaying
them (666 MB per load on one peer thread) until the thread's next snapshot,
which with small writes is up to 49 more updates away.

This migration shortens that to one update. LangGraph decides to snapshot
from the counters it finds in the latest checkpoint's metadata
(``counters_since_delta_snapshot[channel] = [updates, supersteps]``, read back
by the pregel loop on the next step); setting the ``messages`` counter past
any ``snapshot_frequency`` makes the next update of the channel write a full
``_DeltaSnapshot`` blob. From then on a load is the snapshot alone, and the
runtime's checkpoint sweep (``core/checkpoint_gc``, ten minutes after boot)
removes the old writes behind it. Nothing is deleted or rewritten here beyond
that one metadata key; a thread that never gets another turn is left exactly
as it was.

Applies to the Postgres checkpointer only (the sqlite saver has its own delta
layout and never saw the whole-list writes at this scale). Idempotent: arming
an armed thread changes nothing. A database that cannot be reached raises, so
the migration stays pending and runs on the next boot, as the contract says.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ID = "0029_checkpoint_history_resnapshot"

#: A thread is armed once this many ``messages`` writes sit after its last
#: snapshot. Below that the replay is already cheap and an extra snapshot would
#: only cost disk.
MIN_PENDING_WRITES = 5
#: Past any ``snapshot_frequency`` LangGraph or deepagents may use.
ARMED_UPDATES = 1_000_000
STATEMENT_TIMEOUT_S = 300

_PENDING_SQL = """
WITH latest AS (
  SELECT DISTINCT ON (thread_id) thread_id, checkpoint_id
  FROM checkpoints WHERE checkpoint_ns = ''
  ORDER BY thread_id, checkpoint_id DESC
),
snap AS (
  SELECT thread_id, max(checkpoint_id) AS snap_id
  FROM checkpoints
  WHERE checkpoint_ns = '' AND (checkpoint -> 'channel_values') ? 'messages'
  GROUP BY thread_id
)
SELECT l.thread_id, l.checkpoint_id,
  (SELECT count(*) FROM checkpoint_writes w
   WHERE w.thread_id = l.thread_id AND w.checkpoint_ns = '' AND w.channel = 'messages'
     AND w.checkpoint_id > coalesce(s.snap_id, '')) AS pending
FROM latest l LEFT JOIN snap s USING (thread_id)
"""

_ARM_SQL = """
UPDATE checkpoints
SET metadata = jsonb_set(
  coalesce(metadata, '{}'::jsonb),
  '{counters_since_delta_snapshot}',
  coalesce(metadata -> 'counters_since_delta_snapshot', '{}'::jsonb) || %s::jsonb,
  true)
WHERE thread_id = %s AND checkpoint_ns = '' AND checkpoint_id = %s
"""


def _checkpoint_url(home: Path | None) -> str | None:
    from olano.config.home import load_home_config, resolve_checkpoint_backend

    backend, url = resolve_checkpoint_backend(load_home_config(home))
    if backend != "postgres" or not url:
        return None
    return url


def applicable() -> bool:
    """Only a box on the Postgres checkpointer has anything to arm."""
    try:
        return _checkpoint_url(None) is not None
    except Exception:
        return False


def arm_pending_snapshots(
    url: str,
    *,
    min_pending: int = MIN_PENDING_WRITES,
    statement_timeout_s: int = STATEMENT_TIMEOUT_S,
) -> dict[str, Any]:
    """Arm every root thread with ``min_pending`` or more replayed writes.

    Returns ``{"scanned": n, "armed": n, "threads": [...]}``; the thread list
    names what was armed, heaviest first, for the log.
    """
    import json

    import psycopg

    counter = json.dumps({"messages": [ARMED_UPDATES, 0]})
    scanned = 0
    armed: list[tuple[str, int]] = []
    with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = '{int(statement_timeout_s)}s'")
            cur.execute(_PENDING_SQL)
            rows = cur.fetchall()
            scanned = len(rows)
            for thread_id, checkpoint_id, pending in rows:
                if int(pending or 0) < min_pending:
                    continue
                cur.execute(_ARM_SQL, (counter, thread_id, checkpoint_id))
                if cur.rowcount:
                    armed.append((str(thread_id), int(pending)))
    armed.sort(key=lambda item: item[1], reverse=True)
    return {"scanned": scanned, "armed": len(armed), "threads": armed}


def apply(home: Path) -> None:
    url = _checkpoint_url(home)
    if url is None:
        logger.info("%s: sqlite checkpointer, nothing to arm", ID)
        return
    report = arm_pending_snapshots(url)
    top = ", ".join(f"{tid} ({n} writes)" for tid, n in report["threads"][:5])
    logger.info(
        "%s: %d of %d thread(s) armed for a fresh snapshot on their next turn%s",
        ID,
        report["armed"],
        report["scanned"],
        f": {top}" if top else "",
    )
